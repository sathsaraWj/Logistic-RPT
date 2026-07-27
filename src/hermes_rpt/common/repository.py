"""Generic repository base classes.

API handlers must never query ORM models directly (Phase 2 requirement 3) — they go through a
repository (or a service that wraps one). `TenantScopedRepository` is the single place that
applies the `tenant_id` filter for tenant-owned entities, so that filter cannot be forgotten at
an individual call site. It also refuses to touch a row whose `tenant_id` does not match the
caller's `TenantContext`, even if the row's primary key was guessed or supplied correctly —
"reject mismatched tenant and resource IDs" (Phase 2 requirement 6).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hermes_rpt.common.db import Base
from hermes_rpt.tenants.context import TenantContext


class TenantMismatchError(Exception):
    """Raised when a resource's tenant_id does not match the trusted TenantContext.

    This is treated as "not found", not "forbidden", at the API boundary (Phase 3) — the
    platform must not confirm to a caller that a resource belonging to another tenant exists.
    """


class TenantContextRequiredError(Exception):
    """Raised when a tenant-owned operation is attempted without a TenantContext at all."""


class BaseRepository[ModelT: Base]:
    """Repository for entities that are NOT tenant-owned (e.g. Tenant itself, Role,
    PredictionTaskDefinition, shared ModelVersion rows). No implicit tenant filtering happens
    here — callers (services, Phase 3 authorization) are responsible for deciding who may call
    these methods at all."""

    model: type[ModelT]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, entity_id: uuid.UUID) -> ModelT | None:
        return await self.session.get(self.model, entity_id)

    async def add(self, entity: ModelT) -> ModelT:
        self.session.add(entity)
        await self.session.flush()
        return entity

    async def delete(self, entity: ModelT) -> None:
        await self.session.delete(entity)
        await self.session.flush()


class TenantScopedRepository[ModelT: Base](BaseRepository[ModelT]):
    """Repository for tenant-owned entities. Every read/write is scoped to
    `tenant_context.tenant_id`; there is no method on this class that can return or mutate a
    row belonging to a different tenant.
    """

    async def get(  # type: ignore[override]
        self, entity_id: uuid.UUID, *, tenant_context: TenantContext
    ) -> ModelT | None:
        entity = await self.session.get(self.model, entity_id)
        if entity is None:
            return None
        if entity.tenant_id != tenant_context.tenant_id:  # type: ignore[attr-defined]
            # Deliberately indistinguishable from "not found" to the caller — see
            # TenantMismatchError docstring.
            return None
        return entity

    async def require(self, entity_id: uuid.UUID, *, tenant_context: TenantContext) -> ModelT:
        entity = await self.get(entity_id, tenant_context=tenant_context)
        if entity is None:
            raise TenantMismatchError(
                f"{self.model.__name__} {entity_id} not found for tenant {tenant_context.tenant_id}"
            )
        return entity

    async def list_for_tenant(self, *, tenant_context: TenantContext) -> list[ModelT]:
        stmt = select(self.model).where(
            self.model.tenant_id == tenant_context.tenant_id  # type: ignore[attr-defined]
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def add(self, entity: ModelT, *, tenant_context: TenantContext) -> ModelT:  # type: ignore[override]
        entity_tenant_id = getattr(entity, "tenant_id", None)
        if entity_tenant_id is None:
            entity.tenant_id = tenant_context.tenant_id  # type: ignore[attr-defined]
        elif entity_tenant_id != tenant_context.tenant_id:
            raise TenantMismatchError(
                f"Refusing to create {self.model.__name__} for tenant {entity_tenant_id} "
                f"under TenantContext for tenant {tenant_context.tenant_id}"
            )
        return await super().add(entity)

    async def delete(self, entity: ModelT, *, tenant_context: TenantContext) -> None:  # type: ignore[override]
        if entity.tenant_id != tenant_context.tenant_id:  # type: ignore[attr-defined]
            raise TenantMismatchError(
                f"Refusing to delete {self.model.__name__} belonging to tenant "
                f"{entity.tenant_id} under TenantContext for tenant {tenant_context.tenant_id}"  # type: ignore[attr-defined]
            )
        await super().delete(entity)
