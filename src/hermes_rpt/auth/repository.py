"""Repository for `ServiceCredential` rows."""

from __future__ import annotations

from sqlalchemy import select

from hermes_rpt.auth.models import ServiceCredential
from hermes_rpt.common.repository import TenantScopedRepository


class ServiceCredentialRepository(TenantScopedRepository[ServiceCredential]):
    model = ServiceCredential

    async def find_by_client_id(self, client_id: str) -> ServiceCredential | None:
        """The one deliberately tenant-unscoped read on this repository. The token-exchange
        flow has no `TenantContext` yet — resolving *which* tenant a `client_id` belongs to is
        the entire point of this lookup, so it can't itself require a `TenantContext`. Safe only
        because `client_id` is globally unique (DB `unique=True`, not just an index) — this
        never returns more than one tenant's row for a given input."""

        stmt = select(ServiceCredential).where(ServiceCredential.client_id == client_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()
