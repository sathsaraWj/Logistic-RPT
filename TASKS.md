# Hermes-RPT Task Checklist

This file tracks phased progress. Each phase corresponds to a development prompt described in
`prompts.txt` and planned in `docs/IMPLEMENTATION_PLAN.md`. Checked items are done and tested;
unchecked items are planned, not implemented.

## Phase 0 — Master project plan

- [x] `docs/IMPLEMENTATION_PLAN.md`
- [x] `docs/ARCHITECTURE.md`
- [x] `docs/THREAT_MODEL.md`
- [x] `docs/MODEL_RESEARCH_PLAN.md`
- [x] ADRs: 0001 (ADR process), 0002 (monorepo), 0003 (tenant isolation defense in depth),
      0004 (per-tenant read-only pools), 0005 (secret-provider abstraction), 0006 (ontology
      decoupled from customer schemas), 0007 (deterministic mapping before LLM), 0008
      (baselines before relational transformer)
- [x] This file (`TASKS.md`)
- [ ] Proposed repository structure shown to user (see below)
- [ ] Unresolved architectural assumptions listed (see below)
- [ ] Smallest viable first prediction task recommended (delivery-delay risk — see
      `docs/IMPLEMENTATION_PLAN.md` §4)

No application code, dependency manifests, or infrastructure files exist yet. That begins in
Phase 1.

## Phase 1 — Initialise the repository

- [x] Monorepo scaffold (`apps/`, `src/hermes_rpt/`, `tests/`, `configs/`, `migrations/`,
      `notebooks/`, `scripts/`, `docker/`)
- [x] `pyproject.toml` with currently-current stable dependency versions recorded (uv-managed,
      pinned to Python 3.12.13 via `.python-version`; heavy `ml` group deferred to Phase 10)
- [x] Typed application settings + startup env-var validation (`hermes_rpt.common.settings`)
- [x] Structured logging with secret redaction, correlation/request IDs
      (`hermes_rpt.common.logging`, `hermes_rpt.common.correlation`)
- [x] `/health/live`, `/health/ready`, `/version` (`apps/api/main.py`)
- [x] `docker-compose.yml` (control-plane Postgres, MLflow); syntax/config validated via
      `docker compose config` — **not** exercised with a real `docker compose up` in this
      session because the local Docker daemon was not running; do this before relying on it
- [x] Pre-commit hooks (installed, hooks run — no tracked files to check yet since nothing is
      committed); lint/type-check/test/security-check commands; `Makefile` + `tasks.ps1`
      (Windows-without-`make` equivalent)
- [x] GitHub Actions CI workflow (`.github/workflows/ci.yml`)
- [x] Development `README.md`
- [x] ADR recording the licence decision (`docs/adr/0009-apache-2-0-license.md`); `LICENSE`
      (Apache-2.0) already existed at repo init and is now backed by that ADR
- [x] Smoke tests; formatting/lint/type-check/unit-test/docs-check/security-check/
      Docker-Compose-config runs all green (`make ci`, `make security-check`, `make docs-check`)

## Phase 2 — Multi-tenant control plane

- [x] Entities: Tenant, User, Role, UserTenantMembership, CustomerDatabaseConnection,
      DatabaseCredentialReference, SchemaSnapshot, SchemaMapping, MappingVersion,
      DataAccessPolicy, PredictionTaskDefinition, PredictionRequest, PredictionResult,
      AuditEvent, ModelVersion, TenantModelAdapter, DataUsageConsent — all UUID-keyed
      (`src/hermes_rpt/{tenants,connectors,schemas,mappings,inference,registry,audit}/models.py`)
- [x] Alembic migrations — hand-authored (no live Postgres available in this session; see
      migration docstring) and validated via `alembic upgrade head --sql` /
      `alembic downgrade head:base --sql` offline SQL rendering for both directions.
      **Must still be applied to a real PostgreSQL instance and cross-checked with a fresh
      `--autogenerate` diff before being considered final** — tracked below.
- [x] Repository + service layers (`hermes_rpt.common.repository`, per-context
      `repository.py`/`service.py`); no API handlers exist yet in this phase to violate the
      "never query ORM models directly" rule — the rule is enforced by there being exactly one
      way in (repositories), ready for Phase 3+ handlers to use
- [x] Roles: Platform Admin, Tenant Admin, Data Steward, ML Engineer, Manager, Operator,
      Read-only Auditor, Service Account (`hermes_rpt.tenants.enums.RoleName`, seeded via
      migration `96b4009ff8d0`)
