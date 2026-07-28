# Inference API

Status: implements Phase 13 of [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md). Builds on Phase 8
([FEATURE_EXTRACTION.md](FEATURE_EXTRACTION.md) — point-in-time feature extraction, reused
unchanged) and Phase 10 ([BASELINE_MODELS.md](BASELINE_MODELS.md) — the model family this endpoint
serves).

## 1. The endpoint

`POST /v1/predictions/delivery-delay`

```json
// request
{"trip_id": "TRIP-0001", "prediction_time": "2026-06-01T00:00:00Z"}
```

```json
// response — 200
{
  "prediction_id": "...",
  "trip_id": "TRIP-0001",
  "delay_probability": 0.42,
  "risk_level": "medium",
  "model_version": "delivery-delay-risk-logistic_regression",
  "feature_version": "1",
  "prediction_time": "2026-06-01T00:00:00Z",
  "explanations": [{"factor": "planned_distance_km", "direction": "increases_risk"}]
}
```

Optional `Idempotency-Key` request header — see §5.

There is no field anywhere in the request for a tenant id, a connection string, or raw SQL: the
only identity the endpoint trusts is the authenticated `TenantContext`
(`hermes_rpt.auth.dependencies.require_scopes(ScopeName.PREDICTION_EXECUTE)`), built exclusively
from verified JWT claims, the same invariant every other endpoint in this platform holds.

## 2. The 14-step flow

`hermes_rpt.inference.service.PredictionService.predict()` implements prompts.txt Prompt 13's flow
end to end, delegating every step to machinery that already existed from an earlier phase — this
service is coordination, not new business logic:

