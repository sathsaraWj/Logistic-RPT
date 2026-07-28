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

- [x] `PredictionTaskRegistry`-equivalent with the delivery-delay-risk task:
      `hermes_rpt.features.contract.DELIVERY_DELAY_RISK_CONTRACT` (14 features, target entity
      `Trip`), keyed by `task_key` in `apps/api/routers/features.py`'s `_CONTRACTS` dict for
      future tasks to register into.
- [x] Explicit feature contract with leakage-risk/availability-timestamp metadata per feature:
      `hermes_rpt.features.contract.FeatureSpec` (`leakage_risk`, `leakage_note`,
      `availability_timestamp`, `historical_window_days`, `missing_value_behavior`,
      `required`). Two features (`driver_late_delivery_ratio`, `planned_delivery_count`) are
      documented single-hop proxies for what would naturally need a multi-hop join — see
      docs/FEATURE_EXTRACTION.md §2.
- [x] Tenant-aware extraction planner (`hermes_rpt.features.planner.build_query_plan`, dry-run,
      never opens a connection), mapping resolver (`hermes_rpt.features.resolver.
      MappingResolver` — fails closed for the target entity, returns `None` for an unmapped
      related entity rather than raising), safe query compiler
      (`hermes_rpt.features.compiler` — parameterized SQLAlchemy Core only, allowlist-checked
      via `hermes_rpt.connectors.query_guard`, one fixed recipe per `FeatureKind`), query-cost
      guard (`hermes_rpt.features.cost_guard` — 365-day window cap, 500-row related-query cap),
      normaliser (`hermes_rpt.features.normalizer` — `data_type` coercion +
      `missing_value_behavior`), lineage record (`hermes_rpt.features.lineage.
      FeatureLineageRecord` — tenant, task key, contract version, target/related mapping
      *version* ids, schema snapshot id, missing-feature reasons), batch format
      (`hermes_rpt.features.lineage.FeatureBatch`). Orchestrated by
      `hermes_rpt.features.service.FeatureExtractionService` (`plan()` / `extract()`).
