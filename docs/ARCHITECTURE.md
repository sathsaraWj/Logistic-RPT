# Hermes-RPT Architecture

Status: **Draft — target architecture for the phases described in
[IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md). Nothing described here is implemented yet.**

## 1. Two planes

The system is split into two data planes that must never be conflated:

* **Control plane** — Hermes-owned PostgreSQL database holding tenants, users, roles,
  connection *metadata* (never secrets), schema snapshots, mappings, feature/dataset
  manifests, model registry metadata, predictions, and audit events.
* **Customer plane** — each tenant's own operational database (their fleet/logistics system of
  record), reached only through a read-only, per-tenant connection created at request time from
  a resolved secret reference. Hermes-RPT never stores a copy of a customer's raw operational
  database.

No service is allowed to use a control-plane database session to read/write customer-plane
data or vice versa.

## 2. Service map

```text
                    ┌─────────────────────────┐
                    │        apps/api          │  FastAPI process
                    │  (public + internal HTTP) │
                    └───────────┬───────────────┘
                                │
        ┌───────────────────────┼───────────────────────────┐
        │                       │                            │
┌───────▼────────┐   ┌──────────▼─────────┐      ┌───────────▼───────────┐
│ Identity &      │   │ Tenant service      │      │ Prediction / Inference │
│ Access service  │   │ (tenants, roles,    │      │ service                │
│ (authN/authZ,   │   │  memberships,       │      └───────────┬────────────┘
│  TenantContext) │   │  DataUsageConsent)  │                  │
└───────┬─────────┘   └──────────┬──────────┘                  │
        │                        │                              │
        │              ┌─────────▼─────────┐          ┌─────────▼─────────┐
        │              │ Connector service  │          │ Model registry     │
        │              │ (per-tenant pools) │          │ (MLflow-backed)    │
        │              └─────────┬──────────┘          └─────────┬─────────┘
        │                        │                                │
        │              ┌─────────▼─────────┐          ┌───────────▼───────────┐
        │              │ Secret-provider    │          │ Prediction audit       │
        │              │ abstraction        │          │ service                │
        │              └────────────────────┘          └────────────────────────┘
        │
┌───────▼──────────────────────────────────────────────────────────────────┐
│                          apps/worker (background jobs)                    │
│  Schema discovery • Schema-drift detection • Dataset builder •            │
│  Feature extraction batch jobs • Monitoring/metrics rollups               │
└───────┬─────────────────────────────────────────────────────────────────┬─┘
        │                                                                  │
┌───────▼─────────┐  ┌───────────────────┐  ┌───────────────────┐  ┌───────▼────────┐
│ Schema discovery │  │ Schema mapping     │  │ Feature extraction │  │ Data/model      │
│ service          │  │ service            │  │ service            │  │ monitoring       │
└──────────────────┘  └─────────┬──────────┘  └─────────┬──────────┘  └─────────────────┘
                                 │                        │
                       ┌─────────▼──────────┐   ┌─────────▼──────────┐
                       │ Canonical Hermes    │   │ Dataset builder     │
                       │ ontology            │   └─────────┬──────────┘
                       └─────────────────────┘             │
                                                   ┌─────────▼──────────┐
                                                   │ apps/trainer         │
                                                   │ Baseline models •     │
                                                   │ Relational Transformer│
                                                   │ • Training pipeline   │
                                                   └───────────────────────┘
```

Each named box in the diagram maps to a package under `src/hermes_rpt/` (see Phase 1 layout)
and, where it runs long jobs, to `apps/worker` or `apps/trainer` rather than the request/response
path of `apps/api`.

## 3. Service responsibilities

* **Tenant service** — CRUD for `Tenant`, tenant lifecycle (active/suspended), `DataUsageConsent`
  records. Source of truth for "does this tenant exist and is it active."
* **Identity & access service** — validates authentication tokens, resolves `TenantContext`,
  enforces role/scope-based authorization, issues/validates service-to-service credentials.
* **Customer database connector service** — owns per-tenant connection pools, connection
  lifecycle (create/validate/enable/disable/rotate/delete), enforces read-only transactions,
  statement/connection timeouts, concurrency limits, and schema/table allowlists.
* **Secret-provider abstraction** — resolves a `DatabaseCredentialReference` to a usable
  credential at connection time without ever persisting the resolved secret in the control
  plane or returning it via any API. Local development, and future Azure Key Vault / Google
  Secret Manager adapters, implement the same interface.
* **Schema discovery service** — introspects an allowed customer database (schemas, tables,
  columns, types, keys, indexes, approximate row counts, comments; optional limited profiling)
  and produces a versioned, fingerprinted `SchemaSnapshot`.
* **Schema mapping service** — turns a tenant's `SchemaSnapshot` into a versioned
  `SchemaMapping` against the canonical ontology, through a restricted declarative mapping
  language (no arbitrary SQL/Python), with a lifecycle (draft → pending validation → approved →
  active → deprecated/rejected) and a deterministic mapping-suggestion engine.
