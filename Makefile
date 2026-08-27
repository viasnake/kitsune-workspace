.PHONY: install install-python install-web lint format typecheck test test-python test-web build artifacts cli-smoke check clean

install: install-python install-web

install-python:
	uv sync --all-packages --all-groups

install-web:
	pnpm install --frozen-lockfile

lint:
	uv run ruff check .
	pnpm web:lint

format:
	uv run ruff format .
	uv run ruff check --fix .

typecheck:
	uv run pyright
	pnpm web:typecheck

test: test-python test-web

test-python:
	uv run pytest -m "not e2e and not postgresql"

test-web:
	pnpm web:test

build:
	pnpm web:build

artifacts:
	uv build --all-packages --out-dir dist
	python tests/artifacts/verify_distributions.py dist

cli-smoke:
	uv run kitsune workspace --help >/dev/null

check: lint typecheck test build cli-smoke

clean:
	rm -rf .pytest_cache .ruff_cache .pyright htmlcov coverage.xml dist apps/workspace-web/dist apps/workspace-web/playwright-report apps/workspace-web/test-results
