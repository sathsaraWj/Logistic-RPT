"""Tests for the mandatory secret-redaction log processor (docs/THREAT_MODEL.md T-I3)."""

from __future__ import annotations

from hermes_rpt.common.logging import _redact_processor


def test_sensitive_key_names_are_redacted() -> None:
    event = _redact_processor(
        None,
        "info",
        {
            "event": "connected",
            "password": "hunter2",
            "db_secret": "abc123",
            "Authorization": "Bearer sometoken",
        },
    )
    assert event["password"] == "***REDACTED***"
    assert event["db_secret"] == "***REDACTED***"
    assert event["Authorization"] == "***REDACTED***"
    assert event["event"] == "connected"


def test_connection_string_credentials_are_redacted_even_under_a_safe_key() -> None:
    event = _redact_processor(
        None,
        "info",
        {"note": "using postgresql://alice:s3cr3t@db.internal:5432/hermes"},
    )
    assert "s3cr3t" not in event["note"]
    assert "alice" not in event["note"]


def test_bearer_token_in_free_text_is_redacted() -> None:
    event = _redact_processor(
        None,
        "info",
        {"note": "rejected request with Authorization: Bearer abc.def.ghi"},
    )
    assert "abc.def.ghi" not in event["note"]


def test_nested_structures_are_redacted() -> None:
    event = _redact_processor(
        None,
        "info",
        {"context": {"credential_reference": "ref-1", "password": "hunter2"}},
    )
    assert event["context"]["password"] == "***REDACTED***"
