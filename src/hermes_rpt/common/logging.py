"""Structured logging with mandatory secret redaction.

Every log line goes through `_redact_processor` before it is emitted, regardless of which
logger call site produced it. This is defense in depth for the "never log secrets" invariant
(docs/IMPLEMENTATION_PLAN.md §2) — call sites are still expected not to log secrets
deliberately, but a forgotten `str(connection)` or an exception message containing a DSN
should not make it into log output verbatim.
"""

from __future__ import annotations

import logging
import re
from collections.abc import MutableMapping
from typing import Any

import structlog
from structlog.types import EventDict, Processor

from hermes_rpt.common.settings import Settings

_REDACTED = "***REDACTED***"

# Key names that are always redacted regardless of value shape. Covers both secrets (Phase 4)
# and personal data (Phase 15: "logs must redact personal and secret data") — an `email` or
# `display_name` field is exactly as unwelcome in a log line as a password is, even though it
# isn't a credential.
_SENSITIVE_KEYS = re.compile(
    r"(password|secret|token|credential|api[_-]?key|authorization|access[_-]?key|"
    r"connection[_-]?string|dsn|private[_-]?key|"
    r"email|display[_-]?name|full[_-]?name|phone[_-]?number)",
    re.IGNORECASE,
)

# Value shapes that look like a credential-bearing connection string / bearer token, or personal
# data, even if the key name didn't hint at it — e.g. `postgresql://user:pass@host/db`,
# `Bearer eyJ...`, or an email address embedded in free text (an error message that happened to
# interpolate `str(user)`, for instance).
_SENSITIVE_VALUE_PATTERNS = (
    re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^:\s]+:[^@\s]+@"),  # scheme://user:pass@
    re.compile(r"Bearer\s+[A-Za-z0-9\-_.]+", re.IGNORECASE),
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),  # email address
)


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        redacted = value
        for pattern in _SENSITIVE_VALUE_PATTERNS:
            redacted = pattern.sub(f"{_REDACTED} ", redacted)
        return redacted
    if isinstance(value, MutableMapping):
        return {k: _redact_processor_value(k, v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(v) for v in value]
    return value


def _redact_processor_value(key: str, value: Any) -> Any:
    if _SENSITIVE_KEYS.search(str(key)):
        return _REDACTED
    return _redact_value(value)


def _redact_processor(_logger: object, _method_name: str, event_dict: EventDict) -> EventDict:
    return {k: _redact_processor_value(k, v) for k, v in event_dict.items()}


def configure_logging(settings: Settings) -> None:
    """Configure stdlib logging + structlog once at process startup."""

    logging.basicConfig(
        format="%(message)s",
        level=settings.log_level,
    )

    # Note: structlog.stdlib.add_log_level / add_logger_name are for the *stdlib* logging
    # integration (they expect a real logging.Logger). This configuration uses
    # PrintLoggerFactory instead (see below), so we use the logger-agnostic
    # structlog.processors equivalents.
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _redact_processor,
    ]

    renderer: Processor = (
        structlog.processors.JSONRenderer()
        if settings.log_json
        else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[settings.log_level]
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.typing.FilteringBoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
