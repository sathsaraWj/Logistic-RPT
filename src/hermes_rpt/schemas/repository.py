from __future__ import annotations

import uuid

from sqlalchemy import and_, func, select

from hermes_rpt.common.repository import TenantScopedRepository
from hermes_rpt.schemas.enums import DiscoveryStatus
from hermes_rpt.schemas.models import SchemaSnapshot
from hermes_rpt.tenants.context import TenantContext


class SchemaSnapshotRepository(TenantScopedRepository[SchemaSnapshot]):
    model = SchemaSnapshot

    async def next_sequence_number(
        self, connection_id: uuid.UUID, *, tenant_context: TenantContext
    ) -> int:
        stmt = select(func.max(SchemaSnapshot.sequence_number)).where(
            and_(
                SchemaSnapshot.tenant_id == tenant_context.tenant_id,
                SchemaSnapshot.connection_id == connection_id,
            )
        )
        result = await self.session.execute(stmt)
        current_max = result.scalar_one_or_none()
        return (current_max or 0) + 1

    async def list_for_connection(
        self, connection_id: uuid.UUID, *, tenant_context: TenantContext
    ) -> list[SchemaSnapshot]:
        stmt = (
            select(SchemaSnapshot)
            .where(
                and_(
                    SchemaSnapshot.tenant_id == tenant_context.tenant_id,
                    SchemaSnapshot.connection_id == connection_id,
                )
            )
            .order_by(SchemaSnapshot.sequence_number.desc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def latest_completed(
        self, connection_id: uuid.UUID, *, tenant_context: TenantContext, before_sequence: int
    ) -> SchemaSnapshot | None:
        stmt = (
            select(SchemaSnapshot)
            .where(
                and_(
                    SchemaSnapshot.tenant_id == tenant_context.tenant_id,
                    SchemaSnapshot.connection_id == connection_id,
                    SchemaSnapshot.status == DiscoveryStatus.COMPLETED,
                    SchemaSnapshot.sequence_number < before_sequence,
                )
            )
            .order_by(SchemaSnapshot.sequence_number.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()
