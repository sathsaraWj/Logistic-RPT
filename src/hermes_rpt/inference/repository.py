from __future__ import annotations

import uuid

from sqlalchemy import and_, select

from hermes_rpt.common.repository import BaseRepository, TenantScopedRepository
from hermes_rpt.inference.models import (
    PredictionRequest,
    PredictionResult,
    PredictionTaskDefinition,
)
from hermes_rpt.tenants.context import TenantContext


class PredictionTaskDefinitionRepository(BaseRepository[PredictionTaskDefinition]):
    """Not tenant-owned — this is the shared, platform-level task catalogue."""

    model = PredictionTaskDefinition

    async def get_by_task_key(self, task_key: str) -> PredictionTaskDefinition | None:
        stmt = select(PredictionTaskDefinition).where(PredictionTaskDefinition.task_key == task_key)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()


class PredictionRequestRepository(TenantScopedRepository[PredictionRequest]):
    model = PredictionRequest

    async def get_by_idempotency_key(
        self, idempotency_key: str, *, tenant_context: TenantContext
    ) -> PredictionRequest | None:
        stmt = select(PredictionRequest).where(
            and_(
                PredictionRequest.tenant_id == tenant_context.tenant_id,
                PredictionRequest.idempotency_key == idempotency_key,
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()


class PredictionResultRepository(TenantScopedRepository[PredictionResult]):
    model = PredictionResult

    async def get_by_request_id(
        self, prediction_request_id: uuid.UUID, *, tenant_context: TenantContext
    ) -> PredictionResult | None:
        stmt = select(PredictionResult).where(
            and_(
                PredictionResult.tenant_id == tenant_context.tenant_id,
                PredictionResult.prediction_request_id == prediction_request_id,
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()
