# VERTICE developer commands. Run `make` to list them.
.DEFAULT_GOAL := help
.PHONY: help install migrate dev test check data replay preview plan recheck close lint format

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

check: ## Test every data source with a real request and show its status
	$(UV_RUN) python -m collectors.check

data: migrate ## Download universe, two years of daily bars and macro data (takes minutes)
	$(UV_RUN) python -m engine.ingest all

replay: migrate ## Replay past days: make replay FROM=2026-05-01 TO=2026-10-30 [AI=simulated]
	$(UV_RUN) python -m replay --from $(FROM) --to $(TO) --ai $(or $(AI),none)

preview: migrate ## Show the plan from the downloaded data: make preview [AI=simulated|real]
	$(UV_RUN) python -m engine.preview --ai $(or $(AI),none)

plan: migrate ## Build and save today's plan: make plan [AI=real|simulated|none]
	$(UV_RUN) python -m engine.daily premarket --ai $(or $(AI),real)

recheck: migrate ## Review today's plan after the open (purchases that opened too high)
	$(UV_RUN) python -m engine.daily recheck

close: migrate ## Settle the day: shadows, exit rules on them and the day's capital
	$(UV_RUN) python -m engine.daily close

lint: ## Check code style and common mistakes
	$(UV_RUN) ruff check .
	$(UV_RUN) ruff format --check .

format: ## Rewrite the code in the project's style
	$(UV_RUN) ruff format .
	$(UV_RUN) ruff check --fix .
