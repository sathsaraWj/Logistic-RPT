.PHONY: install lint format typecheck test test-unit test-integration security-check \
        docs-check ci up down logs migrate run-api run-worker clean build-synthetic-dataset \
        train-baselines train-hermes-rpt pretrain-hermes-rpt adapt-hermes-rpt \
        demo-up demo-seed demo-discover demo-map demo-train demo-predict demo-security-test \
        demo-down

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

build-synthetic-dataset: ## Generate synthetic Alpha/Beta fleet data and build a sample dataset + quality report.
	$(UV) run python scripts/build_synthetic_dataset.py

train-baselines: ## Train all three baseline models on a synthetic dataset; requires `make install-ml`.
	$(UV) run python -m apps.trainer.main baselines

train-hermes-rpt: ## Train baselines + Hermes-RPT-0.1 (Tiny) and compare; requires `make install-ml`.
	$(UV) run python -m apps.trainer.main hermes-rpt

pretrain-hermes-rpt: ## Pretrain + fine-tune Hermes-RPT-0.1 (Tiny) vs. scratch, plus baselines; requires `make install-ml`.
	$(UV) run python -m apps.trainer.main pretrain

adapt-hermes-rpt: ## Train a shared Hermes-RPT-0.1 backbone + per-tenant private adapters (Alpha, Beta) and compare; requires `make install-ml`.
	$(UV) run python -m apps.trainer.main adapt

# --- Phase 17: end-to-end two-tenant demonstration -------------------------------------------
# See docs/DEMO.md for the full walkthrough, expected output, and what each step proves.
# Requires `make install-ml` (the API app imports mlflow/torch transitively). If the default
# ports (5432/5433/5434/5000) are already taken on your machine, override CONTROL_PLANE_DB_PORT/
# TENANT_ALPHA_DB_PORT/TENANT_BETA_DB_PORT/MLFLOW_PORT — the same env vars every demo-* target
# and docker-compose.yml itself both read, so setting them once (e.g. in a `.env.demo` you
# `export`) is enough.

demo-up: ## Start the full demo stack (control-plane DB, MLflow, Tenant Alpha/Beta databases) and migrate.
	docker compose up -d
	$(UV) run alembic upgrade head

demo-seed: ## Create Alpha/Beta demo tenants and seed synthetic operational history into their real databases.
	$(UV) run --group ml python -m scripts.demo seed

demo-discover: ## Run real schema discovery against both tenant databases.
	$(UV) run --group ml python -m scripts.demo discover

demo-map: ## Create, validate, and activate tenant-specific schema mappings onto the shared ontology.
	$(UV) run --group ml python -m scripts.demo map

demo-train: ## Build tenant datasets, train baselines + Hermes-RPT-0.1 Tiny, promote to production.
	$(UV) run --group ml python -m scripts.demo train

demo-predict: ## Execute a real prediction per tenant and show its full lineage.
	$(UV) run --group ml python -m scripts.demo predict

demo-security-test: ## Trigger drift, suspend the affected mapping, prove isolation and log safety.
	$(UV) run --group ml python -m scripts.demo security-test

demo-down: ## Tear down the demo stack.
	docker compose down
