"""Tests for the mandatory secret-redaction log processor (docs/THREAT_MODEL.md T-I3)."""

from __future__ import annotations

from dataclasses import dataclass

from hermes_rpt.common.logging import _redact_processor
from hermes_rpt.connectors.interfaces import ConnectionTarget


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


def test_personal_data_keys_are_redacted() -> None:
    event = _redact_processor(
        None,
        "info",
        {
            "event": "user_created",
            "email": "alice@example.com",
            "display_name": "Alice Example",
            "phone_number": "+1-555-0100",
        },
    )
    assert event["email"] == "***REDACTED***"
    assert event["display_name"] == "***REDACTED***"
    assert event["phone_number"] == "***REDACTED***"
    assert event["event"] == "user_created"


def test_email_address_in_free_text_is_redacted_even_under_a_safe_key() -> None:
    event = _redact_processor(
        None,
        "info",
        {"note": "failed to notify alice@example.com about the update"},
    )
    assert "alice@example.com" not in event["note"]


def test_keyword_style_dsn_password_fragment_is_redacted() -> None:
    event = _redact_processor(
        None,
        "info",
        {"note": "connect failed: host=db.internal user=alice password=hunter2 dbname=hermes"},
    )
    assert "hunter2" not in event["note"]


def test_a_tuple_of_sensitive_values_is_redacted_element_by_element() -> None:
    event = _redact_processor(None, "info", {"pair": ("safe", "Bearer abc.def.ghi")})
    assert "abc.def.ghi" not in event["pair"][1]
    assert event["pair"][0] == "safe"


def test_an_opaque_object_holding_a_credential_shaped_string_is_fully_redacted() -> None:
    @dataclass
    class Opaque:
        note: str

        def __repr__(self) -> str:
            return f"Opaque(note={self.note!r})"

    event = _redact_processor(
        None, "info", {"context": Opaque("postgresql://alice:s3cr3t@db.internal:5432/hermes")}
    )
    assert event["context"] == "***REDACTED***"


def test_connection_target_repr_never_includes_the_password() -> None:
    """`ConnectionTarget.password` has `field(repr=False)` — belt-and-suspenders alongside the
    generic opaque-object fallback above."""

    target = ConnectionTarget(
        host="db.internal",
        port=5432,
        database="hermes",
        username="alice",
        password="s3cr3t",
        tls_mode="require",
    )
    event = _redact_processor(None, "info", {"target": target})
    assert "s3cr3t" not in str(event["target"])


def test_plain_scalars_pass_through_unredacted() -> None:
    event = _redact_processor(None, "info", {"count": 3, "ratio": 0.5, "ok": True, "gap": None})
    assert event == {"count": 3, "ratio": 0.5, "ok": True, "gap": None}
