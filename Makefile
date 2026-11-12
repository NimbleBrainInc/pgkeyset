.PHONY: help format format-check lint typecheck test verify

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

format: ## Format with ruff
	uv run ruff format src/ tests/

format-check: ## Check formatting
	uv run ruff format --check src/ tests/

lint: ## Lint with ruff
	uv run ruff check src/ tests/

typecheck: ## Type check with ty
	uv run ty check src/ tests/

test: ## Run the tests (the walk suites need PGKEYSET_TEST_URL; see README)
	uv run pytest -q

verify: format-check lint typecheck test ## The full gate CI runs