- [x] Tenant-scoped uniqueness constraints and indexes (e.g. membership role uniqueness,
      schema-snapshot sequence numbers, mapping version numbers, prediction idempotency keys —
      all scoped by `tenant_id`)
- [x] PostgreSQL RLS as defense in depth (ADR-0003) — migration `91b14d4e87dc`, `FORCE ROW
      LEVEL SECURITY` on every tenant-owned table, nullable-tenant tables (`model_versions`)
      get an "own or shared" policy, `audit_events` gets the strict policy despite being
      nullable (platform events must stay invisible to tenant-scoped sessions)
- [x] `TenantContext` abstraction (`hermes_rpt.tenants.context`) — frozen dataclass, never
      constructed from client input outside Phase 3's future auth layer
- [x] Test factories (`tests/factories.py`) + negative cross-tenant-access security tests
      (`tests/security/test_tenant_isolation.py`, 9 tests, all passing) covering: get, require,
      list, add-with-forged-tenant-id, delete, prediction requests, shared vs. private model
      visibility, and audit log isolation (including platform-level event exclusion)
- [ ] **Follow-up before this phase is truly done**: run `make up && make migrate` against a
      real PostgreSQL instance once Docker is available in this environment, then
      `tests/integration/test_row_level_security.py` (currently skips — no reachable DB) to
      prove RLS itself, not just the application-layer control, behaves as intended

## Phase 3 — Authentication and authorisation

- [x] JWT validation: signature, issuer, audience, expiry, required claims
      (`hermes_rpt.auth.verifier.LocalDevTokenVerifier`, PyJWT only — no custom crypto)
- [x] `TenantContext` resolution from verified claims only
      (`hermes_rpt.auth.dependencies.get_tenant_context`); `X-Tenant-Id`-style headers are
      never read anywhere, proven by `test_tenant_id_header_is_ignored`
- [x] Role- and permission-based authorization: `require_roles`, `require_scopes`
      (`hermes_rpt.auth.dependencies`); scopes per prompts.txt (`hermes_rpt.auth.enums.ScopeName`)
- [x] Service-to-service auth: `principal_type` claim, `require_human`/`require_service`
      dependencies; short-lived credentials via `access_token_ttl_seconds` /
      `service_token_ttl_seconds` and `hermes_rpt.auth.dev_tokens.issue_dev_token` (refuses to
      run outside local/CI)
- [x] Audit events for authn/authz failures (`apps/api/exception_handlers.py`, best-effort —
      never turns an auth failure into a 500 if the audit write itself fails); rate-limit
      interface (`hermes_rpt.auth.rate_limit.RateLimiter` +
      `InMemoryFixedWindowRateLimiter`, explicitly non-distributed)
- [x] Safe error responses — fixed generic `detail` strings only, `reason` codes stay
      server-side (`hermes_rpt.auth.errors`), proven by
      `test_authentication_failure_does_not_leak_internal_detail`
- [x] Security tests (`tests/security/test_authentication.py`,
      `tests/security/test_authorization.py`, 14 tests, all passing): missing/garbage/expired
      token, invalid issuer/audience, missing tenant claim, wrong signing secret, missing
      scope, tenant/resource mismatch (404 not 403), service-token-as-human-token,
      cross-tenant object access, tenant-header-ignored
- [x] First real auth-protected routes (`apps/api/routers/memberships.py`) built on the
      Phase 2 `MembershipService`, giving the above tests a concrete API surface
- [x] `docs/AUTHENTICATION.md` (flow + trust boundaries) and
      `docs/adr/0010-jwt-local-dev-provider.md`
- [x] Fixed a real Phase 1 bug surfaced by writing these tests: `configure_logging` used
      `structlog.stdlib.add_logger_name`, which assumes a stdlib logger and crashed the very
      first log call once a route actually ran through `lifespan` startup (earlier tests never
      exercised `lifespan`, so this went uncaught) — replaced with the non-stdlib
      `structlog.processors.add_log_level` equivalent; added
      `tests/unit/test_logging_config.py` as a regression test
- [ ] **Not built yet**: real external OIDC/JWKS provider integration (local HS256 dev
      provider only, by design — see ADR-0010); token revocation / `jti` denylist

## Phase 4 — Secret management and database connections

- [x] `SecretProvider` interface + `LocalDevSecretProvider` (in-memory, dev/test only);
      `AzureKeyVaultSecretProvider` / `GoogleSecretManagerSecretProvider` interface reservations
      that raise `NotImplementedError` (`hermes_rpt.secrets.provider`)
