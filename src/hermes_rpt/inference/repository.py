from __future__ import annotations

import uuid

from sqlalchemy import and_, select

from hermes_rpt.common.repository import BaseRepository, TenantScopedRepository
from hermes_rpt.inference.models import (
    PredictionOutcome,
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


class PredictionOutcomeRepository(TenantScopedRepository[PredictionOutcome]):
    model = PredictionOutcome

    async def get_by_prediction_result_id(
        self, prediction_result_id: uuid.UUID, *, tenant_context: TenantContext
    ) -> PredictionOutcome | None:
        stmt = select(PredictionOutcome).where(
            and_(
                PredictionOutcome.tenant_id == tenant_context.tenant_id,
                PredictionOutcome.prediction_result_id == prediction_result_id,
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_labeled_results_for_model(
        self, model_version_id: uuid.UUID, *, tenant_context: TenantContext
    ) -> list[tuple[PredictionResult, PredictionOutcome]]:
        """Every `(result, outcome)` pair this tenant has for a given model version — the input
        `MonitoringService.compute_realized_model_metrics` needs for precision/recall/calibration
        drift. Tenant-scoped on both sides of the join, not just the outcome table."""

        stmt = (
            select(PredictionResult, PredictionOutcome)
            .join(
                PredictionOutcome,
                PredictionOutcome.prediction_result_id == PredictionResult.id,
            )
            .where(
                and_(
                    PredictionResult.tenant_id == tenant_context.tenant_id,
                    PredictionOutcome.tenant_id == tenant_context.tenant_id,
                    PredictionResult.model_version_id == model_version_id,
                )
            )
        )
        result = await self.session.execute(stmt)
        return [(row.PredictionResult, row.PredictionOutcome) for row in result]


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
