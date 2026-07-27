"""Repositories for the model registry.

`ModelVersionRepository` is intentionally not a plain `TenantScopedRepository`: a `ModelVersion`
with `tenant_id IS NULL` is a shared base model visible to every tenant, while one with a
`tenant_id` set is private to that tenant. `list_available_for_tenant` is the one place that
"shared OR mine, never someone else's" rule is implemented — see
docs/adr/0004-per-tenant-read-only-connection-pools.md's sibling isolation principle applied to
models instead of connections, and Phase 14's "Tenant A cannot load Tenant B's adapter"
requirement, which `TenantModelAdapterRepository` (an ordinary `TenantScopedRepository`)
enforces the same way every other tenant-owned entity does.
"""

from __future__ import annotations

import uuid

from sqlalchemy import and_, or_, select

from hermes_rpt.common.repository import BaseRepository, TenantMismatchError, TenantScopedRepository
from hermes_rpt.registry.enums import ModelStage
from hermes_rpt.registry.models import ModelAlias, ModelVersion, TenantModelAdapter
from hermes_rpt.tenants.context import TenantContext


class ModelVersionRepository(BaseRepository[ModelVersion]):
    model = ModelVersion

    async def list_available_for_tenant(
        self, *, tenant_context: TenantContext
    ) -> list[ModelVersion]:
        stmt = select(ModelVersion).where(
            or_(
                ModelVersion.tenant_id.is_(None),
                ModelVersion.tenant_id == tenant_context.tenant_id,
            )
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_available_for_tenant(
        self, model_version_id: uuid.UUID, *, tenant_context: TenantContext
    ) -> ModelVersion | None:
        model_version = await self.get(model_version_id)
        if model_version is None:
            return None
        owner_id = model_version.tenant_id
        if owner_id is not None and owner_id != tenant_context.tenant_id:
            # Same "looks like not-found, not forbidden" rule as TenantMismatchError.
            return None
        return model_version

    async def require_available_for_tenant(
        self, model_version_id: uuid.UUID, *, tenant_context: TenantContext
    ) -> ModelVersion:
        model_version = await self.get_available_for_tenant(
            model_version_id, tenant_context=tenant_context
        )
        if model_version is None:
            raise TenantMismatchError(
                f"ModelVersion {model_version_id} not available for tenant "
                f"{tenant_context.tenant_id}"
            )
        return model_version

    async def get_production_model(
        self, task_definition_id: uuid.UUID, *, tenant_context: TenantContext
    ) -> ModelVersion | None:
        """The model an inference request actually serves: `PRODUCTION`-staged, and either
        private to this tenant or shared — a tenant-private `PRODUCTION` model always wins over
        a shared one for the same task, mirroring "Shared Hermes-RPT base + tenant-specific
        private adapter" (Phase 14) being the more specific choice."""

        stmt = select(ModelVersion).where(
            and_(
                ModelVersion.task_definition_id == task_definition_id,
                ModelVersion.stage == ModelStage.PRODUCTION,
                ModelVersion.is_active.is_(True),
                or_(
                    ModelVersion.tenant_id.is_(None),
                    ModelVersion.tenant_id == tenant_context.tenant_id,
                ),
            )
        )
        result = await self.session.execute(stmt)
        candidates = list(result.scalars().all())
        tenant_private = [m for m in candidates if m.tenant_id == tenant_context.tenant_id]
        if tenant_private:
            return tenant_private[0]
        return candidates[0] if candidates else None


class TenantModelAdapterRepository(TenantScopedRepository[TenantModelAdapter]):
    model = TenantModelAdapter

    async def get_active_for_base_model(
        self, base_model_version_id: uuid.UUID, *, tenant_context: TenantContext
    ) -> TenantModelAdapter | None:
        """The one adapter a Phase 14 serving path would combine with a shared base model for
        this tenant: `PRODUCTION`-staged, active, and — since this is `TenantScopedRepository` —
        already structurally impossible to be another tenant's row (Phase 14's "Tenant A cannot
        load Tenant B's adapter")."""

        stmt = select(TenantModelAdapter).where(
            and_(
                TenantModelAdapter.base_model_version_id == base_model_version_id,
                TenantModelAdapter.tenant_id == tenant_context.tenant_id,
                TenantModelAdapter.stage == ModelStage.PRODUCTION,
                TenantModelAdapter.is_active.is_(True),
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()


class ModelAliasRepository(BaseRepository[ModelAlias]):
    model = ModelAlias

    async def find(
        self, *, task_definition_id: uuid.UUID, alias_name: str, tenant_id: uuid.UUID | None
    ) -> ModelAlias | None:
        tenant_clause = (
            ModelAlias.tenant_id == tenant_id
            if tenant_id is not None
            else ModelAlias.tenant_id.is_(None)
        )
        stmt = select(ModelAlias).where(
            and_(
                ModelAlias.task_definition_id == task_definition_id,
                ModelAlias.alias_name == alias_name,
                tenant_clause,
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def resolve_for_tenant(
        self, *, task_definition_id: uuid.UUID, alias_name: str, tenant_context: TenantContext
    ) -> ModelAlias | None:
        """A tenant-private alias wins over the shared one of the same name — same precedence
        rule as `ModelVersionRepository.get_production_model`."""

        tenant_specific = await self.find(
            task_definition_id=task_definition_id,
            alias_name=alias_name,
            tenant_id=tenant_context.tenant_id,
        )
        if tenant_specific is not None:
            return tenant_specific
        return await self.find(
            task_definition_id=task_definition_id, alias_name=alias_name, tenant_id=None
        )
