"""Convenience service so every part of the platform records audit events the same way,
instead of constructing `AuditEvent` rows ad hoc at each call site."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from hermes_rpt.audit.enums import AuditOutcome
from hermes_rpt.audit.models import AuditEvent
from hermes_rpt.audit.repository import AuditEventRepository


class AuditService:
    def __init__(self, session: AsyncSession) -> None:
        self._events = AuditEventRepository(session)

    async def record(
        self,
        *,
        action: str,
        outcome: AuditOutcome,
        tenant_id: uuid.UUID | None = None,
        principal_id: uuid.UUID | None = None,
        principal_type: str = "unknown",
        resource_type: str | None = None,
        resource_id: uuid.UUID | None = None,
        correlation_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            tenant_id=tenant_id,
            principal_id=principal_id,
            principal_type=principal_type,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome=outcome,
            correlation_id=correlation_id,
            details=details or {},
        )
        return await self._events.add(event)
