"""Mapping lifecycle service (Phase 7): draft -> pending_validation -> approved -> active ->
deprecated/rejected, plus drift-triggered suspension.

Mappings are immutable after activation (Phase 7 requirement 4): once a `MappingVersion` is
pointed to by `SchemaMapping.active_version_id`, no method here ever mutates that version's
`mapping_document` again. A change is always a *new* `MappingVersion` row (requirement 5),
created by `create_new_version`, which must independently go through validation and approval
before `activate()` can point `active_version_id` at it. Only an approved, active, non-drift-
suspended version is ever returned by `get_production_version` — the one query point
"production" code (Phase 8 feature extraction) is meant to use (requirement 6).
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from hermes_rpt.audit.enums import AuditOutcome
from hermes_rpt.audit.service import AuditService
from hermes_rpt.mappings.document import MappingDocument
from hermes_rpt.mappings.enums import MappingState
from hermes_rpt.mappings.models import MappingVersion, SchemaMapping
from hermes_rpt.mappings.repository import MappingVersionRepository, SchemaMappingRepository
from hermes_rpt.mappings.validation import validate_mapping_document
from hermes_rpt.ontology.registry import get_ontology
from hermes_rpt.schemas.introspection import SchemaIntrospectionResult
from hermes_rpt.schemas.models import SchemaSnapshot
from hermes_rpt.schemas.repository import SchemaSnapshotRepository
from hermes_rpt.tenants.context import TenantContext


class InvalidMappingStateTransitionError(Exception):
    def __init__(self, current: MappingState, action: str) -> None:
        super().__init__(f"Cannot {action} a mapping in state {current.value!r}")


class MappingNotProductionReadyError(Exception):
    pass


class MappingService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._mappings = SchemaMappingRepository(session)
        self._versions = MappingVersionRepository(session)
        self._snapshots = SchemaSnapshotRepository(session)
        self._audit = AuditService(session)

    async def create_draft(
        self,
        *,
        tenant_context: TenantContext,
        schema_snapshot_id: uuid.UUID,
        document: MappingDocument,
        ontology_version: str = "1",
        is_ai_suggested: bool = False,
        confidence: float | None = None,
        explanation: str | None = None,
    ) -> tuple[SchemaMapping, MappingVersion]:
        mapping = await self._mappings.add(
            SchemaMapping(
                entity_name=document.entity,
                schema_snapshot_id=schema_snapshot_id,
                ontology_version=ontology_version,
                state=MappingState.DRAFT,
            ),
            tenant_context=tenant_context,
        )
        version = await self._versions.add(
            MappingVersion(
                schema_mapping_id=mapping.id,
                version_number=1,
                mapping_document=document.model_dump(mode="json", by_alias=True),
                is_ai_suggested=is_ai_suggested,
                confidence=confidence,
                explanation=explanation,
                created_by_user_id=tenant_context.principal_id,
            ),
            tenant_context=tenant_context,
        )
        await self._audit.record(
            action="mapping.create_draft",
            outcome=AuditOutcome.SUCCESS,
            tenant_id=tenant_context.tenant_id,
            principal_id=tenant_context.principal_id,
            resource_type="SchemaMapping",
            resource_id=mapping.id,
            correlation_id=tenant_context.correlation_id,
            details={"entity": document.entity, "is_ai_suggested": is_ai_suggested},
        )
        return mapping, version

    async def create_new_version(
        self,
        mapping_id: uuid.UUID,
        *,
        tenant_context: TenantContext,
        document: MappingDocument,
        is_ai_suggested: bool = False,
        confidence: float | None = None,
        explanation: str | None = None,
    ) -> MappingVersion:
        """Never mutates an existing (especially the active) version — always inserts a new
        row. Resets the mapping's state to DRAFT: the previously active version, if any,
        remains in `active_version_id` and stays production-usable until this new version
        completes its own validate/approve/activate cycle."""

        mapping = await self._mappings.require(mapping_id, tenant_context=tenant_context)
        existing_versions = await self._versions.list_for_tenant(tenant_context=tenant_context)
        next_number = (
            max(
                (v.version_number for v in existing_versions if v.schema_mapping_id == mapping.id),
                default=0,
            )
            + 1
        )
        version = await self._versions.add(
            MappingVersion(
                schema_mapping_id=mapping.id,
                version_number=next_number,
                mapping_document=document.model_dump(mode="json", by_alias=True),
                is_ai_suggested=is_ai_suggested,
                confidence=confidence,
                explanation=explanation,
                created_by_user_id=tenant_context.principal_id,
            ),
            tenant_context=tenant_context,
        )
        mapping.state = MappingState.DRAFT
        await self._audit.record(
            action="mapping.create_new_version",
            outcome=AuditOutcome.SUCCESS,
            tenant_id=tenant_context.tenant_id,
            principal_id=tenant_context.principal_id,
            resource_type="SchemaMapping",
            resource_id=mapping.id,
            correlation_id=tenant_context.correlation_id,
            details={"version_number": next_number},
        )
        return version

    async def submit_for_validation(
        self, mapping_id: uuid.UUID, *, tenant_context: TenantContext
    ) -> SchemaMapping:
        mapping = await self._mappings.require(mapping_id, tenant_context=tenant_context)
        if mapping.state != MappingState.DRAFT:
            raise InvalidMappingStateTransitionError(mapping.state, "submit for validation")

        version = await self._latest_version_for(mapping, tenant_context=tenant_context)
        snapshot = await self._snapshots.require(
            mapping.schema_snapshot_id, tenant_context=tenant_context
        )
        document = MappingDocument.model_validate(version.mapping_document)
        ontology_entity = get_ontology(mapping.ontology_version).get_entity(mapping.entity_name)
        introspection = SchemaIntrospectionResult.model_validate(snapshot.metadata_document)
        table = next(
            (
                t
                for t in introspection.tables
                if t.qualified_name == f"{document.source.schema_name}.{document.source.table}"
            ),
            None,
        )
        available_columns = {c.name for c in table.columns} if table is not None else None

        validate_mapping_document(
            document,
            ontology_entity=ontology_entity,
            available_columns=available_columns,
        )

        mapping.state = MappingState.PENDING_VALIDATION
        await self._audit.record(
            action="mapping.submit_for_validation",
            outcome=AuditOutcome.SUCCESS,
            tenant_id=tenant_context.tenant_id,
            principal_id=tenant_context.principal_id,
            resource_type="SchemaMapping",
            resource_id=mapping.id,
            correlation_id=tenant_context.correlation_id,
        )
        return mapping

    async def approve(
        self, mapping_id: uuid.UUID, *, tenant_context: TenantContext
    ) -> SchemaMapping:
        mapping = await self._mappings.require(mapping_id, tenant_context=tenant_context)
        if mapping.state != MappingState.PENDING_VALIDATION:
            raise InvalidMappingStateTransitionError(mapping.state, "approve")

        version = await self._latest_version_for(mapping, tenant_context=tenant_context)
        version.approved_by_user_id = tenant_context.principal_id
        mapping.state = MappingState.APPROVED
        await self._audit.record(
            action="mapping.approve",
            outcome=AuditOutcome.SUCCESS,
            tenant_id=tenant_context.tenant_id,
            principal_id=tenant_context.principal_id,
            resource_type="SchemaMapping",
            resource_id=mapping.id,
            correlation_id=tenant_context.correlation_id,
        )
        return mapping

    async def activate(
        self, mapping_id: uuid.UUID, *, tenant_context: TenantContext
    ) -> SchemaMapping:
        mapping = await self._mappings.require(mapping_id, tenant_context=tenant_context)
        if mapping.state != MappingState.APPROVED:
            raise InvalidMappingStateTransitionError(mapping.state, "activate")

        version = await self._latest_version_for(mapping, tenant_context=tenant_context)
        mapping.active_version_id = version.id
        mapping.state = MappingState.ACTIVE
        mapping.suspended_due_to_drift = False  # a freshly activated version supersedes drift
        await self._audit.record(
            action="mapping.activate",
            outcome=AuditOutcome.SUCCESS,
            tenant_id=tenant_context.tenant_id,
            principal_id=tenant_context.principal_id,
            resource_type="SchemaMapping",
            resource_id=mapping.id,
            correlation_id=tenant_context.correlation_id,
            details={"active_version_id": str(version.id)},
        )
        return mapping

    async def deprecate(
        self, mapping_id: uuid.UUID, *, tenant_context: TenantContext
    ) -> SchemaMapping:
        mapping = await self._mappings.require(mapping_id, tenant_context=tenant_context)
        if mapping.state != MappingState.ACTIVE:
            raise InvalidMappingStateTransitionError(mapping.state, "deprecate")
        mapping.state = MappingState.DEPRECATED
        await self._audit.record(
            action="mapping.deprecate",
            outcome=AuditOutcome.SUCCESS,
            tenant_id=tenant_context.tenant_id,
            principal_id=tenant_context.principal_id,
            resource_type="SchemaMapping",
            resource_id=mapping.id,
            correlation_id=tenant_context.correlation_id,
        )
        return mapping

    async def reject(
        self, mapping_id: uuid.UUID, *, tenant_context: TenantContext
    ) -> SchemaMapping:
        mapping = await self._mappings.require(mapping_id, tenant_context=tenant_context)
        if mapping.state not in (MappingState.DRAFT, MappingState.PENDING_VALIDATION):
            raise InvalidMappingStateTransitionError(mapping.state, "reject")
        mapping.state = MappingState.REJECTED
        await self._audit.record(
            action="mapping.reject",
            outcome=AuditOutcome.SUCCESS,
            tenant_id=tenant_context.tenant_id,
            principal_id=tenant_context.principal_id,
            resource_type="SchemaMapping",
            resource_id=mapping.id,
            correlation_id=tenant_context.correlation_id,
        )
        return mapping

    async def get_production_version(
        self, mapping_id: uuid.UUID, *, tenant_context: TenantContext
    ) -> MappingVersion:
        """The one method Phase 8 feature extraction is meant to call — "only approved active
        mappings can be used in production" (Phase 7 requirement 6), enforced here rather than
        left for every caller to re-check."""

        mapping = await self._mappings.require(mapping_id, tenant_context=tenant_context)
        if (
            mapping.state != MappingState.ACTIVE
            or mapping.active_version_id is None
            or mapping.suspended_due_to_drift
        ):
            raise MappingNotProductionReadyError(
                f"Mapping {mapping_id} is not production-ready "
                f"(state={mapping.state.value}, suspended={mapping.suspended_due_to_drift})"
            )
        return await self._versions.require(
            mapping.active_version_id, tenant_context=tenant_context
        )

    async def suspend_affected_by_drift(
        self, snapshot: SchemaSnapshot, *, tenant_context: TenantContext
    ) -> list[SchemaMapping]:
        """Called after schema discovery completes with drift (Phase 5). Suspends — never
        rewrites — any ACTIVE mapping for this connection whose source table was removed,
        likely-renamed, or had a column/relationship change reported in the new snapshot's
        drift summary (Phase 7 requirement 7)."""

        if not snapshot.drift_summary:
            return []
        affected_tables = {event["table"] for event in snapshot.drift_summary.get("events", [])}
        if not affected_tables:
            return []

        all_mappings = await self._mappings.list_for_tenant(tenant_context=tenant_context)
        suspended: list[SchemaMapping] = []
        for mapping in all_mappings:
            if mapping.state != MappingState.ACTIVE or mapping.active_version_id is None:
                continue
            version = await self._versions.require(
                mapping.active_version_id, tenant_context=tenant_context
            )
            document = MappingDocument.model_validate(version.mapping_document)
            qualified_source = f"{document.source.schema_name}.{document.source.table}"
            if qualified_source in affected_tables:
                mapping.suspended_due_to_drift = True
                suspended.append(mapping)
                await self._audit.record(
                    action="mapping.suspend_due_to_drift",
                    outcome=AuditOutcome.SUCCESS,
                    tenant_id=tenant_context.tenant_id,
                    resource_type="SchemaMapping",
                    resource_id=mapping.id,
                    correlation_id=tenant_context.correlation_id,
                    details={"snapshot_id": str(snapshot.id), "source_table": qualified_source},
                )
        return suspended

    async def _latest_version_for(
        self, mapping: SchemaMapping, *, tenant_context: TenantContext
    ) -> MappingVersion:
        versions = [
            v
            for v in await self._versions.list_for_tenant(tenant_context=tenant_context)
            if v.schema_mapping_id == mapping.id
        ]
        return max(versions, key=lambda v: v.version_number)


__all__ = [
    "InvalidMappingStateTransitionError",
    "MappingNotProductionReadyError",
    "MappingService",
]
