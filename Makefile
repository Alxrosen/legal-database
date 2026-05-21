.PHONY: install test lint format check clean

# Use uv for dependency management. https://docs.astral.sh/uv/
install:
	uv sync --extra dev

test:
	uv run pytest

lint:
	uv run ruff check src tests

format:
	uv run ruff format src tests
	uv run ruff check --fix src tests

# Lint + format-check without modifying files (CI mode)
check:
	uv run ruff check src tests
	uv run ruff format --check src tests

clean:
	rm -rf .pytest_cache .ruff_cache .coverage htmlcov dist build *.egg-info
