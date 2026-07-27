"""Mapping resolver (Phase 8): finds the active, production-ready `MappingDocument` for the
target entity and for each related entity a feature contract references, per tenant.

A missing mapping for a *related* entity is not an error — "do not assume all customers have
every feature" (Phase 8 requirement) — it just means every feature depending on that entity
comes back missing (`hermes_rpt.features.normalizer` applies each feature's declared
`missing_value_behavior`). A missing mapping for the *target* entity is fatal: there is nothing
to extract features for at all.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from hermes_rpt.mappings.document import MappingDocument
from hermes_rpt.mappings.models import SchemaMapping
from hermes_rpt.mappings.repository import MappingVersionRepository, SchemaMappingRepository
from hermes_rpt.tenants.context import TenantContext


class TargetMappingUnavailableError(Exception):
    def __init__(self, entity_name: str) -> None:
        super().__init__(f"No active production mapping for target entity {entity_name!r}")


@dataclass(frozen=True, slots=True)
class ResolvedMapping:
    schema_mapping: SchemaMapping
    document: MappingDocument


class MappingResolver:
    def __init__(self, session: AsyncSession) -> None:
        self._mappings = SchemaMappingRepository(session)
        self._versions = MappingVersionRepository(session)

    async def resolve(
        self, entity_name: str, *, tenant_context: TenantContext
    ) -> ResolvedMapping | None:
        mapping = await self._mappings.get_active_by_entity_name(
            entity_name, tenant_context=tenant_context
        )
        if mapping is None or mapping.active_version_id is None or mapping.suspended_due_to_drift:
            return None
        version = await self._versions.get(mapping.active_version_id, tenant_context=tenant_context)
        if version is None:
            return None
        document = MappingDocument.model_validate(version.mapping_document)
        return ResolvedMapping(schema_mapping=mapping, document=document)

    async def resolve_target(
        self, entity_name: str, *, tenant_context: TenantContext
    ) -> ResolvedMapping:
        resolved = await self.resolve(entity_name, tenant_context=tenant_context)
        if resolved is None:
            raise TargetMappingUnavailableError(entity_name)
        return resolved