- [x] Point-in-time correctness tests (`tests/unit/test_feature_compiler.py`,
      `test_feature_derive.py` — every `FeatureKind`'s `< prediction_time` cutoff and window
      lower bound, plus `loading_start_delay_minutes`'s independent zero-if-future guard);
      dry-run mode (`test_dry_run_plan_never_touches_the_database`); Alpha/Beta integration
      tests (`tests/security/test_feature_extraction_service.py` — full pipeline against two
      differently-named synthetic schemas via `tests.fakes.SQLiteConnector`, plus missing-related-
      mapping-is-not-an-error and fail-closed-on-missing-target-mapping cases). 39 new tests.
- [x] `GET /v1/features/{task_key}/plan` and `POST /v1/features/{task_key}/extract`
      (`apps/api/routers/features.py`, behind `prediction:execute`) — the extraction layer is
      independently reachable/auditable; the real prediction-serving endpoint is Phase 13.
- [x] `docs/FEATURE_EXTRACTION.md`

Follow-up / known gaps (see docs/FEATURE_EXTRACTION.md §8): no multi-hop join support yet;
window/row-limit constants are platform-wide, not per-tenant configurable; no real-Postgres
integration test for this phase specifically (Docker unavailable in this environment — same
skip-gated pattern as earlier phases; the SQLite end-to-end tests exercise the full pipeline's
logic, just not the Postgres driver).

## Phase 9 — Dataset building and data quality

- [x] Dataset definition (`hermes_rpt.datasets.definition.DatasetDefinition` — task, tenant
      (or explicit shared-research-dataset flag), time range, `LabelDefinition`,
      `TemporalSplitStrategy`), build job (`hermes_rpt.datasets.builder.DatasetBuildService`),
      manifest (`hermes_rpt.datasets.manifest.DatasetManifest` — metadata/counts/checksum only,
      no raw values), checksum (`compute_dataset_checksum`, deterministic SHA-256 over feature
      values + labels in row order — reproducibility verified by test), lineage
      (`DatasetLineage` — ontology version, feature-contract version, and the exact
      `MappingVersion`/`SchemaSnapshot` ids used, not just "current"); temporal train/val/test
      splits (`hermes_rpt.datasets.split.split_temporally` — no shuffling, cut by fraction on
      already-time-ordered rows).
- [x] Data-quality report (`hermes_rpt.datasets.quality.DataQualityReport`), feature/label
      statistics (`hermes_rpt.datasets.statistics` — mean/std/min/max, missing counts, class
      balance), leakage checks (point-in-time validation at the label level — see
      docs/DATASET_BUILDING.md §2 — layered on top of Phase 8's feature-level `< prediction_time`
      cutoffs; a row with a self-contradictory timestamp is excluded, not trusted, and never
      crashes the build).
- [x] Synthetic Alpha/Beta generators (`hermes_rpt.synthetic` — deterministic/seeded, all seven
      entities the delivery-delay-risk contract touches, two differently-named table/column
      schemas per docs/DATASET_BUILDING.md §6, realistic missing values, class imbalance
      (~85-90% on-time), vehicle/route histories, trips near the time-range boundary deliberately
      left unresolved ("avoid placing future events into earlier training rows")); a small fixed
      set of deliberately injected defects (`_inject_defects`) for the quality checks to catch;
      `make build-synthetic-dataset` / `.\tasks.ps1 build-synthetic-dataset`
      (`scripts/build_synthetic_dataset.py`) — run and produced a real sample report (see
      docs/DATASET_BUILDING.md §7).
- [x] Quality checks: duplicate IDs, invalid dates, negative distances, unrecognised statuses,
      missing labels, severe class imbalance, future timestamps, cross-tenant contamination —
      all implemented in `hermes_rpt.datasets.quality` and exercised against real injected
      defects in both the unit suite and the Alpha/Beta end-to-end integration test. "Impossible
      fuel quantities" is generated by the synthetic data (a negative `quantity_litres` on a
      `FuelEvent` row) but not yet checked directly, since `FuelEvent` is a related entity, not
      the target entity these checks currently run against — see follow-up below.
- [x] `docs/DATASET_BUILDING.md`

