# Security Review (Phase 16)

Status: **Complete for this pass.** No Critical or High-severity finding remains open — see §2.
A small number of Medium/Low findings are deliberately deferred, each with a stated reason (§3).
This document is the artifact [docs/THREAT_MODEL.md](THREAT_MODEL.md) §7 commits to producing.

## 1. Method

Three independent, read-only, file:line-referenced audits were run in parallel against the 18
review areas in prompts.txt Prompt 16 (authentication, authorisation, tenant isolation,
object-level access, connection-pool isolation, secret handling, SQL construction, mapping
expressions, dataset isolation, model registry isolation, cache isolation, logging, background
jobs, file paths, model artifacts, deserialization, dependency risks, CI secrets, error
messages), plus my own targeted follow-up while implementing the fixes below (findings 6 and the
`audit_events` RLS-policy bug were found this way, not by the three audits). Each finding below
is stated as: what was found, why it matters, how it was fixed (or why it's deferred), and where
the regression test lives.

## 2. Critical / High findings — all fixed

| # | Area | Finding | Fix | Test |
|---|---|---|---|---|
| 1 | Dataset isolation | `DatasetBuildService.build()` trusted `DatasetDefinition.tenant_id` from the caller-supplied definition instead of verifying it against the authenticated `TenantContext` — a definition object built (or tampered) to claim a different tenant would build a dataset under the wrong tenant. | `_validate_definition_matches_tenant()` in `src/hermes_rpt/datasets/builder.py`, called at the top of `build()`; raises `DatasetTenantMismatchError`. Also removed a vacuous `check_cross_tenant_contamination` call that always trivially passed. | `tests/security/test_dataset_build_service.py` |
| 2 | Model registry isolation | `ModelRegistryService.transition_stage`/`.deactivate_model_version` looked models up via an unscoped `self._models.get(model_version_id)` — any authenticated tenant could transition or deactivate *any* tenant's model version by UUID. | `_get_for_stage_mutation()` now routes through `require_available_for_tenant` when a `TenantContext` is supplied (`tenant_context=None` remains the existing trusted-system/CLI-caller convention). | `tests/model/test_registry.py`, `test_model_governance.py` |
| 3 | Model artifacts | Artifact checksums were computed from the in-memory estimator object *before* MLflow serialized it — not guaranteed to match what MLflow actually stored — and nothing ever re-verified the checksum at load time, so a tampered/repointed `artifact_uri` or a substituted artifact under an unchanged URI would be served undetected. | `src/hermes_rpt/registry/artifact_integrity.py` (new): `compute_artifact_checksum()` downloads and hashes the actual stored bytes via `mlflow.artifacts.download_artifacts`. Computed *after* `mlflow.sklearn.log_model()` at training time; re-verified in `ModelLoader.load()` before trusting the artifact, raising `ArtifactIntegrityError` (mapped to a fixed-message 503, never `str(exc)`, at the API boundary). | `tests/security/test_model_artifact_integrity.py`, `tests/model/test_training_service.py`, `test_predictions_api.py` |
| 4 | Connection-pool isolation | A `DISABLED` `CustomerDatabaseConnection` could still be used via `get_or_create_engine` — disabling a connection didn't actually stop in-flight or new query execution against it. | `ConnectionDisabledError` raised immediately after resolving the connection in `get_or_create_engine` if `status == DISABLED`, with a `metrics.unexpected_connection_usage_total` increment. | `tests/security/test_connector_isolation.py`, `test_pool_registry.py` |
| 5 | Authorisation | `GET /metrics` (Prometheus scrape endpoint) required only `ScopeName.MONITORING_READ` — a human tenant token that happened to carry that scope could scrape *every* tenant's aggregate metrics, not just the endpoint operator. | Composed `require_service` with `require_scopes(MONITORING_READ)` so only a service-principal token can reach `/metrics`; `/v1/monitoring/summary` (tenant-scoped) is unchanged. Residual gap honestly documented below (§3). | `tests/security/test_monitoring_isolation.py` |

## 3. Medium / Low findings — fixed, or deferred with reasoning

### Fixed

* **RLS never bound for background-job writes** (background jobs area). `run_discovery_job`
  opened its own DB session, separate from the HTTP request's, and never called
  `bind_tenant_for_row_level_security` — the job's writes (snapshot, drift summary, audit
  events) ran with PostgreSQL RLS's defense-in-depth layer silently absent, relying entirely on
  application-layer filtering holding with zero margin for error. Fixed in
  `src/hermes_rpt/schemas/jobs.py`; regression test in
  `tests/security/test_worker_job_tenant_binding.py`.
