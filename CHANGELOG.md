# Changelog

Phase-by-phase history of the Hermes-RPT platform build-out (`0.1.0`, unreleased — see
[docs/RELEASE_READINESS.md](docs/RELEASE_READINESS.md) for why this is not yet a tagged release).
Each phase corresponds to a numbered prompt in `prompts.txt`; see [TASKS.md](TASKS.md) for the
full, checkable detail behind every line here.

## Phase 20 — External partner service credentials (this change)

- New `ServiceCredential` (`hermes_rpt.auth.models`): a per-integration `client_id`/`client_secret`
  pair an external partner (e.g. Hermes VMS's backend) exchanges for a short-lived
  `principal_type=SERVICE` JWT via `POST /v1/auth/service-token` — the first real,
  production-usable way for a system outside Hermes-RPT to authenticate without a human login.
  Previously the only token-issuance path (`issue_dev_token`) was hard-gated to `local`/`ci`.
- Solves the blast-radius problem a naive long-lived-JWT approach would have had: there's no
  `jti` denylist anywhere in this codebase, so a leaked long-lived token could only be
  invalidated by rotating the platform's single global signing secret — breaking every other
  tenant and every internal service at once. Revocation now acts at the credential layer
  (`DELETE /v1/auth/service-credentials/{id}`) instead: stops all future exchanges immediately,
  bounded to at most one `service_token_ttl_seconds` window (default 3600s) for any token
  already issued, with zero impact on anything else.
- New scope `service_credential:manage`, kept separate from `tenant:admin` — minting a machine
  credential that can obtain SERVICE-typed tokens is a materially higher-blast-radius capability
  than membership/mapping governance. A credential's own grantable scopes explicitly exclude
  `tenant:admin` and `service_credential:manage` itself.
- First real callers for two previously-idle pieces: `Settings.service_token_ttl_seconds` and
  `hermes_rpt.auth.rate_limit.InMemoryFixedWindowRateLimiter` (now rate-limiting the
  token-exchange endpoint per `client_id` and per source IP — the one endpoint in the API whose
  body *is* the authentication).
- New migration adds `service_credentials` with RLS enabled, but a deliberate, documented policy
  carve-out (an unbound session sees every row) — the token-exchange lookup has no
  `TenantContext` yet, since resolving which tenant a `client_id` belongs to is the point of the
  call. Recorded as an explicit residual risk in `docs/THREAT_MODEL.md` §6.
- 20 new adversarial security tests (`tests/security/test_service_credentials.py`) mirroring the
  existing auth test-suite style: wrong secret/unknown client indistinguishable, revoked
  credential rejected but already-issued tokens honor their tradeoff, expired credential
  rejected, cross-tenant isolation, rate-limit trips, an explicit algorithm-confusion regression
  test (a gap the existing suite didn't cover anywhere), and audit-trail assertions.
- Docs: `docs/AUTHENTICATION.md` new §5a (explicitly distinguished from §5's *internal*
  service-to-service auth), `docs/THREAT_MODEL.md` new actor and threats T-S4/T-D3.

## Phase 19 — Hermes-RPT-0.1 (Tiny, scratch) promoted to production

- Strengthened the synthetic label generator (`hermes_rpt.synthetic.generator._resolve_trip_outcome`)
  to be risk-weighted by trip distance, vehicle age, recent breakdown history, and route/driver
  delay-rate history — the original label was statistically independent of every feature, so no
  baseline-vs-transformer comparison could ever be more than noise. See
  `docs/BASELINE_MODELS.md` §10.
- Re-ran the honest baseline-vs-Hermes-RPT comparison (`make train-hermes-rpt`) against the
  corrected label: `hermes-rpt-0.1-tiny-scratch` beat every Phase 10 baseline on PR-AUC,
  consistently across three training seeds — full numbers in `docs/MODEL_RESEARCH_PLAN.md` §9's
  Phase 19 follow-up and `docs/adr/0008-baseline-models-before-relational-transformer.md`'s
  update. Promoted to `PRODUCTION` as a result, per ADR-0008's own falsifiable bar.
- Built the real-time serving path Hermes-RPT never had: `ModelLoader` (`hermes_rpt.inference.
  model_loading`) now branches by model family to a new torch-checkpoint loading path
  (`LoadedHermesRPTModel`) alongside the existing sklearn path; `PredictionService` now builds
  live relational context via `RelationalContextBuilder` (`label=None`) for Hermes-RPT requests
  instead of `FeatureExtractionService`'s scalar feature row.
- Fixed a real, previously-latent bug this work surfaced: `compute_artifact_checksum`
  (`hermes_rpt.registry.artifact_integrity`) silently hashed *zero bytes* for any single-file
  MLflow artifact (`mlflow.artifacts.download_artifacts` returns the file path itself, not a
  containing directory, for a single-file artifact) — meaning every Hermes-RPT model would have
  failed integrity verification the moment it was actually served for real. Caught by
  `tests/security/test_transformer_inference_tenant_isolation.py`'s real, unmocked round trip, not
  by unit tests that had mocked past the discrepancy. Fixed the shared checksum function and
  aligned `HermesRPTTrainingService.train()` to compute its stored checksum the same way the
  baseline path already does, rather than a separate, hand-rolled local-file hash.
- New tests: `tests/model/test_predictions_api_hermes_rpt.py` (full endpoint contract, served by a
  real trained/promoted Hermes-RPT model), two new checksum-substitution tests in
  `tests/security/test_model_artifact_integrity.py`, and
  `tests/security/test_transformer_inference_tenant_isolation.py` (proves the new live
  `RelationalContextBuilder` caller can't cross tenants).
- Fixed a cold-start latency bug found while exercising the full local demo pipeline: the first
  `mlflow.sklearn.load_model` call in a process pays tens of seconds of one-time torch/skops
  import cost plus an unnecessary MLflow telemetry network call, both charged against the
  10-second per-request model-load timeout. Disabled the telemetry call, warmed the import at
  app startup instead of on the first request, and raised the timeout default to 60s.
- Docs: `docs/INFERENCE_API.md` §3/§7/§8 rewritten for the two-family serving design,
  `docs/RELEASE_READINESS.md` §4/§6 updated with the actual result, `docs/adr/0008-baseline-
  models-before-relational-transformer.md` update note, `docs/MODEL_RESEARCH_PLAN.md` §9
  follow-up.

## Phase 18 — Final repository audit

- Full-repository audit: architecture, tenant isolation, test coverage, type safety, dependency
  health, migration consistency (verified a clean upgrade+downgrade cycle from scratch), API
  documentation, model reproducibility, dataset lineage, schema drift behaviour, registry
  rollback, secret redaction, Docker setup, CI setup, documentation accuracy, dead code, TODOs,
  placeholder implementations, and production-readiness gaps.
- Fixed a real documentation-accuracy gap: `docs/ARCHITECTURE.md` §8 still said deployment was
  "not built" when a real Cloud Run deployment has existed since Phase 14; `docs/
  MODEL_RESEARCH_PLAN.md`'s status line still said "no model code exists yet" after Phases 10–14
  built and tested all of it. Both corrected, with an honest retrospective added to the latter.
- `tasks.ps1` (the Windows equivalent of the Makefile) was missing every `demo-*` target added in
  Phase 17 — added, matching the Makefile exactly.
- New `docs/RELEASE_READINESS.md`: completed capabilities, known limitations, security status,
  model-performance status, required infrastructure, production blockers, and the recommended
  next milestone — the artifact this whole audit phase exists to produce.
- New `CHANGELOG.md` (this file).

## Phase 17 — End-to-end demonstration

- Two-tenant local demo (`scripts/demo/`, `make demo-{up,seed,discover,map,train,predict,
  security-test,down}`) against real, separate PostgreSQL databases per tenant and the real HTTP
  API — not mocks. Alpha/Beta use prompts.txt's literal five-table lists with deliberately
  different schemas and terminology, mapped onto the same five ontology entities.
- Running this for real — the first time anything in the repo combined a real HTTP API, a real
  Postgres control plane, and real Postgres customer databases all at once — surfaced and fixed
  several genuine, previously-latent bugs:
  - A `sqlalchemy.exc.MissingGreenlet` crash on any endpoint that mutates a row and serializes it
    into the same response (10 route handlers across `connections.py`/`mappings.py`/
    `schema_discovery.py`), fixed with an explicit `session.refresh()`.
  - A scikit-learn baseline crash/misalignment (`SimpleImputer` silently dropping, and
    `HistGradientBoostingClassifier` crashing on, a feature that's 100%-missing for a tenant).
  - A missing `tls_mode="disable"` on an existing real-DB integration test that had never
    actually been executed before (Docker was unavailable in this environment until this phase).
- Surfaced a genuinely Critical, still-open finding: the database role this platform connects as
  has `BYPASSRLS`, making every PostgreSQL Row-Level Security policy a structural no-op — see
  `docs/SECURITY_REVIEW.md` §2a and `docs/RELEASE_READINESS.md` §6.
- `docs/DEMO.md`.

## Phase 16 — Security hardening

- Three-way parallel, read-only security audit across 18 review areas (authentication,
  authorization, tenant isolation, object-level access, connection-pool isolation, secret
  handling, SQL construction, mapping expressions, dataset/registry isolation, cache isolation,
  logging, background jobs, file paths, model artifacts, deserialization, dependency risks, CI
  secrets, error messages).
- Fixed all five Critical/High findings, each with a named regression test: tenant-identity
  spoofing in dataset building; unscoped model-registry stage mutations; model artifact
  checksums computed over the wrong bytes and never re-verified at load time; disabled
  connections still usable; `/metrics` reachable by any scoped human token.
- Fixed several Medium findings: RLS never bound for background-job writes; a genuine
  `audit_events` RLS policy bug where `NULL = NULL` silently rejected every platform-level audit
  write; a secret leaking via `repr()`; no opaque-object fallback in log redaction; an
  unconstrained `jwt_algorithm` setting; unbounded recursion in derived mapping expressions.
  Hardened CI to block on `pip-audit` and run the security test suite on every push/deploy.
- Adversarial test coverage added for all 16 named attack vectors from prompts.txt Prompt 16.
- Four required docs: `docs/SECURITY_REVIEW.md`, `docs/DATA_PROTECTION.md`,
  `docs/INCIDENT_RESPONSE.md`, `docs/CUSTOMER_DATABASE_GUIDE.md`.

## Phase 15 — Monitoring and drift detection

- Prometheus-compatible metrics (`hermes_rpt.monitoring`) via a dedicated registry, strict
  label-safety discipline (only bounded platform UUIDs/enum-like strings, never business/customer
  IDs), two separate surfaces: operator-only `GET /metrics` and tenant-scoped
  `GET /v1/monitoring/summary`.
- Realized precision/recall/calibration-drift computed from actually-recorded prediction
  outcomes (`PredictionOutcome`), not just training-time metrics.
- Three operational runbooks: incident, schema-drift, model-rollback.

## Phase 14 — Model registry and per-tenant adaptation

- `ModelAlias`: an atomically-repointable named pointer to a `(ModelVersion, TenantModelAdapter |
  None)` pair, alongside Phase 10's `ModelStage`. Rollback is repointing, distinctly audited.
- Tenant-owned adapters on a frozen shared backbone (`requires_grad=False` on every backbone
  parameter, verified bit-identical before/after adapter training) — tenant isolation proven by
  assertion against the real governance layer, not just repository-level unit tests.

## Phase 13 — Inference API

- `POST /v1/predictions/delivery-delay`: the trusted tenant comes exclusively from the
  authenticated context, never from the request body. Safe explanations (never a raw feature
  dump). Idempotency. Timeout/retry/circuit-breaker around MLflow artifact loading, the one
  genuine external dependency in the inference path.

## Phase 12 — Relational pretraining

- Configurable self-supervised objectives (masked-cell reconstruction, relationship-link
  prediction, temporal-order prediction) with a tenant-boundary-preserving collator.
- Protected-identifier exclusion from reconstruction targets, verified across 30 seeds.
- Shared-pretraining consent gate against `DataUsageConsent` — a hard stop, not a suggestion.
- Bit-identical reproducibility for a fixed seed; pretrained-vs-scratch comparison pipeline.

## Phase 11 — Hermes-RPT-0.1 (Tiny)

- A real relational transformer: explicit entity/relationship embeddings, learned missing-value
  embeddings (never fake zeros), bounded/masked variable-size relational context, six named
  relationships, target always at a fixed pooling position.
- Tiny/Small/Base-experimental configs defined; only Tiny actually trained (Small/Base are
  construction-and-forward-pass tested only — deliberately not trained without a reason to
  justify the compute).

## Phase 10 — Baseline models

- Logistic regression, gradient-boosted trees, small MLP — the quality floor every later model
  is compared against, per ADR-0008. Shared comparison report, MLflow tracking.

## Phases 6–9 — Ontology, mapping, feature extraction, dataset building

- Canonical, customer-agnostic ontology (`configs/ontology/v1.yaml`).
- A restricted, non-Turing-complete declarative mapping/derived-expression language — no
  arbitrary SQL or Python ever reachable from a mapping document.
- Point-in-time-correct, leakage-safe feature extraction and dataset building, with manifests,
  checksums, and lineage; generated data always stored outside the repository.

## Phases 4–5 — Customer connectivity, schema discovery

- Read-only, per-tenant connection pools; register/validate/enable/disable/rotate/delete
  lifecycle; statement-shape and schema/table-allowlist guards.
- Versioned, fingerprinted schema snapshots; drift detection; affected-mapping suspension, never
  silent auto-rewrite.

## Phases 1–3 — Platform foundations

- Tenant/user/membership model, role/scope-based authorization, verified-JWT-only tenant
  resolution (never client-supplied), structured logging with mandatory secret redaction, an
  audit trail for every access, lifecycle change, and prediction.
