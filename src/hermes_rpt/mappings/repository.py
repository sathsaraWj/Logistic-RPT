from __future__ import annotations

from sqlalchemy import and_, select

from hermes_rpt.common.repository import TenantScopedRepository
from hermes_rpt.mappings.enums import MappingState
from hermes_rpt.mappings.models import MappingVersion, SchemaMapping
from hermes_rpt.tenants.context import TenantContext


class SchemaMappingRepository(TenantScopedRepository[SchemaMapping]):
    model = SchemaMapping

    async def get_active_by_entity_name(
        self, entity_name: str, *, tenant_context: TenantContext
    ) -> SchemaMapping | None:
        """The mapping Phase 8 feature extraction resolves per ontology entity. Deliberately
        does not fall back to a non-active mapping — "only approved active mappings can be
        used in production" (Phase 7 requirement 6) applies here too."""

        stmt = select(SchemaMapping).where(
            and_(
                SchemaMapping.tenant_id == tenant_context.tenant_id,
                SchemaMapping.entity_name == entity_name,
                SchemaMapping.state == MappingState.ACTIVE,
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()


class MappingVersionRepository(TenantScopedRepository[MappingVersion]):
    model = MappingVersion
