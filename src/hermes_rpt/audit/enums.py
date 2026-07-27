from __future__ import annotations

from enum import StrEnum


class AuditOutcome(StrEnum):
    SUCCESS = "success"
    DENIED = "denied"
    ERROR = "error"