- [x] `DatabaseConnector` interface + `PostgresConnector` (asyncpg, `default_transaction_
      read_only=on` session setting, connect/statement timeouts, small bounded pool, TLS mode
      handling); `MySQLConnector`/`MSSQLConnector`/`OracleConnector` interface reservations
      (`hermes_rpt.connectors.interfaces`, `hermes_rpt.connectors.postgres`)
- [x] Per-tenant connection registry keyed by `(tenant_id, connection_id)`
      (`hermes_rpt.connectors.pool_registry.TenantConnectionPoolRegistry`), health-check service
      (`hermes_rpt.connectors.health`), lifecycle manager tying repositories + secret provider +
      pool registry + audit together (`hermes_rpt.connectors.service.ConnectionLifecycleManager`)
- [x] Query guard: statement-shape check (read-only) + schema/table allowlist check, fail-closed
      on empty allowlists (`hermes_rpt.connectors.query_guard`)
- [x] Connection APIs: register/validate/enable/disable/rotate-secret/status(get+list)/delete,
      all behind `connection:manage` scope (`apps/api/routers/connections.py`)
- [x] Two simulated customer databases in `docker-compose.yml`
      (`tenant-alpha-db`/`tenant-beta-db`, separate Postgres instances with separate
      users/passwords — not just separate databases on one instance), seeded with intentionally
      different schemas (`docker/postgres-fixtures/{alpha,beta}/init.sql`) that foreshadow the
      Phase 17 demo's `fleet_vehicle` vs `assets` naming
- [x] Fast tests (no real DB, `FakeConnector`): `tests/unit/test_secret_provider.py`,
      `tests/unit/test_query_guard.py`, `tests/unit/test_pool_registry.py`,
      `tests/security/test_connector_isolation.py` — 23 tests covering pool isolation
      (including "same connection_id, different tenant_id" specifically), no-secret-exposure,
      rotation invalidating the old reference, audit events, and log cleanliness
- [x] Real-database integration tests (`tests/integration/test_customer_db_connections.py`,
      skips without `make up`): validate against real Alpha/Beta Postgres, write rejected at
      the database level (`default_transaction_read_only`), tenant-keyed pool lookup holds for
      real engines too, logs contain no passwords against the real driver
- [ ] **Follow-up**: run `make up && uv run pytest tests/integration` once Docker is available
      in this environment to actually execute (not just validate the design of) the real-DB
      tests above

## Phase 5 — Schema discovery and fingerprinting

- [x] Metadata-only discovery via `information_schema`/`pg_catalog` (schemas, tables, views,
      columns, types, nullability, PKs, FKs, unique constraints, indexes, approximate row
      counts via `pg_stat_user_tables`, table/column comments) — fails closed on an empty
      `schema_allowlist`, same policy as `hermes_rpt.connectors.query_guard`
      (`hermes_rpt.schemas.introspection.PostgresSchemaIntrospector`)
- [x] Optional profiling, off by default: small bounded samples, credential columns never
      profiled at all (name-pattern denylist + configurable extra patterns), likely-personal
      fields profiled as aggregate stats only (never sample values), profiling summaries
      (not raw rows) stored, every profiling run audited (`hermes_rpt.schemas.profiling`)
- [x] Versioned, fingerprinted `SchemaSnapshot` — deterministic SHA-256 fingerprints for
      columns, tables (incl. relationships), and the whole schema, order-independent
      (`hermes_rpt.schemas.fingerprint`); drift-tracking columns added via migration
      `7f7e83e4e8e7`
- [x] Drift comparison: added/removed/likely-renamed tables (column-similarity heuristic,
      never auto-approved — flagged, not merged into "no change"), added/removed columns,
      type changes, nullability changes, relationship changes
      (`hermes_rpt.schemas.drift.compare_snapshots`)
- [x] Discovery runs as a background task (Phase 5 requirement: HTTP requests must not stay
      open for long-running work) — `POST .../discovery` creates a `PENDING` snapshot and
      returns immediately (202), `hermes_rpt.schemas.jobs.run_discovery_job` does the actual
      work via `asyncio.create_task`. **Known limitation**: in-process only, not a durable
      queue — a process restart mid-run leaves a snapshot stuck `RUNNING`; a real deployment
      should run this in `apps/worker` against a durable queue instead (not built yet)
- [x] APIs: start/list/get/compare/acknowledge-drift, all behind `schema:discover` scope
      (`apps/api/routers/schema_discovery.py`)