* **Canonical Hermes ontology** — the single, customer-agnostic vocabulary (Vehicle, Driver,
  Trip, Delivery, MaintenanceEvent, FuelEvent, …) with canonical units, types and
  classification labels. Never references a specific customer's table/column names.
* **Feature extraction service** — compiles safe, parameterized, allowlisted queries from an
  active mapping + a feature contract, enforcing point-in-time correctness and row/time-window
  limits; never executes model- or user-generated SQL.
* **Dataset builder** — assembles point-in-time-correct, tenant-isolated training datasets from
  extracted features and labels, with manifests, checksums, and lineage; stores generated data
  outside the source repository.
* **Baseline models** — logistic regression / gradient-boosted trees / small MLP trained on the
  same feature contract as the relational transformer, used as the quality floor.
* **Relational Transformer (Hermes-RPT)** — the experimental model that consumes a target
  record plus its related-record context (vehicle/driver/route/maintenance/fuel history) rather
  than a single flat feature vector.
* **Training pipeline** — reproducible, seeded, MLflow-tracked training runs for both baselines
  and the transformer, including tenant-adapter training.
* **Inference service** — serves predictions for an approved task using an authorized model
  version (+ optional tenant adapter), from features extracted at request time.
* **Model registry** — versioned models/adapters with stage/alias (candidate/staging/
  production/archived), tenant ownership metadata for adapters, and compatibility checks
  against ontology/feature versions.
* **Prediction audit service** — records who asked, for which tenant, using which model/adapter/
  mapping/feature versions, and what was returned, for every prediction.
* **Schema-drift detection** — compares new snapshots against prior ones per tenant and flags/
  suspends affected mappings without auto-rewriting them.
* **Data-drift and model-performance monitoring** — tenant-scoped metrics on feature/label
  distributions, calibration, and — once labels arrive — precision/recall.

## 4. Tenant isolation, defense in depth

Isolation is enforced at multiple independent layers so that a bug in any single layer is not
sufficient for cross-tenant access:

1. **AuthN/AuthZ layer** — `TenantContext` is resolved only from verified token claims (or a
   trusted service identity), never from client input. See ADR-0003.
2. **Service layer** — every tenant-owned operation requires a `TenantContext` and rejects any
   request whose path/body resource IDs resolve to a different tenant.
3. **Repository layer** — all tenant-scoped queries are filtered by `tenant_id`; API handlers
   never query ORM models directly (only through repositories/services), so there is one place
   this filter can be forgotten, not many.
4. **Database layer** — PostgreSQL Row-Level Security is used as defense in depth on
   tenant-owned tables, but is never relied on as the sole control (ADR-0003).
5. **Connection layer** — one connection pool per (`tenant_id`, connection id); pools are never
   reused across tenants and are evicted on credential rotation or disablement.
6. **Cache/dataset/feature layer** — every cache key, dataset, and feature batch is namespaced
   by `tenant_id`; nothing is allowed to key only on a business ID.
7. **Model layer** — tenant adapters are loaded only for the requesting tenant; shared base
   models never embed tenant-private adapter weights.

## 5. Request flow: inference (illustrative)

```text
Client → API (authenticate) → resolve TenantContext (from token claims)
      → authorize `prediction:execute`
      → resolve tenant's ACTIVE connection + ACTIVE mapping + feature definition
      → feature extraction (parameterized, allowlisted, point-in-time)
      → resolve authorized model version (+ tenant adapter if any)
      → run inference → generate safe explanation
      → persist prediction lineage → emit audit event → return response
```

Full endpoint contract is defined in Phase 13.

## 6. Background work

Schema discovery, dataset building, training, and monitoring rollups are all background jobs
(`apps/worker`, `apps/trainer`) rather than long-held HTTP requests, so the API stays
responsive and job identity/tenant context can be strongly typed and audited independently of
any HTTP request.

## 7. Cross-cutting concerns

* **Structured logging** with secret redaction, correlation IDs and request IDs (Phase 1).
* **Auditability**: every access to tenant resources, every authz decision, and every
  prediction is recorded as an `AuditEvent`.
* **Fail-closed behavior**: missing/inconsistent tenant context, disallowed schema drift, or an
  incompatible model/ontology/feature version all result in a rejected operation, not a
  best-effort guess.

## 8. Deployment topology (target, not built in Phases 0–18)

`apps/api`, `apps/worker`, and `apps/trainer` are designed as independently deployable
processes sharing the `src/hermes_rpt` library, so that, in a real deployment, training
workloads (potentially GPU-backed) can be scaled and isolated separately from the request-serving
API. Building and operating that deployment (Kubernetes manifests, autoscaling, secrets
infrastructure in a real cloud KMS, etc.) is out of scope for this repository at this stage and
is called out as a production blocker in the eventual release-readiness assessment (Phase 18).

## 9. Unresolved architectural assumptions

See [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) is the roadmap; unresolved assumptions
that affect this architecture are tracked centrally at the bottom of
[../TASKS.md](../TASKS.md) rather than duplicated here, so there is a single list to keep
up to date as phases progress.
