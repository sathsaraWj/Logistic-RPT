# Monitoring and Drift Detection

Status: implements Phase 15 of [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md). Runbooks:
[INCIDENT_RUNBOOK.md](runbooks/INCIDENT_RUNBOOK.md),
[SCHEMA_DRIFT_RUNBOOK.md](runbooks/SCHEMA_DRIFT_RUNBOOK.md),
[MODEL_ROLLBACK_RUNBOOK.md](runbooks/MODEL_ROLLBACK_RUNBOOK.md).

## 1. Two surfaces, deliberately kept separate

`hermes_rpt.monitoring`:

* **`GET /metrics`** — raw Prometheus scrape output (`hermes_rpt.monitoring.metrics.REGISTRY`,
  a dedicated `CollectorRegistry`, not `prometheus_client`'s process-wide default). Cross-tenant
  *by necessity* — an operator/alerting system needs every tenant's series in one place to know
  which tenant an anomaly belongs to ("alerts should identify the tenant internally"). Gated on
  `ScopeName.MONITORING_READ`.
* **`GET /v1/monitoring/summary`** (`hermes_rpt.monitoring.service.MonitoringService`) — a
  single tenant's own numbers, computed by querying tenant-scoped repositories directly (the
  same isolation mechanism every other tenant-facing read in this platform uses), never by
  reading the Prometheus counters. This is what makes "dashboards must not expose one tenant to
  another" true for the tenant-facing side without needing two different isolation stories.

**Known gap** (see [DEPLOYMENT.md](DEPLOYMENT.md) §3 and flagged again for `docs/SECURITY_
REVIEW.md` once Phase 16 exists): this platform's auth model has
no genuine "platform-wide, not tied to any tenant" credential yet — every token belongs to one
tenant. `MONITORING_READ` is therefore safe only because of *who it's issued to*
(trusted internal scraper/operator credentials), not because of a structural check that a
`/metrics` caller has no tenant. Don't grant this scope to an ordinary tenant-admin role.

## 2. Label-safety rules

Full rationale in `hermes_rpt.monitoring.metrics`'s module docstring; the short version: a label
value is either a platform-assigned, bounded-cardinality UUID (`tenant_id`, `model_version_id`,
`connection_id`) or a small fixed-set string this module defines (`reason`, `check`, `severity`,
`status`, `feature_name` — feature names come from a `FeatureContract`, a platform-defined,
bounded set, not user input). Never a business/customer-supplied value of unbounded cardinality
— a trip number, an email, a connection name a tenant admin typed in.
`tests/unit/test_monitoring_metrics.py::test_every_metric_only_uses_allowed_label_names` is a
standing guard against a future metric breaking this rule.

## 3. Metric catalogue

| Family | Type | Labels | Instrumented at |
|---|---|---|---|
| `hermes_cross_tenant_access_attempts_total` | Counter | `tenant_id` | `apps/api/exception_handlers.py`'s `tenant_mismatch_handler` |
| `hermes_authentication_failures_total` | Counter | `reason` | `apps/api/exception_handlers.py`'s `authentication_error_handler` |
| `hermes_authorization_failures_total` | Counter | `tenant_id`, `reason` | `apps/api/exception_handlers.py`'s `authorization_error_handler` |
| `hermes_secret_resolution_failures_total` | Counter | `tenant_id` | `hermes_rpt.connectors.service.ConnectionLifecycleManager._resolve_target` |
| `hermes_disallowed_query_attempts_total` | Counter | `tenant_id`, `reason` | `hermes_rpt.features.service.FeatureExtractionService.extract` (wraps the two query-execution call sites) |
| `hermes_unexpected_connection_usage_total` | Counter | `tenant_id`, `reason` | `ConnectionLifecycleManager.validate_connection` / `.enable_connection` |
| `hermes_schema_fingerprint_changes_total` | Counter | `tenant_id` | `hermes_rpt.schemas.service.SchemaDiscoveryService.run_discovery` |
| `hermes_mapping_suspensions_total` | Counter | `tenant_id` | `hermes_rpt.mappings.service.MappingService.suspend_affected_by_drift` |
| `hermes_missing_feature_rate` | Gauge | `tenant_id`, `feature_name` | `hermes_rpt.datasets.builder.DatasetBuildService.build` |
| `hermes_invalid_data_rate_total` | Counter | `tenant_id`, `check`, `severity` | `hermes_rpt.datasets.builder.DatasetBuildService.build` |
| `hermes_feature_distribution_drift_score` / `hermes_label_distribution_drift_score` | Gauge | `tenant_id`(, `feature_name`) | Computed by `hermes_rpt.monitoring.drift` — see §4, not wired to automatic instrumentation yet |
| `hermes_prediction_requests_total` | Counter | `tenant_id`, `status` | `hermes_rpt.inference.service.PredictionService.predict` |
| `hermes_prediction_latency_seconds` | Histogram | `tenant_id` | same |
| `hermes_prediction_distribution` | Histogram | `tenant_id`, `model_version_id` | same |
| `hermes_model_version_usage_total` / `hermes_adapter_version_usage_total` | Counter | `tenant_id`, `model_version_id`/`adapter_id` | `PredictionService.predict` (model version only — no serving path uses an adapter yet, see [INFERENCE_API.md](INFERENCE_API.md) §3/§7) |
| `hermes_model_precision` / `hermes_model_recall` / `hermes_model_calibration_drift` | Gauge | `tenant_id`, `model_version_id` | `hermes_rpt.monitoring.outcomes.OutcomeService.compute_realized_model_metrics`, recomputed on every `POST /v1/monitoring/predictions/{id}/outcome` |