- [x] Two simulated customer databases already had different schemas from Phase 4
      (`fleet_vehicle` vs `assets`); Phase 5's real-DB test proves discovery captures both
      correctly and produces different fingerprints
- [x] Tests: 25 fast unit/security tests (fingerprint determinism/order-independence/
      sensitivity, every drift event type incl. the rename heuristic and its false-positive
      guard, credential/personal-field classification, fail-closed empty allowlist, status
      transitions, tenant isolation of snapshots, safe error summaries, audit events) +
      1 real-database integration test (skips without `make up`)

## Phase 6 — Canonical Hermes ontology

- [x] All 28 entities across Fleet(5)/Workforce(5)/Operations(7)/Maintenance(7)/Cost(4) per
      prompts.txt, defined once in `configs/ontology/v1.yaml` — the single source of truth;
      Pydantic runtime models are generated from it (`hermes_rpt.ontology.registry`), never
      hand-duplicated
- [x] Canonical units/types/semantics: distance always km, volume always litres, mass always
      kilograms (`hermes_rpt.ontology.units`, with a real conversion layer: miles/US+UK
      gallons/pounds); money always carries its currency code (`Money`); datetimes always a
      `{utc, original_timezone}` value (`CanonicalDatetime`); business identifiers always
      string, enforced by validation, not convention (`hermes_rpt.ontology.values`,
      `hermes_rpt.ontology.schema`)
- [x] Ontology versioning (`OntologyDefinition.version`, currently `"1"`), unit conversion
      layer, `DataClassification` labels (public/internal/pii/sensitive_pii) on fields (e.g.
      `Driver.license_number` is `sensitive_pii`), required/optional field metadata (missing
      values always explicit `None`, never a sentinel), relationship definitions with
      cardinality, and structural + semantic validation rules (enum needs values, unit only on
      float, business identifiers must be string, no duplicate/dangling entity or field names)
- [x] No customer table/column names anywhere in the ontology (tested directly);
      `docs/ONTOLOGY.md` documents the whole system plus a worked example mapping Tenant
      Alpha's `fleet_vehicle` and Tenant Beta's `assets` (Phase 4/5 fixtures) onto the same
      canonical `Vehicle`
- [x] 38 new tests (unit conversion round-trips, value-object validation, loader structural/
      semantic validation incl. 7 deliberately-broken YAML cases, dynamically-built model
      behaviour for required/optional/enum/money/datetime fields, and a full-ontology
      relationship-resolvability sweep) — 148 total tests passing

## Phase 7 — Schema mapping

