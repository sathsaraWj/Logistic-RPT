.PHONY: install lint format typecheck test test-unit test-integration security-check \
        docs-check ci up down logs migrate run-api run-worker clean

UV ?= uv

install: ## Install main + dev dependencies (fast; excludes the heavy `ml` group).
	$(UV) sync

install-ml: ## Also install torch/mlflow/scikit-learn (needed from Phase 10 onward).
	$(UV) sync --group ml

lint: ## Ruff lint + format check.
	$(UV) run ruff check .
	$(UV) run ruff format --check .

format: ## Ruff auto-format.
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

typecheck: ## mypy over the hermes_rpt package.
	$(UV) run mypy src/hermes_rpt

test: test-unit ## Alias for the test suites runnable without external services.

test-unit:
	$(UV) run pytest tests/unit -v

test-integration: ## Requires `make up` first (Postgres/MLflow via Docker Compose).
	$(UV) run pytest tests/integration -v

test-security:
	$(UV) run pytest tests/security -v

test-model: ## Requires the `ml` dependency group.
	$(UV) run pytest tests/model -v

security-check: ## Static security analysis + dependency vulnerability scan.
	$(UV) run bandit -c pyproject.toml -r src apps
	$(UV) run pip-audit

docs-check: ## Verify markdown cross-references resolve and ADRs have required sections.
	$(UV) run python scripts/check_docs.py

ci: lint typecheck test docs-check ## What CI runs for non-integration, non-model phases.

up: ## Start local dev services (control-plane Postgres, MLflow).
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f

migrate: ## Apply Alembic migrations to the control-plane database.
	$(UV) run alembic upgrade head

run-api:
	$(UV) run uvicorn apps.api.main:app --reload

run-worker:
	$(UV) run python -m apps.worker.main

clean:
	$(UV) run python -c "import shutil; [shutil.rmtree(p, ignore_errors=True) for p in ('.pytest_cache', '.mypy_cache', '.ruff_cache')]"
