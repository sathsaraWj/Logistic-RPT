from __future__ import annotations

from sqlalchemy import select

from hermes_rpt.audit.models import AuditEvent
from hermes_rpt.common.repository import BaseRepository
from hermes_rpt.tenants.context import TenantContext


class AuditEventRepository(BaseRepository[AuditEvent]):
    """Not a `TenantScopedRepository`: audit events may be created with no tenant at all
    (pre-auth failures), so writes cannot assume a TenantContext. Reads are still tenant-scoped
    via `list_for_tenant` — a tenant's audit trail never includes another tenant's rows, and
    never includes platform-level (`tenant_id IS NULL`) rows, which are Platform-Admin-only.
    """

    model = AuditEvent

    async def list_for_tenant(self, *, tenant_context: TenantContext) -> list[AuditEvent]:
        stmt = (
            select(AuditEvent)
            .where(AuditEvent.tenant_id == tenant_context.tenant_id)
            .order_by(AuditEvent.created_at.desc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