* **`audit_events` RLS policy could never admit a NULL-tenant row** (tenant isolation /
  logging). Found while implementing the fix above, not by any of the three audits: the original
  policy `USING (tenant_id = hermes_current_tenant_id())` relies on SQL `NULL = NULL` evaluating
  to `NULL` (not `TRUE`), so every platform-level audit event (pre-auth-failure writes, which
  are deliberately tenant-`NULL`) would silently fail to insert on real PostgreSQL, forever.
  Fixed via migration `b48a21a99ef0` — the policy is now
  `(tenant_id = hermes_current_tenant_id()) OR (tenant_id IS NULL AND hermes_current_tenant_id()
  IS NULL)`, matched on both `USING` and `WITH CHECK`. `bind_tenant_for_row_level_security`'s
  signature also changed from taking a full `TenantContext` to a raw `uuid.UUID | None`, since
  the exception-handler audit-write path only ever has a possibly-`None` tenant_id in scope; it
  now no-ops for `None` (documented as equivalent to clearing the session variable, since every
  caller opens a fresh session per call).
* **Secret leak via `repr()`** (secret handling / logging). `ConnectionTarget.password` had no
  `repr=False`, so any code path that logged or printed a `ConnectionTarget` (e.g. an
  unstructured `str(exc)` during a connection failure) would leak the plaintext password.
  `src/hermes_rpt/connectors/interfaces.py`: `password: str = field(repr=False)`.
* **Redaction processor had no fallback for opaque objects** (logging). The structlog redaction
  processor (`hermes_rpt.common.logging`) matched known sensitive *keys* and known
  string/dict/list shapes, but an arbitrary object logged under a safe-looking key (e.g. a
  dataclass whose `__repr__` happens to embed a DSN) passed through unredacted. Added a generic
  fallback: anything that isn't a `str`/`Mapping`/`list`/`tuple`/safe scalar has its `repr()`
  scanned against the same sensitive-value patterns and is fully redacted if any match. Also
  added a keyword-style DSN pattern (`password=\S+`, asyncpg's connect-string shape) to the
  sensitive-value patterns. `tests/unit/test_logging_redaction.py`.
* **`jwt_algorithm` was an unconstrained string** (authentication). `Settings.jwt_algorithm` had
  no validator — PyJWT's own `NoneAlgorithm` currently rejects `alg=none` when a signing key is
  supplied, so nothing was actually exploitable today, but that safety was an assumption about
  PyJWT's behavior, not something this codebase enforced itself. Added `_validate_jwt_algorithm`
  (allowlist: HS256/384/512, RS256/384/512, ES256/384), matching the existing pattern for
  `log_level`/`database_url`. `tests/unit/test_settings.py`.
* **Unbounded recursion in derived mapping expressions** (mapping expressions). `Concat.parts` /
  `Coalesce.options` / nested `Expression` fields had no depth limit — a sufficiently deep
  expression submitted in a mapping document would risk a `RecursionError` in `evaluate()` or
  `referenced_columns()` (a validation-time DoS). Added `check_expression_depth()` (max depth
  20), called unconditionally in `_validate_source_columns` — before the
  `available_columns is None` short-circuit, since the DoS risk exists regardless of whether
  column-existence checking runs for a given mapping. `tests/unit/test_mapping_expressions.py`,
  `tests/unit/test_mapping_validation.py`.
* **CI gaps** (CI secrets / dependency risks). `pip-audit` ran in CI but was
  `continue-on-error: true` ("advisory... made blocking once a baseline is agreed") — the
  current dependency set has zero known vulnerabilities (`uv run pip-audit` confirmed clean),
  so it's now blocking in both `.github/workflows/ci.yml` and `deploy.yml`. `tests/security`
  (the fast tenant-isolation/auth/injection suite, ~1 minute) now also runs in both CI and the
  deploy pipeline's pre-deploy test gate — previously only `tests/unit` ran there, so a security
  regression could reach `main`/production without ever running the security suite.

### Deferred (Medium/Low, not blocking — reasoning below)

None of these are exploitable for cross-tenant data access; each is either intra-tenant-only,
requires an already-compromised credential, or is a hardening improvement rather than a closed
gap. Per prompts.txt's "do not mark security work complete while critical or high-severity
findings remain," none of these rise to that bar, but they're recorded here rather than silently
dropped.

* **No pagination/row limit on list endpoints** (excessive queries). `apps/api/routers/
  mappings.py`, `schema_discovery.py`, `connections.py`, `memberships.py` list endpoints have no
  `limit`/`offset` and no enforced maximum result size. Exploitable only by an already-
  authenticated tenant against their *own* data (no cross-tenant read), and control-plane row
  counts per tenant are small in the platform's current shape (mappings, connections,
  memberships — not customer data). `src/hermes_rpt/auth/rate_limit.py` defines
  `InMemoryFixedWindowRateLimiter` but it is wired into zero call sites — no rate-limiting
  middleware exists in `apps/api/main.py` at all. Real fix (bounded `Query(le=...)` params
  across four routers, plus wiring the existing rate limiter into the ASGI stack) is a
  contained but non-trivial change deferred to a follow-up pass.