| # | Step | Implementation |
|---|---|---|
| 1-2 | Authenticate / resolve tenant | `apps.api.deps` dependency chain, before the service is constructed |
| 3 | Authorise `prediction:execute` | `require_scopes` router dependency |
| 4-6 | Resolve active connection / mapping / feature definition | `FeatureExtractionService` (Phase 8) for baselines, `RelationalContextBuilder` (Phase 11) for Hermes-RPT — same underlying allowlist/mapping-resolution safety either way |
| 7 | Extract point-in-time features | `FeatureExtractionService.extract` for baselines (Phase 8's leakage-cutoff enforcement applies as-is); `RelationalContextBuilder.build_example(..., label=None)` + `encode_batch` for Hermes-RPT — raw relational context instead of a scalar feature row |
| 8 | Validate model input | fixed feature-name ordering (`_FEATURE_NAMES`) + scikit-learn's own shape check inside `predict_proba` for baselines; `EncodedBatch`'s fixed tensor shapes (`encode_batch`) for Hermes-RPT |
| 9 | Resolve authorised model version | `ModelVersionRepository.get_production_model` — `PRODUCTION`-staged, shared-or-mine, tenant-private wins ties |
| 10 | Run prediction | `ModelLoader` branches by model family to `LoadedModel.predict_proba` (sklearn) or `LoadedHermesRPTModel.predict_proba` (torch) |
| 11 | Generate safe explanation | `hermes_rpt.inference.explanation` |
| 12 | Persist prediction lineage | `PredictionRequest` + `PredictionResult` rows |
| 13 | Emit audit event | `hermes_rpt.audit`, `prediction.execute`, `SUCCESS` or `ERROR` |
| 14 | Return response | router builds the HTTP response from the service's `PredictionResponse` |

A failure at any point after the `PredictionRequest` row is created marks it `FAILED` and emits an
`ERROR` audit event before re-raising — every prediction attempt is auditable, not just successful
ones.

## 3. Scope decision: both model families are servable; Hermes-RPT-0.1 (Tiny, scratch) is
promoted to production (Phase 19)

This endpoint serves both Phase 10's scikit-learn baselines and Phase 11's Hermes-RPT-0.1.
`hermes_rpt.inference.model_loading.ModelLoader.load()` branches by model family — a `ModelVersion`
whose name marks it as `hermes-rpt` goes through a torch-checkpoint loading path
(`LoadedHermesRPTModel`), everything else through the pre-existing sklearn-flavor path
(`LoadedModel`) — rather than one endpoint silently assuming a single family. `PredictionService`
branches the same way: baselines get a scalar feature row from `FeatureExtractionService`; Hermes-RPT
gets raw relational context from `RelationalContextBuilder`, built live per request with `label=None`
(the same tenant-scoped fetch machinery training already used, now also called from this endpoint).
Any model family this loader doesn't recognize still fails closed with `UnsupportedModelFamilyError`
rather than being silently mis-loaded.

This was, until Phase 19, deliberately out of scope: Hermes-RPT-0.1 was documented throughout
Phases 11/12 as experimental, not production-ready (see
[ADR 0008](adr/0008-baseline-models-before-relational-transformer.md)), and ADR-0008 set an
explicit, falsifiable bar — the transformer must *beat* the strongest baseline to justify serving
it, not merely exist. That bar wasn't answerable at all on the original synthetic dataset, whose
label was statistically independent of every feature (see
[docs/BASELINE_MODELS.md](BASELINE_MODELS.md) §10 and
[docs/MODEL_RESEARCH_PLAN.md](MODEL_RESEARCH_PLAN.md) §9's Phase 19 follow-up). Phase 19 fixed the
label generator's correlation, re-ran the comparison, and `hermes-rpt-0.1-tiny-scratch` won
consistently across three training seeds — see the numbers and the full honesty caveat (still
synthetic data; still no real-customer-data evidence) in
[docs/MODEL_RESEARCH_PLAN.md](MODEL_RESEARCH_PLAN.md) §9 and
[docs/RELEASE_READINESS.md](RELEASE_READINESS.md) §4. It was promoted to `PRODUCTION` as a result.

What's still out of scope: shared cross-tenant pretraining and per-tenant adapters
(Phases 12/14) are not wired into this endpoint — only the scratch-per-tenant training path is
servable here. Building an adapter-aware serving path is explicit future follow-up, not something
this endpoint silently half-does.

## 4. What a response never contains

Prompt 13 lists specific things a prediction response must never expose. How each is prevented,
structurally, not just by convention:

* **Raw SQL** — `FeatureExtractionService` (Phase 8) only ever returns already-typed feature
  values through an allowlisted, parameterised query layer; nothing downstream of it ever
  constructs or forwards a SQL string.
* **Passwords / connection strings** — `PredictionResponse` and `DeliveryDelayPredictionResponse`
  are closed Pydantic/dataclass shapes listing exactly the fields in §1; there is no code path
  that reads a connection secret and no field that could carry one.
* **Other tenants' identifiers** — `get_production_model` and every repository call in the flow is
  tenant-scoped (`TenantScopedRepository`/`ModelVersionRepository`'s shared-or-mine rule); a model
  or prediction belonging to another tenant is never loaded into scope in the first place.
* **Sensitive raw source values** — explanations name a *feature* and a *direction*
  (`increases_risk`/`decreases_risk`), never the underlying raw database value that produced it
  (`hermes_rpt.inference.explanation.generate_explanations`).
* **Unsupported causal claims** — `PredictionExplanation.direction` is a correlational statement
  about a fitted linear coefficient, not a causal one; nothing in the response uses causal
  language ("caused", "because"), and this file documents that framing so it isn't drifted into
  later.
* **Internal error detail** — every domain exception the service can raise
  (`TargetRowNotFoundError`, `TargetMappingUnavailableError`, `NoProductionModelError`,
  `PredictionTaskNotConfiguredError`, `UnsupportedModelFamilyError`, `CircuitOpenError`,
  `TimeoutExceededError`) is caught in `apps/api/routers/predictions.py` and mapped to a clean
  status code with a short, safe message — never a raw traceback or exception string from a
  layer below.

`tests/model/test_predictions_api.py::test_response_never_leaks_raw_sql_secrets_or_connection_strings`
and `::test_response_never_leaks_the_other_tenants_identifiers` assert this directly against a
real response body, not just by code inspection.

## 5. Idempotency

`PredictionRequest.idempotency_key` (Phase 2 schema, unique per `(tenant_id, idempotency_key)`)
was already there; this phase is the first to use it. Sending the same `Idempotency-Key` header
twice returns the identical `prediction_id` and result without re-running feature extraction or
inference — `PredictionService._cached_response` looks up a prior `COMPLETED` request + its result
before doing any new work. Omitting the header (or reusing a key with a *different* `trip_id`, which
is not currently rejected — see §7) always runs a fresh prediction.

## 6. Resilience: timeout, retry, circuit breaker

`hermes_rpt.inference.resilience` — generic, reusable, not inference-specific:

* `with_timeout(coro, seconds=...)` — wraps `asyncio.wait_for`, raises `TimeoutExceededError`.
* `retry_with_backoff(operation, config=RetryConfig(...))` — exponential backoff, only retries
  exception types in `retryable` (default: everything) so a programming error or auth failure is
  never silently retried into a different outcome.
* `CircuitBreaker` — closed → open (after `failure_threshold` consecutive failures) → half-open
  (after `reset_timeout_seconds`) → closed again on the next success. Not cross-process; an
  in-process guard, same scope as `hermes_rpt.connectors.pool_registry`'s per-process pooling.

All three wrap exactly one place: `ModelLoader.load()`'s MLflow artifact fetch — the one genuinely
external dependency in the inference flow (the database calls before and after it already have
their own connection-level handling from earlier phases). A loaded model is cached in-process by
`ModelVersion.id`, so the wrapped call only actually runs on a cache miss (first request for a
given production model, or after a process restart).

`CircuitOpenError` → `503`, `TimeoutExceededError` → `504` at the router.

## 7. Known gaps / follow-up

* Shared cross-tenant pretraining and per-tenant adapters (Phases 12/14) are not wired into this
  endpoint — only baselines and scratch-per-tenant Hermes-RPT-0.1 are servable. An
  adapter-aware serving path (resolving a `TenantModelAdapter` alongside its frozen base
  `ModelVersion`) is explicit future follow-up.
* An idempotency key reused with a *different* `trip_id`/`prediction_time` silently returns the
  first call's cached result rather than rejecting the mismatch with a `409` — acceptable for now
  since idempotency keys are expected to be generated per logical request by the caller, but worth
  tightening if this endpoint gets a wider caller base.
* `ModelLoader`'s in-process cache never evicts on a model being `ARCHIVED`/replaced — a newly
  promoted `PRODUCTION` model is picked up on its own first request (a different `ModelVersion.id`
  is a cache miss), but a stale in-memory copy of a superseded model is only cleared by a process
  restart. Fine for this platform's current scale; a TTL or explicit invalidation hook is natural
  follow-up.
* The synthetic dataset's delay label was strengthened in Phase 19 to be risk-weighted by
  distance/vehicle-age/breakdown/route/driver history (see
  [BASELINE_MODELS.md](BASELINE_MODELS.md) §10) specifically so a baseline-vs-Hermes-RPT
  comparison could be honest — see [MODEL_RESEARCH_PLAN.md](MODEL_RESEARCH_PLAN.md) §9's Phase 19
  follow-up for the actual result. This is still synthetic data with deliberately-injected
  correlation, not real fleet operations — a demo prediction's `delay_probability` is evidence the
  pipeline and the architecture can exploit learnable structure, not evidence of real-world
  predictive skill.

## 8. Testing

* `tests/security/test_predictions_api_auth.py` — auth/authorization checks that don't need a real
  trained model: missing bearer token (`401`), missing `prediction:execute` scope (`403`), request
  body cannot select a tenant, and an unconfigured endpoint fails closed with a safe error body.
* `tests/model/test_predictions_api.py` (gated behind `uv sync --group ml`) — the full flow against
  a real trained, `PRODUCTION`-promoted baseline and real synthetic tenant data: successful
  prediction, unknown-trip `404`, exact response contract (no extra/missing fields), no
  SQL/secret/connection-string/other-tenant leakage, idempotency (same key → same
  `prediction_id`; different keys → independent predictions), and a light concurrent-request smoke
  test.
* `tests/model/test_predictions_api_hermes_rpt.py` (gated behind `uv sync --group ml`) — the same
  suite as above, but served by a real trained, `PRODUCTION`-promoted Hermes-RPT-0.1 model, proving
  the endpoint's public contract is identical regardless of serving family, plus an explicit
  assertion that no explanation is fabricated for a model with no linear coefficients.
* `tests/security/test_model_artifact_integrity.py` — checksum-substitution rejection for both
  the sklearn and Hermes-RPT loading paths.
* `tests/security/test_transformer_inference_tenant_isolation.py` — proves the live,
  per-request `RelationalContextBuilder` call this endpoint makes for Hermes-RPT can't resolve
  another tenant's trip, and that predicting against another tenant's `trip_id` returns `404`
  with no cross-tenant data in the response.
* `tests/unit/test_inference_resilience.py` — `with_timeout`, `retry_with_backoff`, and
  `CircuitBreaker`'s closed/open/half-open transitions in isolation, independent of MLflow or the
  API.

## 9. Running it

```bash
make install-ml
uvicorn apps.api.main:app --reload   # or: .\tasks.ps1 run-api
```

Requires a `PredictionTaskDefinition` row for `delivery-delay-risk` (seeded by
`migrations/versions/96b4009ff8d0_seed_roles_and_delivery_delay_task.py`) and at least one
`PRODUCTION`-staged `ModelVersion` for that task — `make train-baselines` trains and registers
candidates; promotion to `STAGING`/`PRODUCTION` is a separate, deliberate
`ModelRegistryService.transition_stage` call ([BASELINE_MODELS.md](BASELINE_MODELS.md) §6), not
automatic.
