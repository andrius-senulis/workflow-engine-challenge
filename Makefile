.PHONY: setup lint fix format test check run live-test

setup:  ## Install Python and all dependencies
	uv sync

lint:  ## Check lint, formatting and types
	uv run ruff check .
	uv run ruff format --check .
	uv run ty check

fix:  ## Fix lint issues and format the code
	uv run ruff check --fix .
	uv run ruff format .

format:  ## Format the code
	uv run ruff format .

test:  ## Run the tests
	uv run pytest

check: lint test  ## Run lint, type checks and tests

run:  ## Start the API on http://127.0.0.1:8000 (docs at /docs)
	uv run uvicorn workflow.app:create_app --factory --reload

live-test:  ## Drive a running API (start it with `make run`) through sample scenarios (needs curl and jq)
	./scripts/live_test.sh
