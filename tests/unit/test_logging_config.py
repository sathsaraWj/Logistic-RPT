"""Regression test: `configure_logging` + `get_logger(...).info(...)` must actually work
end-to-end, not just the redaction processor in isolation (tests/unit/test_logging_redaction.py
tests `_redact_processor` directly and would not have caught a processor pipeline that raises
on the very first real log call — e.g. using a stdlib-only processor with a non-stdlib logger
factory)."""

from __future__ import annotations

from hermes_rpt.common.logging import configure_logging, get_logger
from hermes_rpt.common.settings import Settings


def test_configured_logger_can_emit_a_log_line_without_raising() -> None:
    configure_logging(Settings())
    logger = get_logger(__name__)
    logger.info("smoke_test_event", some_field="value", tenant_id="not-a-secret")
