"""Tenant-scoped monitoring summary (Phase 15) — "metrics must be tenant-scoped" satisfied the
same way every other tenant-facing read in this platform is: by querying tenant-scoped
repositories directly, not by reading the process-wide Prometheus counters in
`hermes_rpt.monitoring.metrics` (which exist for the separate, operator-only `/metrics` scrape
surface — see that module's docstring for why the two surfaces are safe to keep distinct).

Authentication failures are deliberately absent from `TenantMonitoringSummary` — they happen
*before* a tenant is ever resolved (`hermes_rpt.audit.models.AuditEvent.tenant_id` is `NULL` for
them), so there is no tenant to scope them to; they only ever appear on the operator-only
`/metrics` surface.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from hermes_rpt.audit.repository import AuditEventRepository
from hermes_rpt.inference.enums import PredictionStatus
from hermes_rpt.inference.repository import PredictionRequestRepository
from hermes_rpt.mappings.enums import MappingState
from hermes_rpt.mappings.repository import SchemaMappingRepository
from hermes_rpt.tenants.context import TenantContext


class TenantMonitoringSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    tenant_id: uuid.UUID
    prediction_requests_total: int
    prediction_requests_completed: int
    prediction_requests_failed: int
    authorization_denials: int
    cross_tenant_access_attempts: int
    active_mapping_count: int
    suspended_mapping_count: int


class MonitoringService:
    def __init__(self, session: AsyncSession) -> None:
        self._requests = PredictionRequestRepository(session)
        self._audit = AuditEventRepository(session)
        self._mappings = SchemaMappingRepository(session)

    async def tenant_summary(self, *, tenant_context: TenantContext) -> TenantMonitoringSummary:
        requests = await self._requests.list_for_tenant(tenant_context=tenant_context)
        audit_events = await self._audit.list_for_tenant(tenant_context=tenant_context)
        mappings = await self._mappings.list_for_tenant(tenant_context=tenant_context)

        return TenantMonitoringSummary(
            tenant_id=tenant_context.tenant_id,
            prediction_requests_total=len(requests),
            prediction_requests_completed=sum(
                1 for r in requests if r.status == PredictionStatus.COMPLETED
            ),
            prediction_requests_failed=sum(
                1 for r in requests if r.status == PredictionStatus.FAILED
            ),
            authorization_denials=sum(1 for e in audit_events if e.action == "authz.check"),
            cross_tenant_access_attempts=sum(
                1 for e in audit_events if e.action == "tenant.cross_tenant_access_attempt"
            ),
            active_mapping_count=sum(
                1
                for m in mappings
                if m.state == MappingState.ACTIVE and not m.suspended_due_to_drift
            ),
            suspended_mapping_count=sum(1 for m in mappings if m.suspended_due_to_drift),
        )
