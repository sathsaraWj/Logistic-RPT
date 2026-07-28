# Hermes-RPT

An early, secure, multi-tenant relational machine-learning and transformer research platform
for fleet and logistics data — built to learn from each Hermes VMS customer's own fleet,
logistics and operational data, however differently that data is shaped, while keeping every
tenant's database, mappings, features, models and predictions strictly isolated from every other
tenant's. "Hermes-RPT" (Relational Pretrained Transformer) names this platform's own
from-scratch architecture — it is **not** equivalent to, compatible with, or a replacement for
any third-party "SAP-RPT"-style product.

**Status: not production ready.** [docs/RELEASE_READINESS.md](docs/RELEASE_READINESS.md) carries
the honest, current capability/limitation/production-blocker summary; [TASKS.md](TASKS.md) is
the source of truth for what is and isn't implemented, phase by phase.

## Start here

* [docs/RELEASE_READINESS.md](docs/RELEASE_READINESS.md) — what's actually done, what's
  missing, and what blocks production use, as of Phase 18.
* [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md) — roadmap and invariants.
* [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — system design.
* [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md) — security assumptions and mitigations.
* [docs/SECURITY_REVIEW.md](docs/SECURITY_REVIEW.md) — the Phase 16/17 security audit: findings,
  fixes, and the one Critical item still open.
* [docs/DATA_PROTECTION.md](docs/DATA_PROTECTION.md) — data classification and handling.
* [docs/CUSTOMER_DATABASE_GUIDE.md](docs/CUSTOMER_DATABASE_GUIDE.md) — what connecting a customer
  database actually does and doesn't do.
* [docs/INCIDENT_RESPONSE.md](docs/INCIDENT_RESPONSE.md) — security incident handling.
* [docs/MODEL_RESEARCH_PLAN.md](docs/MODEL_RESEARCH_PLAN.md) — modelling strategy and (§9) the
  Phase 18 retrospective on what was actually found.
* [docs/DEMO.md](docs/DEMO.md) — the Phase 17 two-tenant, real-database, real-HTTP-API
  end-to-end demonstration (`make demo-up` onward).
* [docs/adr/](docs/adr/) — why things are built the way they are.
* [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) — the GCP/Cloud Run deployment and its GitHub Actions
  automation, including what's deliberately out of scope for it.
* [docs/MONITORING.md](docs/MONITORING.md) — metrics, drift detection, and the
  [runbooks](docs/runbooks/) for incidents, schema drift, and model rollback.
* [CHANGELOG.md](CHANGELOG.md) — phase-by-phase history.
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
make lint             # ruff check + format --check
make format            # ruff format + ruff check --fix
make typecheck         # mypy over src/hermes_rpt
make test-unit          # pytest tests/unit — fast, no external services
make test-security       # pytest tests/security — tenant isolation, auth, injection
make test-integration     # pytest tests/integration — requires `make up`
make test-model            # pytest tests/model — requires `make install-ml`
make security-check         # bandit + pip-audit
make docs-check              # verify markdown cross-references and ADR structure
```

`pre-commit install` sets up the same checks (plus basic hygiene hooks) to run on every commit.
On Windows without `make`, every target above has a `./tasks.ps1 <target>` equivalent.

## Two-tenant end-to-end demo

A full local walkthrough — real, separate PostgreSQL databases for two synthetic tenants with
deliberately different schemas, discovery, mapping, training, prediction with lineage, and a
security-test step (schema drift, tenant isolation, log safety). See
[docs/DEMO.md](docs/DEMO.md) for the full walkthrough and expected output.

```bash
make demo-up && make demo-seed && make demo-discover && make demo-map \
  && make demo-train && make demo-predict && make demo-security-test
make demo-down   # tear down when finished
```

## License

Apache License 2.0 — see [LICENSE](LICENSE) and
[docs/adr/0009-apache-2-0-license.md](docs/adr/0009-apache-2-0-license.md).
