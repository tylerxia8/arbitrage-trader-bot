#!/usr/bin/env python3
"""Static enforcement of FR-002: no binary floating point in the money path.

Ruff and mypy cannot express this rule -- ``float`` is a perfectly ordinary
type, and the prohibition is scoped to particular modules rather than to the
language. So it is enforced here, at the AST level, over the roots declared in
``[tool.arbbot] money_path_roots``.

What counts as a violation:

*   a float literal (``0.05``), because ``Decimal(0.05)`` is not 0.05;
*   any use of the name ``float`` -- as a call, an annotation, or a cast;
*   importing a module whose functions return floats (``math``, ``statistics``,
    ``numpy``, ``random``, ``cmath``).

One exemption: ``isinstance(x, float)``. That is a *rejection* of float, which
is the behaviour this rule exists to produce, so flagging it would punish the
correct pattern. A trailing ``# money-path: allow`` comment suppresses a line
where a human has justified it in review. (The marker deliberately avoids
ruff's ``# noqa:`` namespace so the two linters cannot confuse each other.)

Run directly (``python tools/check_no_float.py``) or via the test suite, which
also verifies that this checker actually catches a planted violation -- a
silent checker is worse than none.
"""

from __future__ import annotations

import argparse
import ast
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

FLOAT_PRODUCING_MODULES = frozenset({"math", "cmath", "statistics", "numpy", "random"})
SUPPRESSION = "money-path: allow"

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True, slots=True)
class Violation:
    path: Path
    line: int
    col: int
    message: str

    def render(self, root: Path) -> str:
        try:
            shown = self.path.relative_to(root)
        except ValueError:
            shown = self.path
        return f"{shown}:{self.line}:{self.col}: {self.message}"


class MoneyPathVisitor(ast.NodeVisitor):
    """Collects float usage in a single module."""

    def __init__(self, path: Path, source_lines: list[str]) -> None:
        self.path = path
        self.lines = source_lines
        self.violations: list[Violation] = []
        self._exempt_nodes: set[int] = set()

    # -- helpers ---------------------------------------------------------
    def _suppressed(self, line: int) -> bool:
        if 1 <= line <= len(self.lines):
            return SUPPRESSION in self.lines[line - 1]
        return False

    def _report(self, node: ast.AST, message: str) -> None:
        line = getattr(node, "lineno", 0)
        if self._suppressed(line):
            return
        self.violations.append(
            Violation(self.path, line, getattr(node, "col_offset", 0) + 1, message)
        )

    # -- visits ----------------------------------------------------------
    def visit_Call(self, node: ast.Call) -> None:
        # isinstance(x, float) and issubclass(t, float) reject floats; exempt
        # the `float` name inside them, but still inspect everything else.
        if isinstance(node.func, ast.Name) and node.func.id in ("isinstance", "issubclass"):
            for arg in node.args[1:]:
                for sub in ast.walk(arg):
                    if isinstance(sub, ast.Name) and sub.id == "float":
                        self._exempt_nodes.add(id(sub))
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id == "float" and id(node) not in self._exempt_nodes:
            self._report(
                node,
                "'float' is not permitted in the money path (FR-002); "
                "use decimal.Decimal, or arbbot.money.to_usd at the boundary",
            )
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, float | complex) and not isinstance(node.value, bool):
            self._report(
                node,
                f"float literal {node.value!r} is not permitted in the money path "
                f"(FR-002); write Decimal({str(node.value)!r}) instead",
            )
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            root = alias.name.split(".")[0]
            if root in FLOAT_PRODUCING_MODULES:
                self._report(
                    node,
                    f"module '{alias.name}' returns floats and is not permitted "
                    f"in the money path (FR-002)",
                )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        root = (node.module or "").split(".")[0]
        if root in FLOAT_PRODUCING_MODULES:
            self._report(
                node,
                f"module '{node.module}' returns floats and is not permitted "
                f"in the money path (FR-002)",
            )
        self.generic_visit(node)


def check_file(path: Path) -> list[Violation]:
    """Return every float violation in one Python file."""
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:  # pragma: no cover -- ruff catches this first
        return [Violation(path, exc.lineno or 0, exc.offset or 0, f"syntax error: {exc.msg}")]
    visitor = MoneyPathVisitor(path, source.splitlines())
    visitor.visit(tree)
    return visitor.violations


def money_path_roots(repo_root: Path) -> list[str]:
    """Read the declared money-path roots from pyproject.toml."""
    pyproject = repo_root / "pyproject.toml"
    with pyproject.open("rb") as handle:
        data = tomllib.load(handle)
    roots = data.get("tool", {}).get("arbbot", {}).get("money_path_roots", [])
    if not roots:
        raise SystemExit(
            "pyproject.toml declares no [tool.arbbot] money_path_roots; "
            "FR-002 cannot be enforced against an empty set"
        )
    return list(roots)


def collect_files(repo_root: Path, roots: list[str]) -> list[Path]:
    """Expand declared roots to concrete Python files.

    Roots that do not exist yet are skipped rather than failing: they name
    modules that later milestones will add, and declaring them now means the
    rule applies from the first commit that creates them.
    """
    files: list[Path] = []
    for root in roots:
        target = repo_root / root
        if target.is_dir():
            files.extend(sorted(target.rglob("*.py")))
        elif target.is_file():
            files.append(target)
    return files


def run(repo_root: Path = REPO_ROOT) -> list[Violation]:
    """Check every declared money-path file. Returns all violations found."""
    files = collect_files(repo_root, money_path_roots(repo_root))
    return [v for path in files for v in check_file(path)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root", type=Path, default=REPO_ROOT, help="repository root to check"
    )
    args = parser.parse_args()

    violations = run(args.repo_root)
    if violations:
        print(f"FR-002 violations ({len(violations)}):", file=sys.stderr)
        for violation in violations:
            print(f"  {violation.render(args.repo_root)}", file=sys.stderr)
        return 1

    print("FR-002: no float in the money path.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
