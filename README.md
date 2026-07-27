# Hermes-RPT

Hermes-RPT is a secure, multi-tenant Relational Pretrained Transformer platform for Hermes VMS
customers, built to learn from each customer's own fleet, logistics and operational data —
however differently that data is shaped — while keeping every tenant's database, mappings,
features, models and predictions strictly isolated from every other tenant's.

**Status: early research/engineering platform, under active phased build-out. Not production
ready.** `docs/RELEASE_READINESS.md`, added in Phase 18, will carry an honest capability/
limitation summary; until then, [TASKS.md](TASKS.md) is the source of truth for what is and
isn't implemented.

## Start here

* [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md) — roadmap and invariants.
* [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — system design.
* [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md) — security assumptions and mitigations.
* [docs/MODEL_RESEARCH_PLAN.md](docs/MODEL_RESEARCH_PLAN.md) — modelling strategy.
* [docs/adr/](docs/adr/) — why things are built the way they are.
* [TASKS.md](TASKS.md) — phased, checkable progress.

## Requirements

* [uv](https://docs.astral.sh/uv/) (manages the Python 3.12 interpreter and virtualenv — no
  separate Python install needed, `uv python install 3.12` handles it).
* [Docker](https://www.docker.com/) + Docker Compose v2, for local Postgres/MLflow.
* GNU Make, if you want to use the `Makefile` — on Windows without `make`, use the equivalent
  `tasks.ps1` (`./tasks.ps1 <task>`) instead; both call the same underlying `uv run …` commands.

## Getting started

```bash
cp .env.example .env          # review values; defaults work for local dev
make install                  # uv sync — fast, excludes torch/mlflow/scikit-learn
make up                       # start Postgres (control-plane) + MLflow via Docker Compose
make ci                       # lint + typecheck + unit tests + docs-check
make run-api                  # http://localhost:8000 — see /health/live, /health/ready, /version
```

On Windows without `make`: substitute `./tasks.ps1 install`, `./tasks.ps1 up`,
`./tasks.ps1 ci`, `./tasks.ps1 run-api`.

The heavy ML stack (PyTorch, MLflow client, scikit-learn) is a separate dependency group,
installed only when you actually need it (Phase 10 onward):

```bash
make install-ml                # or: uv sync --group ml
```

## Repository layout

```text
apps/            deployable entry points (api, worker, trainer) — thin, no business logic
src/hermes_rpt/  the actual platform, organized by bounded context (see ARCHITECTURE.md)
tests/           unit / integration / security / model test suites
configs/         non-secret configuration (ontology defaults, etc. — added Phase 6+)
migrations/      Alembic migrations for the control-plane database
docs/            plans, architecture, threat model, ADRs
docker/          Dockerfiles for local dev services
scripts/         maintenance scripts (e.g. scripts/check_docs.py)
```

## Security ground rules (see docs/THREAT_MODEL.md for the full model)

* Customer database connections are always read-only, one connection pool per tenant.
* The tenant is always resolved from a verified auth token, never from client-supplied input.
* Secrets are never logged and never returned by any API.
* No unrestricted, model- or user-generated SQL is ever executed.

## Contributing / local checks

```bash
make lint          # ruff check + format --check
make format         # ruff format + ruff check --fix
make typecheck      # mypy over src/hermes_rpt
make test-unit       # pytest tests/unit
make security-check  # bandit + pip-audit
make docs-check      # verify markdown cross-references and ADR structure
```

`pre-commit install` sets up the same checks (plus basic hygiene hooks) to run on every commit.

## License

Apache License 2.0 — see [LICENSE](LICENSE) and
[docs/adr/0009-apache-2-0-license.md](docs/adr/0009-apache-2-0-license.md).