## 4. Distribution drift — computed, not yet auto-scheduled

`hermes_rpt.monitoring.drift.compute_feature_drift`/`compute_label_drift` compare two
`FeatureStatistic`/`LabelStatistics` snapshots (the same types `hermes_rpt.datasets.statistics`
already produces per dataset build) and score the shift — same shape as
`hermes_rpt.schemas.drift.compare_snapshots` for schema drift. **Not wired to run
automatically inside `DatasetBuildService.build()`**: doing that needs a "previous build's
statistics for this dataset_key" lookup this platform doesn't persist anywhere queryable yet
(manifests are written to disk by CLI tooling, not stored as DB rows — see
[DATASET_BUILDING.md](DATASET_BUILDING.md)). Call the drift functions directly with two
manifests' statistics if you need this today; a "dataset build history" store is natural
follow-up before this can run unattended.

## 5. Precision/recall when labels arrive

`hermes_rpt.inference.models.PredictionOutcome` (migration `978402aae411`) records the actual,
later-known outcome for a prediction — a new row, never a mutation of the original
`PredictionResult`. `POST /v1/monitoring/predictions/{prediction_id}/outcome` (tenant-scoped,
`ScopeName.PREDICTION_EXECUTE`) is the only way to write one; every call recomputes and
republishes that tenant's realized precision/recall/calibration-drift for the model version
involved, from every labeled prediction on record — not just the one just submitted. Decision
boundary is a fixed 0.5 on `delay_probability`, independent of the low/medium/high risk-level
buckets (those are for explanation, not a binary decision).

## 6. Logs

`hermes_rpt.common.logging`'s redaction processor (mandatory, applied to every log line
regardless of call site) now covers personal data as well as secrets — `email`, `display_name`,
`phone_number`-shaped keys, and an email-address value pattern, on top of the password/token/
connection-string coverage from Phase 4. See `tests/unit/test_logging_redaction.py`.

## 7. Testing

* `tests/unit/test_monitoring_metrics.py` — label-safety rule enforcement, scrape-output smoke
  tests.
* `tests/unit/test_monitoring_drift.py` — feature/label drift scoring correctness.
* `tests/unit/test_monitoring_outcomes.py` — realized precision/recall/calibration-drift
  arithmetic, and that outcome recording is tenant-scoped.
* `tests/unit/test_tenant_session.py` — unrelated Phase 15 addition surfaced while building this
  phase; see [DEPLOYMENT.md](DEPLOYMENT.md)'s note on the RLS binding bug it caught.
* `tests/security/test_monitoring_isolation.py` — scope enforcement on both monitoring routes,
  and the actual isolation proof: tenant B's summary never reflects tenant A's activity.

## 8. Known gaps / follow-up

* `/metrics`'s access control is scope-only (§1) — a real platform-operator credential type
  (distinct from "a token that happens to belong to some tenant") is natural Phase 16 follow-up.
* Feature/label distribution drift isn't auto-scheduled (§4).
* No alerting rules are checked into this repository yet (no Alertmanager config) — the metric
  names and label conventions above are the contract a future alerting setup would consume;
  [INCIDENT_RUNBOOK.md](runbooks/INCIDENT_RUNBOOK.md) documents what triage looks like once one
  exists.
* Adapter version usage (`hermes_adapter_version_usage_total`) is defined but never incremented
  — no serving path uses a tenant adapter yet (same gap as
  [MODEL_ADAPTATION.md](MODEL_ADAPTATION.md) §6 / [INFERENCE_API.md](INFERENCE_API.md) §7).
