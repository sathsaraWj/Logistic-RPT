# PowerShell task runner, equivalent to the Makefile, for contributors on Windows without
# GNU make installed (this dev machine is one of them). CI and the Makefile remain canonical;
# keep both in sync when adding a task.
#
# Usage: .\tasks.ps1 <task>   e.g. .\tasks.ps1 ci

param(
    [Parameter(Mandatory = $true)][string]$Task
)

$ErrorActionPreference = "Stop"

switch ($Task) {
    "install"          { uv sync }
    "install-ml"       { uv sync --group ml }
    "lint"             { uv run ruff check .; if ($?) { uv run ruff format --check . } }
    "format"           { uv run ruff format .; uv run ruff check --fix . }
    "typecheck"        { uv run mypy src/hermes_rpt }
    "test"             { uv run pytest tests/unit -v }
    "test-unit"        { uv run pytest tests/unit -v }
    "test-integration" { uv run pytest tests/integration -v }
    "test-security"    { uv run pytest tests/security -v }
    "test-model"       { uv run pytest tests/model -v }
    "security-check"   { uv run bandit -c pyproject.toml -r src apps; uv run pip-audit }
    "docs-check"       { uv run python scripts/check_docs.py }
    "ci" {
        uv run ruff check .
        uv run ruff format --check .
        uv run mypy src/hermes_rpt
        uv run pytest tests/unit -v
        uv run python scripts/check_docs.py
    }
    "up"          { docker compose up -d }
    "down"        { docker compose down }
    "logs"        { docker compose logs -f }
    "migrate"     { uv run alembic upgrade head }
    "run-api"     { uv run uvicorn apps.api.main:app --reload }
    "run-worker"  { uv run python -m apps.worker.main }
    "build-synthetic-dataset" { uv run python scripts/build_synthetic_dataset.py }
    "train-baselines"  { uv run python -m apps.trainer.main baselines }
    "train-hermes-rpt" { uv run python -m apps.trainer.main hermes-rpt }
    "pretrain-hermes-rpt" { uv run python -m apps.trainer.main pretrain }
    "adapt-hermes-rpt" { uv run python -m apps.trainer.main adapt }
    default       { throw "Unknown task '$Task'. See tasks.ps1 for the list." }
}
