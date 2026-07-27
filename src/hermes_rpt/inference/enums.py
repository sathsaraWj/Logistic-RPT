from __future__ import annotations

from enum import StrEnum


class PredictionStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