- [x] Declarative mapping language: direct column mapping, implicit type conversion (target
      field's `logical_type`), unit conversion (`source_unit` + `hermes_rpt.ontology.units.
      convert_to_canonical`), static values, null handling (`null_values`/`default`),
      enumerated-value mapping (`value_map`), filters (`FilterCondition`, structured, never a
      SQL fragment), joins through approved relationships only (`JoinDefinition`, validated
      against the ontology entity's declared relationships + the connection's allowlist),
      safe derived-field expressions (`hermes_rpt.mappings.expressions` — closed set of
      structured node types, no `eval`/parsing), timestamp transformation (`source_timezone`),
      source-priority rules (`FieldMapping.sources`, ordered, first non-null wins) — no
      arbitrary Python or unrestricted SQL anywhere (`hermes_rpt.mappings.document`)
- [x] Mapping lifecycle: draft -> pending_validation -> approved -> active ->
      deprecated/rejected (`hermes_rpt.mappings.service.MappingService`); immutable after
      activation (a `MappingVersion` pointed to by `active_version_id` is never mutated again;
      changes always insert a new version via `create_new_version`, which does not touch the
      currently-active version or its production usability); only an approved, active,
      non-drift-suspended version is ever returned by `get_production_version`, the single
      method Phase 8 is meant to call
- [x] Drift-triggered suspension (`suspend_affected_by_drift`): flips `suspended_due_to_drift`
      on affected active mappings without ever changing `state` or rewriting the document —
      "cannot automatically rewrite" holds structurally, not just by convention
- [x] Confidence/explanation fields (already on `MappingVersion` since Phase 2) populated by
      the suggestion engine; human approval (`MappingService.approve`, gated behind
      `tenant:admin` scope at the API layer) is required for every mapping regardless of
      `is_ai_suggested` — a strictly stronger rule than "only AI-suggested ones need approval"
- [x] Deterministic mapping-suggestion engine (ADR-0007): normalized column-name similarity
      (averaged character- and token-level signals — an earlier max()-based version was
      caught by testing to occasionally prefer a wrong-but-noisily-similar field name over the
      right one), data-type compatibility (including "integer source column -> STRING
      identifier field," the realistic common case), primary-key boost, table/column comment
      boost, safe profile-statistic boost; global greedy (field, column) assignment so a weak
      match for one field can't steal a column a different field would have matched strongly;
      no external LLM call (`hermes_rpt.mappings.suggest`)
- [x] `docs/SCHEMA_MAPPING.md`
- [x] 39 new tests using Tenant Alpha's (`fleet_vehicle`) and Tenant Beta's (`assets`) schemas
      specifically: expression evaluation, document/validation structural+semantic checks,
      suggestion-engine correctness on both schemas (incl. the primary-key-over-name-match
      case), and full lifecycle + immutability + drift-suspension + tenant-isolation tests
      exercised against both — 200 total tests passing
- [ ] **Not built yet**: compiling an active mapping into an executable, allowlist-checked
      query is Phase 8's "safe query compiler," not this phase

## Phase 8 — Safe feature extraction

- [ ] `PredictionTaskRegistry` with delivery-delay-risk task
- [ ] Explicit feature contract with leakage-risk/availability-timestamp metadata per feature
- [ ] Tenant-aware extraction planner, mapping resolver, safe query compiler/validator,
      query-cost guard, normaliser, lineage record, batch format
- [ ] Point-in-time correctness tests; dry-run mode; Alpha/Beta integration tests

## Phase 9 — Dataset building and data quality

- [ ] Dataset definition/build job/manifest/checksum/lineage; temporal train/val/test splits
- [ ] Data-quality report, feature/label statistics, leakage checks
- [ ] Synthetic Alpha/Beta generators (different schemas, missing values, imbalance, drift,
      delays, histories); `make build-synthetic-dataset`
- [ ] Quality checks: duplicate IDs, invalid dates, negative distances, impossible fuel
      quantities, unrecognised statuses, missing labels, imbalance, future timestamps,
      cross-tenant contamination

## Phase 10 — Baseline models

- [ ] Logistic regression, gradient-boosted trees, small MLP behind a common interface
- [ ] MLflow tracking, artifact registration, model signature, evaluation report
- [ ] Metrics: ROC-AUC, PR-AUC, precision/recall/F1, Brier score, calibration, confusion matrix
- [ ] Registry stages: Candidate/Staging/Production/Archived; no auto-promotion
- [ ] Reproducibility, serialization, schema-validation, missing-feature, registry, and tenant
      model-access isolation tests
- [ ] Baseline comparison report

## Phase 11 — Hermes-RPT v0.1 (Tiny)

- [ ] Encoders (numeric/categorical/timestamp), entity/table/column embeddings, missing-value
      embeddings, record encoder, relationship embeddings, transformer blocks, attention
      masks, target pooling, classification head
- [ ] Explicit relationship representation; capped, variable-size relational context; masks
      over fake values
- [ ] Shape/masking tests, tiny-overfit test, numerical-stability checks, checkpoint tests
- [ ] Comparison against strongest baseline; honest reporting
- [ ] Tiny/Small/Base-experimental configs defined; only Tiny trained
- [ ] `docs/HERMES_RPT_0_1.md`

## Phase 12 — Relational pretraining

- [ ] Configurable self-supervised objectives per prompts.txt
- [ ] Tenant-boundary-preserving, relation-structure-preserving collator
- [ ] Multi-task loss; memorisation-risk canary tests; pretraining lineage
- [ ] Pretrained vs. non-pretrained Tiny comparison; honest experimental report

## Phase 13 — Inference API

- [ ] `POST /v1/predictions/delivery-delay` per the flow in prompts.txt
- [ ] No raw SQL/passwords/connection strings/other-tenant identifiers/unsupported causal
      claims in responses
- [ ] Idempotency, timeout/retry/circuit-breaker interfaces
- [ ] Security, integration, load, contract tests

## Phase 14 — Model registry and per-tenant adaptation

- [ ] Shared base + tenant adapters; aliases; approval workflow; rollback; deactivation;
      compatibility checks
- [ ] Tenant-owned adapters, tenant-specific checkpoint paths, tenant-isolated adapter lookup
- [ ] Alpha/Beta adapter experiment; rollback tested

## Phase 15 — Monitoring and drift detection

- [ ] Security, data/schema, and model monitoring per prompts.txt; tenant-scoped metrics
- [ ] No high-cardinality raw-ID labels; redaction in logs; internal-only tenant-identifying
      alerts
- [ ] Incident, schema-drift, and model-rollback runbooks
- [ ] Metric isolation and redaction tests

## Phase 16 — Security hardening

- [ ] Full repository security review across the areas listed in prompts.txt
- [ ] Adversarial test suite per prompts.txt
- [ ] `docs/SECURITY_REVIEW.md`, `docs/DATA_PROTECTION.md`, `docs/INCIDENT_RESPONSE.md`,
      `docs/CUSTOMER_DATABASE_GUIDE.md`
- [ ] No critical/high findings left open

## Phase 17 — End-to-end demonstration

- [ ] Alpha/Beta demo tenants with distinct schemas per prompts.txt
- [ ] `make demo-{up,seed,discover,map,train,predict,security-test,down}`
- [ ] `docs/DEMO.md`; full run demonstrating isolation, drift handling, and safe logging

## Phase 18 — Final repository audit

- [ ] Full audit per prompts.txt checklist; all available checks run
- [ ] Updated `README.md`, `TASKS.md`, `docs/ARCHITECTURE.md`, `docs/SECURITY_REVIEW.md`,
      `docs/MODEL_RESEARCH_PLAN.md`, `CHANGELOG.md`
- [ ] `docs/RELEASE_READINESS.md`
- [ ] Accurate positioning ("early, secure, multi-tenant relational ML/transformer research
      platform for fleet and logistics data" — not equivalent to any third-party product)

---

## Proposed repository structure (target, built up over Phases 1–18)

```text
hermes-rpt/
├── apps/
│   ├── api/
│   ├── worker/
│   └── trainer/
├── src/hermes_rpt/
│   ├── auth/
│   ├── tenants/
│   ├── connectors/
│   ├── secrets/
│   ├── schemas/
│   ├── mappings/
│   ├── ontology/
│   ├── features/
│   ├── datasets/
│   ├── models/
│   ├── training/
│   ├── inference/
│   ├── registry/
│   ├── audit/
│   ├── monitoring/
│   └── common/
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── security/
│   └── model/
├── configs/
├── migrations/
├── notebooks/
├── scripts/
├── docs/
│   └── adr/
├── docker/
├── pyproject.toml
├── docker-compose.yml
├── Makefile
├── .env.example
├── .gitignore
├── README.md
└── TASKS.md
```

Only `docs/`, `LICENSE`, and this file exist today. Everything else above is created starting
in Phase 1.

## Unresolved architectural assumptions

These need a decision (from the user, a future design partner, or a later phase) before they
can be closed out; they are tracked here rather than left implicit:

1. **Deployment target.** No cloud provider, Kubernetes setup, or managed Postgres/secret-store
   is chosen yet. Phases 0–18 assume local Docker Compose only.
2. **Identity provider.** Phase 3 assumes a to-be-named external OIDC provider eventually, with
   a safe local dev provider standing in until then; which real provider (Auth0, Entra ID,
   Keycloak, etc.) is not decided.
3. **Real customer/design-partner data.** All phases through 18 use synthetic Alpha/Beta data
   only; there is no committed plan yet for how/when real customer data would be onboarded,
   which affects how much confidence to place in Phase 11/12 model results.
4. **Row-Level Security engine specifics.** ADR-0003 commits to RLS as defense in depth but the
   exact policy design (session variable vs. current_setting-based tenant binding) is a Phase 2
   implementation detail, not decided here.
5. **Scale targets.** No target tenant count, request volume, or data volume has been specified,
   which affects connection-pool sizing (ADR-0004) and query-cost limits (Phase 8) — current
   plan assumes "moderate number of tenants, not high-frequency trading-style QPS."
6. **MySQL/SQL Server/Oracle connectors.** Interfaces are reserved (Phase 4) but no timeline is
   set for making them functional.
7. **LLM-assisted mapping suggestions.** ADR-0007 defers this deliberately; no phase in the
   current plan (0–18) implements it — it's future work the plumbing is prepared for.
8. **Licensing.** `LICENSE` (Apache-2.0) already exists in the repo; Phase 1 needs to add the
   ADR that formally records this choice rather than treating it as an open question, but the
   choice itself is not re-litigated here.
9. **Hosting of generated artifacts.** Datasets/models are specified as "stored outside the
   source repository" but no concrete artifact store (S3-compatible, filesystem, MLflow
   artifact store backend) is chosen yet — left as a Phase 1/9/10 implementation decision.