Follow-up / known gaps (see docs/DATASET_BUILDING.md §8): quality checks are reporting-only —
an `error`-severity finding does not itself drop rows from the built dataset (label-level and
point-in-time issues are the exception — those rows are excluded); checks only run against the
target entity's (`Trip`) raw rows, not related entities, so "impossible fuel quantities" isn't
directly checked yet; `DatasetManifest` is a file artifact, not a queryable control-plane table;
no real-Postgres integration test for this phase (Docker unavailable in this environment, same
as Phase 8 — SQLite end-to-end tests cover the pipeline's logic).

## Phase 10 — Baseline models

- [x] Logistic regression, gradient-boosted trees, small MLP behind a common interface
      (`hermes_rpt.models.interface.BaselineModel`, implemented by
      `hermes_rpt.models.baselines` — all scikit-learn, not torch, so this tier stays
      independent of Phase 11's transformer stack). `HistGradientBoostingClassifier` chosen for
      the GBT baseline specifically for native `NaN` support (see docs/BASELINE_MODELS.md §2 —
      matters because Phase 8/9 features are legitimately missing per tenant by design).
      Training configuration + reproducible seeds: `hermes_rpt.models.config.TrainingConfig`.
- [x] MLflow tracking (params/metrics/tags — tenant id, dataset checksum, mapping version ids,
      code revision, feature-contract/ontology versions), artifact registration + model
      signature (`mlflow.sklearn.log_model`, with an explicit narrow `skops_trusted_types`
      allowlist rather than falling back to less-safe pickle serialization — see
      docs/BASELINE_MODELS.md §5), evaluation report (`hermes_rpt.models.comparison`'s markdown
      table). Tracking store is SQLite (`sqlite:///mlflow.db`), matching
      docker-compose.yml's MLflow service — MLflow's filesystem backend is in maintenance mode
      as of 3.x and refuses to initialize without an explicit opt-out.
- [x] Metrics: ROC-AUC, PR-AUC, precision/recall/F1, Brier score, calibration (10-bin curve),
      confusion matrix at a selected threshold (`hermes_rpt.models.metrics`) — accuracy is
      deliberately never computed. Threshold selection
      (`select_threshold`, default `max_f1` on the *validation* split, never test).
- [x] Registry stages: Candidate/Staging/Production/Archived
      (`hermes_rpt.registry.service.ModelRegistryService` — `register_candidate` always forces
      `CANDIDATE` regardless of caller input; `transition_stage` enforces a fixed transition
      graph, `ARCHIVED` terminal; every transition audited). No auto-promotion.
- [x] Reproducibility (same seed + data -> identical predictions, per baseline), serialization
      (MLflow-stored artifact round-trips via `mlflow.sklearn.load_model` and predicts
      identically), schema-validation (mismatched feature count raises, scikit-learn's own
      guarantee, exercised as a platform contract), missing-feature (GBT native `NaN`; LR/MLP
      imputation — both exercised with real gaps), registry (metadata round-trip, valid/invalid
      stage transitions), and tenant model-access isolation tests (shared-vs-private
      `ModelVersion` visibility, `TenantModelAdapter` strict tenant scoping) — all in
      `tests/model/` (`test_baselines_and_metrics.py`, `test_registry.py`,
      `test_training_service.py`), gated behind `uv sync --group ml` per `make test-model`.
- [x] Baseline comparison report (`hermes_rpt.models.comparison.build_comparison_report` — picks
      the highest-PR-AUC baseline, not ROC-AUC or accuracy, given class imbalance) — produced end
      to end via `make train-baselines` / `.\tasks.ps1 train-baselines`
      (`apps/trainer/main.py`'s `baselines` command, reusing Phase 9's synthetic-tenant
      provisioning via the newly-extracted `scripts.build_synthetic_dataset.
      provision_and_build_dataset`).
- [x] `docs/BASELINE_MODELS.md`

Follow-up / known gaps (see docs/BASELINE_MODELS.md §10): the synthetic generator's delay
outcome is only weakly correlated with the feature set, so a demo comparison report's ROC-AUC
hovering near 0.5 is expected — Phase 9's synthetic data optimizes for realistic
missingness/imbalance/history, not for this baseline's predictive strength; gradient-boosted
trees has no feature-importance signal through this interface (permutation importance would need
held-out data the interface doesn't pass through); the trainer CLI always builds a fresh
synthetic dataset rather than accepting an existing one.

## Phase 11 — Hermes-RPT v0.1 (Tiny)

- [x] Encoders (numeric/categorical/timestamp), entity/table/column embeddings, missing-value
      embeddings, record encoder, relationship embeddings, transformer blocks, attention masks,
      target pooling, classification head — all real, separate modules under
      `hermes_rpt.models.transformer` (`modules.py`, `model.py`); see docs/HERMES_RPT_0_1.md §1
      for the component-to-module mapping. New `hermes_rpt.features.compiler.
      fetch_related_records` reuses Phase 8's exact allowlist/point-in-time-cutoff safety
      machinery to fetch raw per-record relational context (not Phase 8's aggregated scalar
      features), via `hermes_rpt.models.transformer.context.RelationalContextBuilder`.
- [x] Explicit relationship representation (six named relationships — `uses_vehicle`,
      `assigned_driver_history`, `follows_route_history`, `has_deliveries`,
      `has_maintenance_events`, `has_fuel_events` — docs/HERMES_RPT_0_1.md §3); capped
      (`max_records_per_relation`, enforced once at the query layer via `LIMIT`), variable-size
      relational context (`attention_mask` tracks how many of each relation's fixed `K` slots
      hold a real record); masks over fake values (every field-kind encoder substitutes a
      learned missing-value embedding, never a bare `0.0`, for an absent field — categorical's
      missing embedding is embedding index 0 by construction). Tenant isolation (never combine
      contexts across tenants; no `tenant_id` field) is structural, verified directly by test.
- [x] Shape/masking tests (`test_transformer_shapes.py` — every documented tensor shape, target
      always at position 0, missing-field masking, per-relation truncation at the cap, a
      dedicated test proving extra padding doesn't change the target's pooled output),
      tiny-overfit test (`test_transformer_overfit.py` — Tiny memorizes 16 synthetic examples,
      documented as a wiring check, not a capability claim), numerical-stability checks (finite
      logits/loss/gradients after a real forward+backward pass), checkpoint tests
      (`test_transformer_checkpoint.py` — save/reload round-trips to bit-identical predictions).
      39 new tests across `tests/model/test_transformer_*.py`.
- [x] Comparison against strongest baseline (`apps/trainer/main.py`'s new `hermes-rpt` command
      trains all three Phase 10 baselines *and* Hermes-RPT-0.1 Tiny on the identical
      provisioned dataset/split, then folds every result into the same
      `hermes_rpt.models.comparison.build_comparison_report` table); honest reporting — the CLI
      output and docs/HERMES_RPT_0_1.md §8 both explicitly warn against reading a win as evidence
      of production-readiness, since the synthetic labels are only weakly correlated with the
      available features for baselines and transformer alike (see docs/BASELINE_MODELS.md §10).
- [x] Tiny/Small/Base-experimental configs defined (`hermes_rpt.models.transformer.model`); only
      Tiny trained — Small/Base-experimental are construction-and-forward-pass tested only.
- [x] `docs/HERMES_RPT_0_1.md`

Follow-up / known gaps (see docs/HERMES_RPT_0_1.md §10): the model's own checkpoint format uses
`torch.load(weights_only=False)` (needed since it stores a plain config dataclass alongside
weights) and must only ever be loaded from checkpoints this platform itself wrote — no
skops-style trusted-types guard exists for it yet, unlike Phase 10's baselines; no
feature-importance/explainability equivalent for the transformer in this phase; `apps/trainer/
main.py`'s `hermes-rpt` command always provisions a fresh synthetic Tenant-Alpha dataset rather
than accepting an existing one, same limitation as Phase 10's `baselines` command.

## Phase 12 — Relational pretraining

- [x] Configurable self-supervised objectives (`hermes_rpt.models.transformer.pretraining`):
      masked cell reconstruction (covers masked categorical-value prediction + numeric-value
      reconstruction), relationship-link prediction (covers foreign-key target prediction),
      temporal-order prediction. Table/column semantic alignment and record-context matching
      explicitly not implemented — documented scope decisions, not silent gaps (see
      docs/HERMES_RPT_PRETRAINING.md §1). "Avoid objectives that expose direct identifiers
      unnecessarily": `protected_field_names` reads `OntologyFieldDefinition.
      is_business_identifier` (Phase 6) directly, so a business identifier is never a
      reconstruction target — verified across 30 seeds at `mask_probability=1.0`.
- [x] Tenant-boundary-preserving, relation-structure-preserving collator
      (`build_pretraining_batch`) — operates entirely within one already-tenant-scoped
      `EncodedBatch`; a corrupted link keeps its claimed relationship/entity-type embedding
      (only content is swapped), which is what makes the corruption detectable at all; tracks
      which values are labels via explicit per-cell/per-position target tensors; supports
      variable relational contexts by reusing Phase 11's padded layout unchanged.
- [x] Multi-task loss with configurable weights (`hermes_rpt.models.transformer.
      pretraining_model.LossWeights`/`compute_pretraining_loss`) — includes a real regression
      fix caught during development (unnormalized numeric-field MSE, e.g. `manufacture_year`
      ~2020, produced billions-scale loss dominating every other objective; fixed via per-slot
      magnitude scaling, covered by a dedicated regression test). Memorisation-risk canary
      tests (`tests/model/test_pretraining_masking.py` — protected-field exclusion using
      literal canary-shaped values; `tests/model/test_pretraining_end_to_end.py::
      test_finetuning_inference_output_never_exposes_more_than_a_risk_score` — structural proof
      the serving model's only output is one scalar per example, no reconstruction head
      reachable from inference). Pretraining lineage (tenant id, dataset checksum/id, code
      revision, ontology version, active objectives/weights — same tags every other training
      path logs).
- [x] Pretrained vs. non-pretrained Tiny comparison (`apps/trainer/main.py`'s new `pretrain`
      command — pretrains a backbone, fine-tunes both a pretrained and a from-scratch Tiny on
      identical data/config, folds baselines + both variants into one comparison report);
      honest experimental report (docs/HERMES_RPT_PRETRAINING.md §10 — explicit about the
      synthetic dataset's known-weak label correlation meaning this is a pipeline-correctness
      demonstration, not evidence of generalizable pretraining benefit).
- [x] Shared-pretraining consent gate (`hermes_rpt.models.transformer.pretraining_consent` —
      "shared pretraining requires an explicit approved dataset class and consent record," a
      hard stop against `hermes_rpt.tenants.models.DataUsageConsent`, a Phase 2 table whose
      docstring already anticipated this exact use); tenant-isolated by default (every actual
      pretraining call in this phase runs against exactly one tenant).
- [x] Reproducibility checks (`test_pretraining_is_reproducible_given_the_same_seed` — bit-
      identical backbone weights and loss curves for the same seed) and ablation configuration
      (any `LossWeights` with every objective but one at `0.0`; exercised both in isolated loss
      tests and through a full `pretrain()` run).
- [x] `docs/HERMES_RPT_PRETRAINING.md`

21 new tests across `tests/model/test_pretraining_*.py`.

## Phase 13 — Inference API

- [x] `POST /v1/predictions/delivery-delay` per the 14-step flow in prompts.txt
      (`hermes_rpt.inference.service.PredictionService.predict`,
      `apps/api/routers/predictions.py`) — every step delegated to machinery from an earlier
      phase (Phase 3 auth, Phase 8 feature extraction, Phase 10 registry/models), this service is
      coordination, not new business logic. Deliberately scoped to serve Phase 10 baseline
      models only, not Hermes-RPT-0.1 — `ModelLoader.load()` fails closed with
      `UnsupportedModelFamilyError` for a Hermes-RPT `ModelVersion` rather than mis-serving one
      (see docs/INFERENCE_API.md §3 for the reasoning).
- [x] No raw SQL/passwords/connection strings/other-tenant identifiers/unsupported causal
      claims in responses — structural, not just convention (docs/INFERENCE_API.md §4): closed
      response schemas, tenant-scoped repository calls throughout, explanations name a feature +
      correlational direction only, and every domain exception is mapped to a short safe message
      before reaching the client. Verified directly against real response bodies by
      `tests/model/test_predictions_api.py`'s leakage tests, not just by code inspection.
- [x] Idempotency (`PredictionRequest.idempotency_key`, unique per tenant — a Phase 2 column
      used for the first time here) and timeout/retry/circuit-breaker interfaces
      (`hermes_rpt.inference.resilience` — generic, PEP 695 generic primitives, not
      inference-specific; wrapped around `ModelLoader`'s MLflow artifact fetch, the one genuinely
      external dependency in the flow).
- [x] Security, integration, load, contract tests —
      `tests/security/test_predictions_api_auth.py` (auth/scope checks, no model needed),
      `tests/model/test_predictions_api.py` (gated behind `ml`: full flow against a real trained
      + `PRODUCTION`-promoted baseline and real synthetic data; response contract; no
      SQL/secret/connection-string/other-tenant leakage; idempotency; a sequential
      repeated-request smoke test — see note below on why this isn't concurrent),
      `tests/unit/test_inference_resilience.py` (circuit breaker / retry / timeout in isolation).
- [x] `docs/INFERENCE_API.md`

Two real bugs caught while writing the integration test against real trained/promoted models
(not by inspection):
1. `apps/api/deps.py`'s `get_prediction_service` called the `@lru_cache`d `get_model_loader()`
   directly as a plain function rather than through FastAPI's `Depends()` — invisible to
   `app.dependency_overrides`, so tests (and any future caller needing a different `ModelLoader`)
   could never actually swap it. Fixed by adding `ModelLoaderDep` and injecting it as a real
   dependency.
2. `ModelVersion.stage`/`TenantModelAdapter.stage` were plain `String(20)` columns, not a
   converting `Enum(ModelStage)` — since `ModelStage` is a `StrEnum`, most comparisons kept
   silently working against a bare `str` after certain DB round-trips (autoflush/RETURNING
   population during a large multi-entity session, as happens in the real provisioning +
   training pipeline but not in smaller isolated tests), until
   `ModelRegistryService.transition_stage`'s `current.value` access hit a plain `str` and raised
   `AttributeError`. Fixed with `Enum(ModelStage, native_enum=False, ...)` (no DDL/migration
   change — still VARCHAR on disk); the same plain-`String`-for-an-enum pattern exists elsewhere
   in the schema (`RoleName`, `AuditOutcome`, `DatabaseEngine`, ...) and is flagged as follow-up
   for Phase 16's security/consistency review rather than fixed everywhere here.

The load test is sequential, not concurrent: this fixture's single shared `AsyncSession` (the
same one-session-per-test pattern every other test file in this project uses) is not safe for
concurrent access, so a real concurrent-request test would need a differently-shaped fixture
(e.g. a running server + a real connection pool) — noted as a documented limitation, not silently
glossed over, in both the test's docstring and docs/INFERENCE_API.md.

## Phase 14 — Model registry and per-tenant adaptation

- [x] Shared base + tenant adapters; aliases; approval workflow; rollback; deactivation;
      compatibility checks — `hermes_rpt.registry.models.ModelAlias` (new table + RLS migration
      `995d4a06ea5c`) is an atomically-repointable named pointer to a `(ModelVersion,
      TenantModelAdapter | None)` pair, alongside (not replacing) Phase 10's `ModelStage`;
      `ModelRegistryService.set_alias`/`rollback_alias` (rollback is repointing, recorded under a
      distinct audit action), `.register_adapter_candidate`/`.transition_adapter_stage`/
      `.deactivate_model_version`/`.deactivate_adapter`, `IncompatibleAdapterError` (checked at
      both adapter-registration and alias-pointing time). "Promotion requires authorised
      approval": promoting to `STAGING`/`PRODUCTION` or pointing/rolling back an alias now
      requires `ScopeName.MODEL_PROMOTE` when a `TenantContext` is supplied (a `None`
      `tenant_context` — the pre-Phase-14 CLI/script convention — remains a trusted-system-caller
      bypass); archiving/deactivating never requires the scope.
- [x] Tenant-owned adapters, tenant-specific checkpoint paths, tenant-isolated adapter lookup —
      `hermes_rpt.models.transformer.adapter.HermesRPTAdapter` (Houlsby-style residual bottleneck
      + its own small head) / `HermesRPTWithAdapter` (frozen shared backbone, `requires_grad =
      False` on every backbone parameter *and* a `torch.no_grad()` forward pass, belt and
      suspenders — verified by a dedicated regression test that trains for several steps and
      asserts every backbone tensor is bit-identical afterward).
      `hermes_rpt.models.transformer.adapter_training.TenantAdapterTrainingService` writes
      checkpoints to `checkpoint_dir/<tenant_id>/adapter_<size>.pt` and serializes only the
      adapter's own `state_dict()` — never the backbone's, so "shared models must never contain
      tenant-private adapter weights" holds structurally. `register_adapter_candidate` requires a
      real `TenantContext` (not optional, unlike shared-model registration) — "training jobs must
      include trusted tenant context."
- [x] Alpha/Beta adapter experiment; rollback tested — `apps/trainer/main.py`'s new `adapt`
      command (`make adapt-hermes-rpt`): trains one shared Hermes-RPT-0.1 (Tiny) backbone, then a
      private adapter per tenant on top of it, and **proves tenant isolation against the real
      governance layer, not just a repository unit test** — Alpha's `TenantContext` attempting to
      read or alias Beta's adapter both fail, asserted (the run itself fails loudly if either
      isolation property doesn't hold). Ran successfully end to end; comparison table (shared
      base alone vs. each tenant's adapted result) printed and reviewed. Rollback is tested via
      `test_rollback_repoints_the_alias_and_is_distinguishable_in_the_audit_trail` (repoints an
      alias back to a prior model version and confirms both the resolved target and the audit
      action name).
- [x] `docs/MODEL_ADAPTATION.md`

New tests: `tests/model/test_model_governance.py` (17 tests — compatibility, approval gate,
aliases/rollback, deactivation), `tests/model/test_adapter_shapes.py` (6 tests — shape,
gradient-isolation, numerical stability). Full `tests/model` suite (110 tests) and
`tests/unit`+`tests/security` (312 tests) re-run clean after these changes; ruff/mypy/bandit all
clean across `src apps scripts tests`.

## Phase 15 — Monitoring and drift detection

- [x] Security, data/schema, and model monitoring per prompts.txt; tenant-scoped metrics —
      `hermes_rpt.monitoring.metrics` (Prometheus-compatible, dedicated `CollectorRegistry`),
      instrumented at the real failure/event sites: auth/authz/cross-tenant handlers
      (`apps/api/exception_handlers.py`), secret resolution + connection usage
      (`hermes_rpt.connectors.service`), disallowed query attempts
      (`hermes_rpt.features.service.FeatureExtractionService.extract`), schema fingerprint
      changes + mapping suspension (`hermes_rpt.schemas.service`, `hermes_rpt.mappings.
      service`), missing-feature-rate + invalid-data-rate (`hermes_rpt.datasets.builder`),
      prediction volume/latency/error/distribution/model-version-usage
      (`hermes_rpt.inference.service.PredictionService.predict`). Feature/label distribution
      drift scoring (`hermes_rpt.monitoring.drift`) is implemented and tested but not yet
      auto-scheduled — see docs/MONITORING.md §4/§8 for why. "Precision/recall when labels
      arrive" is new domain logic, not just a metric: `PredictionOutcome` (migration
      `978402aae411`) + `hermes_rpt.monitoring.outcomes.OutcomeService`, exposed via
      `POST /v1/monitoring/predictions/{id}/outcome`.
      Tenant-scoped summary (`GET /v1/monitoring/summary`,
      `hermes_rpt.monitoring.service.MonitoringService`) queries tenant-scoped repositories
      directly rather than reading the process-wide Prometheus counters — the same isolation
      mechanism every other tenant-facing read in this platform uses, deliberately kept
      separate from the operator-only `GET /metrics` scrape endpoint (`ScopeName.
      MONITORING_READ`) — see docs/MONITORING.md §1 for the known gap in that scope's
      enforcement (no genuine platform-wide credential type exists yet).
- [x] No high-cardinality raw-ID labels; redaction in logs; internal-only tenant-identifying
      alerts — label-safety rules enforced structurally (`hermes_rpt.monitoring.metrics`'s
      docstring + `test_every_metric_only_uses_allowed_label_names`); log redaction extended to
      personal data (email/display_name/phone-shaped keys and an email-value pattern), not just
      secrets (`hermes_rpt.common.logging`); `tenant_mismatch_handler` labels its metric with
      the *caller's* tenant only, never the tenant whose resource was almost reached.
- [x] Incident, schema-drift, and model-rollback runbooks — docs/runbooks/{INCIDENT_RUNBOOK,
      SCHEMA_DRIFT_RUNBOOK,MODEL_ROLLBACK_RUNBOOK}.md, docs/MONITORING.md
- [x] Metric isolation and redaction tests — tests/security/test_monitoring_isolation.py,
      tests/unit/test_monitoring_{metrics,drift,outcomes}.py

`tests/unit/test_tenant_session.py` / `tests/security/test_tenant_rls_binding.py` predate this
phase (added during GCP deployment troubleshooting: `bind_tenant_for_row_level_security` existed
but was never wired into the request pipeline, and its `SET LOCAL ... = :param` was invalid
asyncpg syntax — see docs/DEPLOYMENT.md §5 and git history for the two fix commits) but are
exactly the kind of coverage this phase's "metric isolation and redaction tests" line item is
about, so they're listed here too rather than only under Phase 14's deployment work.

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