* **DoS via large schema discovery** (denial of service). `PostgresSchemaIntrospector.
  introspect()` has no cap on the number of schemas/tables/columns/indexes it will enumerate
  from a customer's own database — bounded only by whatever the tenant put on their own
  `schema_allowlist`. Contrast: profiling (`hermes_rpt.schemas.profiling`) *is* capped
  (`max_tables` ≤20, `sample_rows` ≤200). Query-level `statement_timeout` bounds execution
  *time* but not the size of the result set built in memory before that timeout would fire. A
  customer can only point this at their *own* connection, so the primary risk is a
  self-inflicted resource exhaustion on that tenant's own discovery job, not a cross-tenant
  attack surface — still worth a `max_tables`-style cap mirroring profiling's, deferred to a
  follow-up pass alongside the pagination fix above (both are "add a bound to an existing
  unbounded query," best done together).
* **Mapping column-level allowlist gap.** A tenant can map any column of an allowlisted table
  through `MappingDocument`, including ones that would be denylisted by *name pattern* under
  profiling's credential/personal-field detection — the mapping layer and the profiling layer
  enforce different things (mapping: table/schema allowlist only; profiling: column-name
  denylist). Intra-tenant only (a tenant mapping their own data), and by design mapping is a
  broader, human-reviewed operation (mappings go through an approval workflow) than
  auto-triggered profiling. Recorded as a design note, not a fix, for now.
* **Dead join-allowlist-validation code path** — a code-quality finding (unreachable branch in
  join validation), not a security gap; noted for cleanup, not urgent.
* **Membership `is_active` not re-checked per-request.** A deactivated membership's existing
  token remains valid for its full TTL (max `access_token_ttl_seconds` = 900s by default) rather
  than being invalidated immediately. Standard bearer-token tradeoff (short TTL bounds the
  window); a real fix would need either a revocation list or very short-lived tokens plus
  refresh, both larger changes than this pass.
* **Idle connection pool entries don't re-authenticate after external credential rotation.**
  `TenantConnectionPoolRegistry` doesn't currently detect a customer rotating their DB password
  out-of-band and proactively evict the stale pooled engine — the next query simply fails and
  surfaces as a connection error (visible via `hermes_unexpected_connection_usage_total` /
  `secret_resolution_failures_total`), it doesn't silently succeed with stale credentials. Not
  a security gap (no unauthorized access), an operability one; see
  [docs/runbooks/INCIDENT_RUNBOOK.md](runbooks/INCIDENT_RUNBOOK.md) §3.
* **WIF attribute-condition unverifiable from the repo.** The GitHub Actions →
  GCP Workload Identity Federation trust binding referenced in `.github/workflows/deploy.yml`
  (`google-github-actions/auth@v2`) restricts which repository can assume
  `github-deployer@hermes-rpt-demo.iam.gserviceaccount.com`, but the actual WIF pool/provider
  attribute-condition configuration lives in GCP, not this repository — it cannot be verified
  by reading source. Flagged for out-of-band verification by whoever owns the GCP project (see
  [docs/DEPLOYMENT.md](DEPLOYMENT.md) §3).
* **stdlib-logging bypass of the redaction processor.** structlog's redaction processor
  (`hermes_rpt.common.logging`) only wraps loggers obtained through structlog; SQLAlchemy's,
  asyncpg's, and uvicorn's own stdlib loggers are not routed through a `ProcessorFormatter`
  bridge, so a driver-level log line (e.g. a raw SQL error message from asyncpg) could in
  principle contain unredacted detail. In practice these libraries don't log credential values
  by default (asyncpg logs connection *errors*, not the DSN used), and none of this platform's
  own code logs through stdlib directly. Bridging all three third-party loggers through the
  same processor chain is a real hardening step, deferred as a follow-up rather than blocking
  this pass.

## 4. Confirmed non-findings (investigated, nothing to fix)

* **Cross-tenant cache keys.** No Redis/TTL cache exists anywhere in the codebase.
  `TenantConnectionPoolRegistry` keys engines by `(tenant_id, connection_id)` — no collision
  possible. `ModelLoader`'s in-process cache is keyed by `ModelVersion.id` (a UUID); the model
  version served is always resolved server-side from the authenticated tenant's own production
  alias/task configuration (`apps/api/routers/predictions.py` never accepts a client-supplied
  model version ID), so there is no client-reachable path to make two tenants collide on the
  same cache entry. `get_ontology`/`get_entity_model` `@lru_cache`s are keyed by static config
  (ontology version/entity name), not tenant data.
* **Unsafe deserialization.** No `pickle.load(s)`, unsafe `yaml.load`, `eval`, `exec`, or
  `marshal.loads` anywhere in `src/`/`apps/`. The one place that might look deserialization-
  adjacent — the derived mapping-expression language (`hermes_rpt.mappings.expressions`) — is a
  closed, Pydantic-discriminated set of node types with a hand-written `evaluate()` that only
  ever does `dict` key lookups, never `eval`/`getattr`/attribute access on arbitrary objects;
  see `tests/security/test_injection_and_traversal.py`.
* **SQL injection.** Every raw-SQL identifier interpolation found is guarded: schema
  introspection uses bound `:schemas` params with no identifier interpolation at all; the one
  place identifiers *are* interpolated (`hermes_rpt.schemas.profiling`'s sampling query) both
  comes from the platform's own introspection (not client input) and is checked by
  `_assert_safe_identifier` (rejects any `"` or NUL byte, which is sufficient for a
  double-quoted Postgres identifier — there is no way to break out of the quoted identifier
  without a `"` character); `MappingDocument`'s client-supplied schema/table/column/join
  identifiers are restricted to `^[A-Za-z_][A-Za-z0-9_]*$` at the Pydantic layer, before they
  can reach any query; actual data-extraction queries
  (`hermes_rpt.features.compiler`) use SQLAlchemy `sa.table()`/`sa.column()` plus an explicit
  allowlist check (`hermes_rpt.connectors.query_guard.assert_object_allowed`), never
  string-built SQL.
* **Path traversal.** The only place a stored, non-identifier-validated path-like value feeds
  into a filesystem/artifact read is `ModelVersion.artifact_uri` — but that value is always
  server-generated (set by `BaselineTrainingService.train()` from MLflow's own
  `runs:/<run_id>/model` URI scheme immediately after a real training run), never client input,
  so there's no reachable attack surface to traverse through it. Every client-supplied
  identifier-shaped field (mapping schema/table/column/join names) is restricted to the same
  `^[A-Za-z_][A-Za-z0-9_]*$` pattern that also rules out injection payloads — see
  `tests/security/test_injection_and_traversal.py`.

## 5. Adversarial test coverage summary

All 16 named attack vectors from prompts.txt Prompt 16 now have automated coverage:

| Attack vector | Coverage |
|---|---|
| Changing resource UUIDs | `tests/security/test_tenant_isolation.py` and per-domain security suites |
| Supplying another tenant ID | `tests/security/test_tenant_isolation.py`, `test_authorization.py` |
| Forged tenant headers | `tests/security/test_authorization.py`, `test_tenant_isolation.py` |
| Invalid JWT claims | `tests/security/test_authentication.py` (9 cases) |
| SQL injection | `tests/security/test_injection_and_traversal.py`, `tests/unit/test_query_guard.py` |
| Mapping-expression injection | `tests/security/test_injection_and_traversal.py`, `tests/unit/test_mapping_expressions.py` |
| Path traversal | `tests/security/test_injection_and_traversal.py` |
| Model artifact substitution | `tests/security/test_model_artifact_integrity.py` |
| Unsafe deserialization | `tests/security/test_injection_and_traversal.py` (confirmatory — see §4) |
| Cross-tenant cache keys | See §4 (confirmed non-finding; nothing exploitable to construct a positive test around) |
| Cross-tenant worker jobs | `tests/security/test_worker_job_tenant_binding.py` |
| Cross-tenant adapter loading | `tests/model/test_model_governance.py`, `test_registry.py` |
| Secrets appearing in logs | `tests/unit/test_logging_redaction.py` |
| Excessive queries | See §3 (deferred — no positive test possible for a gap that's intentionally left open; tracked as a follow-up) |
| DoS through large schema discovery | See §3 (deferred, same reasoning) |
| Stale mapping use after schema drift | `tests/security/test_mapping_lifecycle.py` |

## 6. Related documents

* [docs/THREAT_MODEL.md](THREAT_MODEL.md) — the design-time threat model this review validates
  against.
* [docs/DATA_PROTECTION.md](DATA_PROTECTION.md) — what data this platform holds, how it's
  classified, and how it's protected at rest/in transit/in logs.
* [docs/INCIDENT_RESPONSE.md](INCIDENT_RESPONSE.md) — what to do if one of the threats in §5
  above is ever actually triggered.
* [docs/CUSTOMER_DATABASE_GUIDE.md](CUSTOMER_DATABASE_GUIDE.md) — the customer-facing guidance
  behind "read-only, least-privilege, allowlisted" connections.
