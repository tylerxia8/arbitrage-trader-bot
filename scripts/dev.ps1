<#
.SYNOPSIS
    Windows equivalent of the Makefile targets.

.DESCRIPTION
    Windows has no make, and this project's primary development machine is
    Windows, so the same tasks live here. Keep the two in sync: CI runs the
    Makefile, and a check that only exists on one platform is a check that
    will eventually be wrong on the other.

.EXAMPLE
    .\scripts\dev.ps1 setup
    .\scripts\dev.ps1 check
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('setup', 'lint', 'format', 'typecheck', 'fr002', 'test', 'check', 'migrate', 'up', 'down', 'help')]
    [string]$Task = 'help'
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$bin = Join-Path $repo '.venv\Scripts'
$py = Join-Path $bin 'python.exe'

function Assert-Venv {
    if (-not (Test-Path $py)) {
        throw "No virtualenv found. Run: .\scripts\dev.ps1 setup"
    }
}

function Invoke-Step([string]$Name, [scriptblock]$Body) {
    Write-Host "==> $Name" -ForegroundColor Cyan
    & $Body
    if ($LASTEXITCODE -ne 0) { throw "$Name failed (exit $LASTEXITCODE)" }
}

Push-Location $repo
try {
    switch ($Task) {
        'setup' {
            if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
                throw "uv is required. Install it from https://docs.astral.sh/uv/"
            }
            Invoke-Step 'uv python install 3.12' { uv python install 3.12 }
            Invoke-Step 'uv venv' { uv venv --python 3.12 }
            Invoke-Step 'install' { uv pip install -e ".[dev]" }
            Write-Host "Done. Run: .\scripts\dev.ps1 check" -ForegroundColor Green
        }
        'lint' {
            Assert-Venv
            Invoke-Step 'ruff check' { & "$bin\ruff.exe" check . }
            Invoke-Step 'ruff format --check' { & "$bin\ruff.exe" format --check . }
        }
        'format' {
            Assert-Venv
            Invoke-Step 'ruff check --fix' { & "$bin\ruff.exe" check . --fix }
            Invoke-Step 'ruff format' { & "$bin\ruff.exe" format . }
        }
        'typecheck' { Assert-Venv; Invoke-Step 'mypy' { & $py -m mypy } }
        'fr002' { Assert-Venv; Invoke-Step 'FR-002' { & $py tools\check_no_float.py } }
        'test' { Assert-Venv; Invoke-Step 'pytest' { & $py -m pytest --cov --cov-report=term-missing } }
        'check' {
            & $PSCommandPath lint
            & $PSCommandPath typecheck
            & $PSCommandPath fr002
            & $PSCommandPath test
            Write-Host "All checks passed." -ForegroundColor Green
        }
        'migrate' { Assert-Venv; Invoke-Step 'alembic upgrade head' { & "$bin\alembic.exe" upgrade head } }
        'up' { Invoke-Step 'docker compose up -d db' { docker compose up -d db } }
        'down' { Invoke-Step 'docker compose down' { docker compose down } }
        default {
            Write-Host "Tasks: setup, lint, format, typecheck, fr002, test, check, migrate, up, down"
        }
    }
}
finally {
    Pop-Location
}
