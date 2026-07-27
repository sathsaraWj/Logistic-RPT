"""Model registry lifecycle (Phase 10/14): every new `ModelVersion` starts at `CANDIDATE` —
"do not promote a model automatically" — and only moves to `STAGING`/`PRODUCTION`/`ARCHIVED`
through an explicit call here, each one audited.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from hermes_rpt.audit.enums import AuditOutcome
from hermes_rpt.audit.service import AuditService
from hermes_rpt.registry.enums import ModelStage
from hermes_rpt.registry.models import ModelVersion
from hermes_rpt.registry.repository import ModelVersionRepository
from hermes_rpt.tenants.context import TenantContext

_VALID_TRANSITIONS: dict[ModelStage, frozenset[ModelStage]] = {
    ModelStage.CANDIDATE: frozenset({ModelStage.STAGING, ModelStage.ARCHIVED}),
    ModelStage.STAGING: frozenset(
        {ModelStage.PRODUCTION, ModelStage.CANDIDATE, ModelStage.ARCHIVED}
    ),
    ModelStage.PRODUCTION: frozenset({ModelStage.ARCHIVED, ModelStage.STAGING}),
    ModelStage.ARCHIVED: frozenset(),
}


class InvalidModelStageTransitionError(Exception):
    def __init__(self, current: ModelStage, target: ModelStage) -> None:
        super().__init__(f"Cannot move a model from {current.value!r} to {target.value!r}")


class ModelRegistryService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._models = ModelVersionRepository(session)
        self._audit = AuditService(session)

    async def register_candidate(self, model_version: ModelVersion) -> ModelVersion:
        """Persists a newly trained model at `CANDIDATE` — the only stage a fresh registration
        may start at, regardless of how good its evaluation metrics were."""

        model_version.stage = ModelStage.CANDIDATE
        registered = await self._models.add(model_version)
        await self._audit.record(
            action="model.register_candidate",
            outcome=AuditOutcome.SUCCESS,
            tenant_id=registered.tenant_id,
            resource_type="ModelVersion",
            resource_id=registered.id,
            details={"name": registered.name, "version_label": registered.version_label},
        )
        return registered

    async def transition_stage(
        self,
        model_version_id: uuid.UUID,
        *,
        to_stage: ModelStage,
        tenant_context: TenantContext | None = None,
    ) -> ModelVersion:
        model_version = await self._models.get(model_version_id)
        if model_version is None:
            raise ValueError(f"ModelVersion {model_version_id} not found")

        current = model_version.stage
        if to_stage not in _VALID_TRANSITIONS[current]:
            raise InvalidModelStageTransitionError(current, to_stage)

        model_version.stage = to_stage
        await self._audit.record(
            action="model.transition_stage",
            outcome=AuditOutcome.SUCCESS,
            tenant_id=model_version.tenant_id,
            principal_id=tenant_context.principal_id if tenant_context else None,
            resource_type="ModelVersion",
            resource_id=model_version.id,
            correlation_id=tenant_context.correlation_id if tenant_context else None,
            details={"from_stage": current.value, "to_stage": to_stage.value},
        )
        return model_version


__all__ = ["InvalidModelStageTransitionError", "ModelRegistryService"]
