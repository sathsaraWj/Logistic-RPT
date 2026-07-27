# Hermes-RPT Implementation Plan

Status: **Draft — planning phase only. No application code has been written yet.**

## 1. Purpose

Hermes-RPT is a secure, multi-tenant Relational Pretrained Transformer platform for Hermes VMS
(Vehicle/Fleet Management System) customers. It learns from each customer's own structured
fleet, logistics and operational data — which may live in differently shaped databases — and
serves predictions (starting with delivery-delay risk) back to that same customer only.

This document is the top-level roadmap. It should be read together with:

* [ARCHITECTURE.md](ARCHITECTURE.md) — system design.
* [THREAT_MODEL.md](THREAT_MODEL.md) — security assumptions and mitigations.
* [MODEL_RESEARCH_PLAN.md](MODEL_RESEARCH_PLAN.md) — modelling strategy.
* [adr/](adr/) — individual architecture decision records.
* [../TASKS.md](../TASKS.md) — phased, checkable task list.

## 2. Non-negotiable invariants

These constraints apply to every phase and every future change. They are restated in the
threat model with concrete mitigations; they are listed here because they should shape
implementation planning, not just security review.

1. `tenant_id` is never accepted as an authoritative value from an editable request body.
2. The tenant is always resolved from a verified authentication token or trusted service
   identity — never from a client-supplied header, query parameter, or form field.
3. Raw database passwords / connection secrets are never returned through any API.
4. Secrets are never written to logs.
5. The model layer never generates or executes unrestricted SQL.
6. Every tenant's customer-database connection uses its own connection pool. Pools are never
   shared across tenants.
7. Caches, datasets, feature batches and prediction requests never mix rows from more than one
   tenant.
8. No model is trained on more than one tenant's data unless an explicit,
   platform-recorded consent (`DataUsageConsent`) authorizes it.
9. All connections into customer databases are read-only.
10. All access to tenant resources is auditable (who, what, when, which tenant, outcome).
11. Missing or inconsistent tenant context results in access denial, not a best-effort guess.
12. When a customer's schema drifts in a way that could invalidate a mapping, the platform fails
    closed (suspends the mapping) rather than continuing to use possibly-wrong assumptions.

## 3. Phased roadmap

Each phase below corresponds to one development prompt. Phases are intentionally sequential —
each depends on artifacts produced by the previous one. A phase is not "done" until its own
tests pass; later phases assume earlier invariants hold.

| Phase | Deliverable | Depends on |
|---|---|---|
| 0 | Plans, architecture, threat model, ADRs, task checklist (this phase) | — |
| 1 | Repository scaffold, tooling, CI, Docker Compose, no business logic | 0 |
| 2 | Control-plane data model, tenant-scoped repositories, RBAC entities | 1 |
| 3 | AuthN/AuthZ, `TenantContext`, scopes, security tests | 2 |
| 4 | Secret provider abstraction, per-tenant read-only DB connections | 2, 3 |
| 5 | Schema discovery, fingerprinting, drift detection | 4 |
| 6 | Canonical Hermes ontology (fleet/workforce/ops/maintenance/cost) | 0 |
| 7 | Schema mapping engine + deterministic suggestion engine | 5, 6 |
| 8 | Safe, tenant-aware feature extraction (no generated SQL) | 7 |
| 9 | Dataset builder, synthetic data generators, data-quality checks | 8 |
| 10 | Baseline tabular models (logreg / GBM / small MLP) + MLflow tracking | 9 |
| 11 | Hermes-RPT v0.1 (Tiny) relational transformer | 9, 10 |
| 12 | Self-supervised relational pretraining objectives | 11 |
| 13 | Inference API (`/v1/predictions/delivery-delay`) | 8, 10 (11 optional) |
| 14 | Model registry, tenant adapters, promotion/rollback | 10, 11, 13 |
| 15 | Monitoring: security, data/schema, model | 3–14 |
| 16 | Dedicated security hardening pass + adversarial tests | all prior |
| 17 | End-to-end demo (Tenant Alpha vs Tenant Beta) | all prior |
| 18 | Final repository audit and release-readiness assessment | all prior |

This plan deliberately puts baseline models (10) before the relational transformer (11) and
puts the inference API (13) on a path that does not hard-depend on the transformer being
finished — the platform must be able to serve a useful prediction from a baseline model alone.
See [ADR-0008](adr/0008-baseline-models-before-relational-transformer.md).

## 4. Smallest viable first prediction task

**Delivery delay risk** (binary/probabilistic: will a given trip's delivery be late relative to
its planned time) is recommended as the first prediction task. Rationale:

* It is present, in some form, in nearly every fleet/logistics dataset (trip + planned time +
  actual/observed time), so it survives heterogeneous customer schemas.
* Its features (vehicle age, driver history, route history, maintenance recency, load, time of
  day) exercise every part of the relational pipeline (multiple related entities, temporal
  windows, categorical + numeric + timestamp features) without requiring exotic data.
* It has a natural point-in-time cutoff (prediction must be made before the trip completes),
  which forces the platform to solve leakage prevention early rather than retrofitting it.
* Business value is easy to explain to a non-ML stakeholder (a manager can act on "this trip is
  high risk of being late").

This choice is recorded as the only entry in the initial `PredictionTaskRegistry`
(see Phase 8) and used consistently through baselines, the relational transformer, pretraining
experiments, the inference API, and the end-to-end demo.

## 5. Technology stack (subject to version pinning in Phase 1)

* Language/runtime: Python 3.12 (or newest supported stable release at Phase 1 time).
* API: FastAPI + Pydantic v2.
* Data access: SQLAlchemy 2.x async ORM, Alembic migrations.
* Control-plane database: PostgreSQL.
* Customer databases: PostgreSQL first; interfaces reserved for MySQL, SQL Server, Oracle.
* ML: PyTorch, scikit-learn / gradient-boosted trees for baselines, MLflow for tracking and
  registry.
* Quality gates: pytest, Ruff, mypy, pre-commit, dependency/security scanning.
* Infra for local dev: Docker Compose (Postgres control-plane, Postgres customer-sim
  databases, MLflow).
* CI: GitHub Actions.

Exact versions are not pinned in this document; Phase 1 records currently-supported stable
versions in `pyproject.toml` / lockfiles and, where a non-obvious choice is made, in an ADR.

## 6. Environments

* **Local development** — Docker Compose, synthetic data only, safe dev identity provider.
* **CI** — ephemeral containers, synthetic data only, full test suite including security tests.
* **Staging/production** — out of scope for implementation in this repository at this stage;
  the architecture is designed to allow it (see ARCHITECTURE.md §8) but no cloud deployment is
  built as part of Phases 0–18.

## 7. What this phase does NOT do

Per the originating prompt, Phase 0 produces plans and repository design only. No source code,
dependency manifests, or infrastructure files are created in this phase. Implementation begins
in Phase 1.

## 8. Documentation checks

Markdown files in this phase were checked for internal consistency (cross-references resolve to
files that exist or are explicitly planned) and for accidental inclusion of secrets or
customer-identifying data — none were introduced. No customer data exists yet at this phase, so
no data-classification check applies. A markdown linter is not yet configured (that is a Phase 1
tooling task); running one retroactively over these docs is tracked in TASKS.md.
