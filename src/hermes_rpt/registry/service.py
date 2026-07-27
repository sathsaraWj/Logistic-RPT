"""Model registry lifecycle (Phase 10/14): every new `ModelVersion` starts at `CANDIDATE` —
"do not promote a model automatically" — and only moves to `STAGING`/`PRODUCTION`/`ARCHIVED`
through an explicit call here, each one audited.

Phase 14 adds the same lifecycle for `TenantModelAdapter`, plus governance on top of both:
compatibility checks (an adapter must match its base model's ontology/feature-contract
version), an approval gate on promoting/aliasing ("Promotion requires authorised approval" —
`ScopeName.MODEL_PROMOTE`, checked here rather than only at a router, since this phase has no
dedicated promotion endpoint), and `ModelAlias` (see `hermes_rpt.registry.models.ModelAlias` for
why this exists alongside `stage`).
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from hermes_rpt.audit.enums import AuditOutcome
from hermes_rpt.audit.service import AuditService
from hermes_rpt.auth.enums import ScopeName
from hermes_rpt.auth.errors import AuthorizationError
from hermes_rpt.registry.enums import ModelStage
from hermes_rpt.registry.models import ModelAlias, ModelVersion, TenantModelAdapter
from hermes_rpt.registry.repository import (
    ModelAliasRepository,
    ModelVersionRepository,
    TenantModelAdapterRepository,
)
from hermes_rpt.tenants.context import TenantContext

_VALID_TRANSITIONS: dict[ModelStage, frozenset[ModelStage]] = {
    ModelStage.CANDIDATE: frozenset({ModelStage.STAGING, ModelStage.ARCHIVED}),
    ModelStage.STAGING: frozenset(
        {ModelStage.PRODUCTION, ModelStage.CANDIDATE, ModelStage.ARCHIVED}
    ),
    ModelStage.PRODUCTION: frozenset({ModelStage.ARCHIVED, ModelStage.STAGING}),
    ModelStage.ARCHIVED: frozenset(),
}

# "Promotion requires authorised approval" — promoting *to* one of these stages is the
# high-stakes move that needs `ScopeName.MODEL_PROMOTE`; demoting/archiving does not (failing
# safe should never be gated behind an extra permission check).
_PROMOTION_STAGES = frozenset({ModelStage.STAGING, ModelStage.PRODUCTION})

_DEFAULT_ALIAS_NAME = "production"


class InvalidModelStageTransitionError(Exception):
    def __init__(self, current: ModelStage, target: ModelStage) -> None:
        super().__init__(f"Cannot move a model from {current.value!r} to {target.value!r}")


class IncompatibleAdapterError(Exception):
    """ "Incompatible ontology or feature versions must block deployment" (Phase 14) — raised
    when a `TenantModelAdapter` and the `ModelVersion` it's meant to sit on top of disagree on
    `ontology_version` or `feature_contract_version`."""

    def __init__(self, adapter: TenantModelAdapter, base: ModelVersion) -> None:
        super().__init__(
            f"Adapter {adapter.id} (ontology={adapter.ontology_version!r}, "
            f"feature_contract={adapter.feature_contract_version!r}) is not compatible with "
            f"base model {base.id} (ontology={base.ontology_version!r}, "
            f"feature_contract={base.feature_contract_version!r})"
        )


def _ensure_compatible(base: ModelVersion, adapter: TenantModelAdapter) -> None:
    if (
        base.ontology_version != adapter.ontology_version
        or base.feature_contract_version != adapter.feature_contract_version
    ):
        raise IncompatibleAdapterError(adapter, base)


def _require_promotion_authorization(tenant_context: TenantContext | None) -> None:
    """A `None` `tenant_context` means a trusted system/CLI caller (the same convention
    `register_candidate`/`transition_stage` already used pre-Phase-14, e.g. a training script
    registering a fresh `CANDIDATE`) — only an authenticated, scoped caller is actually checked.
    """

    if tenant_context is None:
        return
    if ScopeName.MODEL_PROMOTE.value not in tenant_context.scopes:
        raise AuthorizationError(
            "missing_scope:model:promote",
            tenant_id=tenant_context.tenant_id,
            principal_id=tenant_context.principal_id,
        )


class ModelRegistryService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._models = ModelVersionRepository(session)
        self._adapters = TenantModelAdapterRepository(session)
        self._aliases = ModelAliasRepository(session)
        self._audit = AuditService(session)

    # --- ModelVersion lifecycle -----------------------------------------------------------

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
        if to_stage in _PROMOTION_STAGES:
            _require_promotion_authorization(tenant_context)

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

    async def deactivate_model_version(
        self, model_version_id: uuid.UUID, *, tenant_context: TenantContext | None = None
    ) -> ModelVersion:
        """ "Model deactivation" (Phase 14) — immediately excludes the model from
        `ModelVersionRepository.get_production_model` (already filters `is_active`) without
        needing a stage transition; a deactivated model can be reactivated by the mirror-image
        call, but never silently starts serving again on its own."""

        model_version = await self._models.get(model_version_id)
        if model_version is None:
            raise ValueError(f"ModelVersion {model_version_id} not found")

        model_version.is_active = False
        await self._audit.record(
            action="model.deactivate",
            outcome=AuditOutcome.SUCCESS,
            tenant_id=model_version.tenant_id,
            principal_id=tenant_context.principal_id if tenant_context else None,
            resource_type="ModelVersion",
            resource_id=model_version.id,
            correlation_id=tenant_context.correlation_id if tenant_context else None,
        )
        return model_version

    # --- TenantModelAdapter lifecycle -----------------------------------------------------

    async def register_adapter_candidate(
        self,
        adapter: TenantModelAdapter,
        *,
        base_model: ModelVersion,
        tenant_context: TenantContext,
    ) -> TenantModelAdapter:
        """ "Training jobs must include trusted tenant context" — `tenant_context` is required
        (not optional, unlike the shared-model-capable `register_candidate`), and
        `TenantModelAdapterRepository.add` (a `TenantScopedRepository`) refuses to persist an
        adapter whose `tenant_id` doesn't match it. Also where "incompatible ontology or feature
        versions must block deployment" is actually enforced, at registration time rather than
        only at serving time."""

        _ensure_compatible(base_model, adapter)
        adapter.stage = ModelStage.CANDIDATE
        registered = await self._adapters.add(adapter, tenant_context=tenant_context)
        await self._audit.record(
            action="model.register_adapter_candidate",
            outcome=AuditOutcome.SUCCESS,
            tenant_id=tenant_context.tenant_id,
            principal_id=tenant_context.principal_id,
            resource_type="TenantModelAdapter",
            resource_id=registered.id,
            correlation_id=tenant_context.correlation_id,
            details={
                "base_model_version_id": str(base_model.id),
                "name": registered.name,
                "version_label": registered.version_label,
            },
        )
        return registered

    async def transition_adapter_stage(
        self,
        adapter_id: uuid.UUID,
        *,
        to_stage: ModelStage,
        tenant_context: TenantContext,
    ) -> TenantModelAdapter:
        adapter = await self._adapters.require(adapter_id, tenant_context=tenant_context)

        current = adapter.stage
        if to_stage not in _VALID_TRANSITIONS[current]:
            raise InvalidModelStageTransitionError(current, to_stage)
        if to_stage in _PROMOTION_STAGES:
            _require_promotion_authorization(tenant_context)

        adapter.stage = to_stage
        await self._audit.record(
            action="model.transition_adapter_stage",
            outcome=AuditOutcome.SUCCESS,
            tenant_id=tenant_context.tenant_id,
            principal_id=tenant_context.principal_id,
            resource_type="TenantModelAdapter",
            resource_id=adapter.id,
            correlation_id=tenant_context.correlation_id,
            details={"from_stage": current.value, "to_stage": to_stage.value},
        )
        return adapter

    async def deactivate_adapter(
        self, adapter_id: uuid.UUID, *, tenant_context: TenantContext
    ) -> TenantModelAdapter:
        adapter = await self._adapters.require(adapter_id, tenant_context=tenant_context)
        adapter.is_active = False
        await self._audit.record(
            action="model.deactivate_adapter",
            outcome=AuditOutcome.SUCCESS,
            tenant_id=tenant_context.tenant_id,
            principal_id=tenant_context.principal_id,
            resource_type="TenantModelAdapter",
            resource_id=adapter.id,
            correlation_id=tenant_context.correlation_id,
        )
        return adapter

    # --- Aliases: atomic pointer + rollback -----------------------------------------------

    async def set_alias(
        self,
        *,
        task_definition_id: uuid.UUID,
        model_version_id: uuid.UUID,
        tenant_model_adapter_id: uuid.UUID | None = None,
        alias_name: str = _DEFAULT_ALIAS_NAME,
        tenant_context: TenantContext | None = None,
    ) -> ModelAlias:
        return await self._point_alias(
            task_definition_id=task_definition_id,
            model_version_id=model_version_id,
            tenant_model_adapter_id=tenant_model_adapter_id,
            alias_name=alias_name,
            tenant_context=tenant_context,
            action="model.set_alias",
        )

    async def rollback_alias(
        self,
        *,
        task_definition_id: uuid.UUID,
        model_version_id: uuid.UUID,
        tenant_model_adapter_id: uuid.UUID | None = None,
        alias_name: str = _DEFAULT_ALIAS_NAME,
        tenant_context: TenantContext | None = None,
    ) -> ModelAlias:
        """Mechanically identical to `set_alias` — repointing *is* the rollback — but recorded
        under a distinct audit action so "roll this back" is distinguishable from "promote this
        forward" in the audit trail, and so a test can assert on the action name directly rather
        than inferring intent from the target version."""

        return await self._point_alias(
            task_definition_id=task_definition_id,
            model_version_id=model_version_id,
            tenant_model_adapter_id=tenant_model_adapter_id,
            alias_name=alias_name,
            tenant_context=tenant_context,
            action="model.rollback_alias",
        )

    async def _point_alias(
        self,
        *,
        task_definition_id: uuid.UUID,
        model_version_id: uuid.UUID,
        tenant_model_adapter_id: uuid.UUID | None,
        alias_name: str,
        tenant_context: TenantContext | None,
        action: str,
    ) -> ModelAlias:
        _require_promotion_authorization(tenant_context)

        model_version = await self._models.get(model_version_id)
        if model_version is None:
            raise ValueError(f"ModelVersion {model_version_id} not found")
        if not model_version.is_active or model_version.stage == ModelStage.ARCHIVED:
            raise ValueError(
                f"ModelVersion {model_version_id} is inactive or archived and cannot be aliased"
            )
        if model_version.tenant_id is not None and (
            tenant_context is None or model_version.tenant_id != tenant_context.tenant_id
        ):
            raise ValueError(
                f"ModelVersion {model_version_id} is not shared and does not belong to the "
                "aliasing tenant"
            )

        adapter: TenantModelAdapter | None = None
        if tenant_model_adapter_id is not None:
            if tenant_context is None:
                raise ValueError("A tenant-private adapter requires a tenant_context")
            adapter = await self._adapters.require(
                tenant_model_adapter_id, tenant_context=tenant_context
            )
            _ensure_compatible(model_version, adapter)

        alias_tenant_id = tenant_context.tenant_id if tenant_context is not None else None
        existing = await self._aliases.find(
            task_definition_id=task_definition_id, alias_name=alias_name, tenant_id=alias_tenant_id
        )
        if existing is not None:
            existing.model_version_id = model_version.id
            existing.tenant_model_adapter_id = adapter.id if adapter else None
            alias = existing
        else:
            alias = await self._aliases.add(
                ModelAlias(
                    tenant_id=alias_tenant_id,
                    task_definition_id=task_definition_id,
                    alias_name=alias_name,
                    model_version_id=model_version.id,
                    tenant_model_adapter_id=adapter.id if adapter else None,
                )
            )

        await self._audit.record(
            action=action,
            outcome=AuditOutcome.SUCCESS,
            tenant_id=alias_tenant_id,
            principal_id=tenant_context.principal_id if tenant_context else None,
            resource_type="ModelAlias",
            resource_id=alias.id,
            correlation_id=tenant_context.correlation_id if tenant_context else None,
            details={
                "alias_name": alias_name,
                "model_version_id": str(model_version.id),
                "tenant_model_adapter_id": str(adapter.id) if adapter else None,
            },
        )
        return alias

    async def resolve_alias(
        self,
        *,
        task_definition_id: uuid.UUID,
        tenant_context: TenantContext,
        alias_name: str = _DEFAULT_ALIAS_NAME,
    ) -> ModelAlias | None:
        return await self._aliases.resolve_for_tenant(
            task_definition_id=task_definition_id,
            alias_name=alias_name,
            tenant_context=tenant_context,
        )


__all__ = [
    "IncompatibleAdapterError",
    "InvalidModelStageTransitionError",
    "ModelRegistryService",
]
