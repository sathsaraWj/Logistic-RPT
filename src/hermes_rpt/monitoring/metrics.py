"""Prometheus-compatible metrics (Phase 15) — every metric this platform exposes lives here,
in one place, so the label conventions below are actually enforced rather than merely
documented.

**Label discipline — read before adding a metric.** A label value must be either a
platform-assigned, bounded-cardinality identifier (`tenant_id`, `model_version_id`,
`connection_id` — all internal UUIDs the platform controls the population of) or a small,
fixed-set enum-like string this module defines (`reason`, `check`, `engine`, `status`). A label
must **never** carry a customer/business-supplied value of unbounded cardinality — a trip
number, a VIN, an email address, a connection name a tenant admin typed in, a raw error message.
That's "avoid high-cardinality labels containing raw business IDs" (Phase 15 requirement) and
also what keeps this process's Prometheus series count bounded regardless of tenant data volume.

**Tenant scoping vs. tenant isolation.** Every tenant-relevant metric here *does* carry a
`tenant_id` label — "alerts should identify the tenant internally" requires that. What makes
this safe is where each surface sits: `/metrics` (`hermes_rpt.monitoring.router`) is an
operator-only scrape endpoint gated by `ScopeName.MONITORING_READ`, never a tenant-facing view —
a tenant is never granted a scope that lets it read another tenant's series off that endpoint.
The tenant-facing summary (`MonitoringService.tenant_summary`) does not read these Prometheus
counters at all; it queries tenant-scoped repositories directly, the same isolation mechanism
every other tenant-facing read in this platform uses. See docs/DEPLOYMENT.md and
docs/runbooks/INCIDENT_RUNBOOK.md for how the two surfaces are meant to be operated.

A dedicated `CollectorRegistry` (not `prometheus_client`'s global default) so this module can be
imported freely by tests without polluting — or being polluted by — anything else that might
use the default registry.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

REGISTRY = CollectorRegistry()

# --- Platform security ---------------------------------------------------------------------

cross_tenant_access_attempts_total = Counter(
    "hermes_cross_tenant_access_attempts_total",
    "Requests that resolved to a resource belonging to a different tenant than the caller's.",
    labelnames=("tenant_id",),
    registry=REGISTRY,
)

authentication_failures_total = Counter(
    "hermes_authentication_failures_total",
    "Requests that failed authentication, by reason.",
    labelnames=("reason",),
    registry=REGISTRY,
)

authorization_failures_total = Counter(
    "hermes_authorization_failures_total",
    "Authenticated requests denied at the authorization step, by reason.",
    labelnames=("tenant_id", "reason"),
    registry=REGISTRY,
)

secret_resolution_failures_total = Counter(
    "hermes_secret_resolution_failures_total",
    "Failed attempts to resolve a stored secret reference.",
    labelnames=("tenant_id",),
    registry=REGISTRY,
)

disallowed_query_attempts_total = Counter(
    "hermes_disallowed_query_attempts_total",
    "Query-guard or cost-guard rejections against a tenant's customer database, by reason.",
    labelnames=("tenant_id", "reason"),
    registry=REGISTRY,
)

unexpected_connection_usage_total = Counter(
    "hermes_unexpected_connection_usage_total",
    "Customer database connection failures/misuse (connect errors, disabled-connection use).",
    labelnames=("tenant_id", "reason"),
    registry=REGISTRY,
)

# --- Data and schema -------------------------------------------------------------------------

schema_fingerprint_changes_total = Counter(
    "hermes_schema_fingerprint_changes_total",
    "Schema snapshots whose fingerprint differs from the immediately preceding one.",
    labelnames=("tenant_id",),
    registry=REGISTRY,
)

mapping_suspensions_total = Counter(
    "hermes_mapping_suspensions_total",
    "Schema mappings auto-suspended because their source schema drifted.",
    labelnames=("tenant_id",),
    registry=REGISTRY,
)

missing_feature_rate = Gauge(
    "hermes_missing_feature_rate",
    "Fraction of rows missing a given feature in the most recent dataset build.",
    labelnames=("tenant_id", "feature_name"),
    registry=REGISTRY,
)

feature_distribution_drift_score = Gauge(
    "hermes_feature_distribution_drift_score",
    "Normalized shift in a feature's mean between two dataset builds (0 = no shift).",
    labelnames=("tenant_id", "feature_name"),
    registry=REGISTRY,
)

label_distribution_drift_score = Gauge(
    "hermes_label_distribution_drift_score",
    "Absolute change in positive-label fraction between two dataset builds.",
    labelnames=("tenant_id",),
    registry=REGISTRY,
)

invalid_data_rate_total = Counter(
    "hermes_invalid_data_rate_total",
    "Data-quality issues found during dataset builds, by check name and severity.",
    labelnames=("tenant_id", "check", "severity"),
    registry=REGISTRY,
)

# --- Model -------------------------------------------------------------------------------------

prediction_requests_total = Counter(
    "hermes_prediction_requests_total",
    "Prediction requests, by tenant and outcome status.",
    labelnames=("tenant_id", "status"),
    registry=REGISTRY,
)

prediction_latency_seconds = Histogram(
    "hermes_prediction_latency_seconds",
    "End-to-end latency of a prediction request.",
    labelnames=("tenant_id",),
    registry=REGISTRY,
)

prediction_distribution = Histogram(
    "hermes_prediction_distribution",
    "Distribution of predicted probabilities returned to callers.",
    labelnames=("tenant_id", "model_version_id"),
    buckets=(0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
    registry=REGISTRY,
)

model_calibration_drift = Gauge(
    "hermes_model_calibration_drift",
    "Absolute difference between mean predicted probability and realized positive rate, over "
    "predictions with a recorded outcome.",
    labelnames=("tenant_id", "model_version_id"),
    registry=REGISTRY,
)

model_precision = Gauge(
    "hermes_model_precision",
    "Realized precision, computed from predictions with a recorded actual outcome.",
    labelnames=("tenant_id", "model_version_id"),
    registry=REGISTRY,
)

model_recall = Gauge(
    "hermes_model_recall",
    "Realized recall, computed from predictions with a recorded actual outcome.",
    labelnames=("tenant_id", "model_version_id"),
    registry=REGISTRY,
)

model_version_usage_total = Counter(
    "hermes_model_version_usage_total",
    "Predictions served by a given model version.",
    labelnames=("tenant_id", "model_version_id"),
    registry=REGISTRY,
)

adapter_version_usage_total = Counter(
    "hermes_adapter_version_usage_total",
    "Predictions served by a given tenant adapter version.",
    labelnames=("tenant_id", "adapter_id"),
    registry=REGISTRY,
)
