"""Tests for `hermes_rpt.monitoring.metrics` — the label-safety rules the module's own
docstring commits to, plus a smoke test that increments/observations actually work and show up
in scrape output. Instrumentation-site tests (does an auth failure *actually* increment the
right counter) live alongside the code they instrument, e.g.
tests/security/test_monitoring_isolation.py.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, generate_latest

from hermes_rpt.monitoring import metrics

# Every label name any metric in this module is allowed to use — platform-controlled,
# bounded-cardinality identifiers and small fixed-set enum-like strings only. Extend this list
# deliberately, not reflexively, whenever a new metric is added (see metrics.py's module
# docstring for why).
_ALLOWED_LABEL_NAMES = {
    "tenant_id",
    "reason",
    "status",
    "check",
    "severity",
    "feature_name",
    "model_version_id",
    "adapter_id",
}


def _all_metric_objects() -> list[Counter | Gauge | Histogram]:
    return [
        value for value in vars(metrics).values() if isinstance(value, Counter | Gauge | Histogram)
    ]


def test_every_metric_only_uses_allowed_label_names() -> None:
    offenders = {
        metric._name: metric._labelnames  # noqa: SLF001 - introspection is the point of this test
        for metric in _all_metric_objects()
        if not set(metric._labelnames).issubset(_ALLOWED_LABEL_NAMES)  # noqa: SLF001
    }
    assert offenders == {}


def test_at_least_the_documented_metric_families_exist() -> None:
    names = {metric._name for metric in _all_metric_objects()}  # noqa: SLF001
    expected = {
        "hermes_cross_tenant_access_attempts",
        "hermes_authentication_failures",
        "hermes_authorization_failures",
        "hermes_secret_resolution_failures",
        "hermes_disallowed_query_attempts",
        "hermes_unexpected_connection_usage",
        "hermes_schema_fingerprint_changes",
        "hermes_mapping_suspensions",
        "hermes_missing_feature_rate",
        "hermes_invalid_data_rate",
        "hermes_prediction_requests",
        "hermes_prediction_latency_seconds",
        "hermes_model_precision",
        "hermes_model_recall",
        "hermes_model_calibration_drift",
        "hermes_model_version_usage",
        "hermes_adapter_version_usage",
    }
    assert expected.issubset(names)


def test_a_counter_increment_shows_up_in_scrape_output() -> None:
    metrics.authentication_failures_total.labels(reason="test_reason_xyz").inc()
    output = generate_latest(metrics.REGISTRY).decode()
    assert 'reason="test_reason_xyz"' in output


def test_a_gauge_set_shows_up_in_scrape_output() -> None:
    metrics.missing_feature_rate.labels(tenant_id="test-tenant-xyz", feature_name="x1").set(0.25)
    output = generate_latest(metrics.REGISTRY).decode()
    assert 'tenant_id="test-tenant-xyz"' in output
    assert "0.25" in output
