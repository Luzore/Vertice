# VERTICE developer commands. Run `make` to list them.
.DEFAULT_GOAL := help
.PHONY: help install migrate dev test lint format

BACKEND := backend
UV_RUN := cd $(BACKEND) && uv run --frozen

help: ## List the available commands
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "} {printf "  make %-10s %s\n", $$1, $$2}'

install: ## Install the pinned backend dependencies
	cd $(BACKEND) && uv sync --frozen

migrate: ## Create or upgrade the database schema
	$(UV_RUN) alembic upgrade head

dev: migrate ## Run the API locally with auto-reload on http://127.0.0.1:8000
	$(UV_RUN) uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

test: ## Run the whole test suite
	$(UV_RUN) pytest

lint: ## Check code style and common mistakes
	$(UV_RUN) ruff check .
	$(UV_RUN) ruff format --check .

format: ## Rewrite the code in the project's style
	$(UV_RUN) ruff format .
	$(UV_RUN) ruff check --fix .
