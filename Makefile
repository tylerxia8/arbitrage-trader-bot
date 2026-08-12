.DEFAULT_GOAL := help
VENV := .venv
ifeq ($(OS),Windows_NT)
	BIN := $(VENV)/Scripts
else
	BIN := $(VENV)/bin
endif
PY := $(BIN)/python

.PHONY: help setup lint format typecheck fr002 test check migrate downgrade up down clean

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-12s %s\n", $$1, $$2}'

setup: ## Create the virtualenv and install everything (one command, from clean)
	uv python install 3.12
	uv venv --python 3.12
	uv pip install -e ".[dev]"
	@echo "Done. Run 'make check' to verify."

lint: ## Lint and check formatting
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

format: ## Apply formatting and safe lint fixes
	$(BIN)/ruff check . --fix
	$(BIN)/ruff format .

typecheck: ## Run mypy in strict mode
	$(PY) -m mypy

fr002: ## Assert no binary floating point in the money path
	$(PY) tools/check_no_float.py

test: ## Run the test suite with coverage
	$(PY) -m pytest --cov --cov-report=term-missing

check: lint typecheck fr002 test ## Everything CI runs

migrate: ## Apply migrations to the configured database
	$(BIN)/alembic upgrade head

downgrade: ## Roll back one migration
	$(BIN)/alembic downgrade -1

up: ## Start Postgres in the background
	docker compose up -d db

down: ## Stop containers
	docker compose down

clean: ## Remove caches and the virtualenv
	rm -rf $(VENV) .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage
