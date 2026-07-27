from __future__ import annotations

from hermes_rpt.common.repository import BaseRepository, TenantScopedRepository
from hermes_rpt.inference.models import (
    PredictionRequest,
    PredictionResult,
    PredictionTaskDefinition,
)


class PredictionTaskDefinitionRepository(BaseRepository[PredictionTaskDefinition]):
    """Not tenant-owned — this is the shared, platform-level task catalogue."""

    model = PredictionTaskDefinition


class PredictionRequestRepository(TenantScopedRepository[PredictionRequest]):
    model = PredictionRequest


class PredictionResultRepository(TenantScopedRepository[PredictionResult]):
    model = PredictionResult
