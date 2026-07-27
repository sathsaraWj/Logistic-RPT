"""Mapping lifecycle tests (Phase 7): state transitions, immutability after activation,
production-readiness gating, drift suspension, and tenant isolation — exercised against Tenant
Alpha's and Tenant Beta's differently-shaped schemas.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from hermes_rpt.connectors.enums import DatabaseEngine
from hermes_rpt.connectors.pool_registry import TenantConnectionPoolRegistry
from hermes_rpt.connectors.service import ConnectionLifecycleManager
from hermes_rpt.mappings.document import FieldMapping, MappingDocument, SourceTable, ValueSource
from hermes_rpt.mappings.enums import MappingState
from hermes_rpt.mappings.service import (
    InvalidMappingStateTransitionError,
    MappingNotProductionReadyError,
    MappingService,
)
from hermes_rpt.schemas.enums import DiscoveryStatus
from hermes_rpt.schemas.introspection import (
    ColumnMetadata,
    SchemaIntrospectionResult,
    TableMetadata,
)
from hermes_rpt.schemas.models import SchemaSnapshot
from hermes_rpt.schemas.repository import SchemaSnapshotRepository
from hermes_rpt.secrets.provider import LocalDevSecretProvider
from hermes_rpt.tenants.context import TenantContext
from hermes_rpt.tenants.repository import TenantRepository, UserRepository
from tests.factories import make_tenant, make_user
from tests.fakes import FakeConnector

_ALPHA_TABLE = TableMetadata(
    schema_name="public",
    name="fleet_vehicle",
    kind="table",
    columns=[
        ColumnMetadata(
            name="vehicle_id", sql_type="integer", is_nullable=False, ordinal_position=1
        ),
        ColumnMetadata(
            name="registration_no",
            sql_type="character varying",
            is_nullable=False,
            ordinal_position=2,
        ),
        ColumnMetadata(
            name="odometer_km", sql_type="numeric", is_nullable=False, ordinal_position=3
        ),
    ],
    primary_key_columns=["vehicle_id"],
)

_BETA_TABLE = TableMetadata(
    schema_name="public",
    name="assets",
    kind="table",
    columns=[
        ColumnMetadata(name="asset_id", sql_type="integer", is_nullable=False, ordinal_position=1),
        ColumnMetadata(
            name="tag", sql_type="character varying", is_nullable=False, ordinal_position=2
        ),
        ColumnMetadata(name="total_km", sql_type="numeric", is_nullable=False, ordinal_position=3),
    ],
    primary_key_columns=["asset_id"],
)


def _alpha_document() -> MappingDocument:
    return MappingDocument(
        entity="Vehicle",
        source=SourceTable(schema="public", table="fleet_vehicle"),
        identity={"vehicle_id": FieldMapping(sources=(ValueSource(column="vehicle_id"),))},
        fields={
            "registration_number": FieldMapping(sources=(ValueSource(column="registration_no"),)),
            "current_odometer_km": FieldMapping(sources=(ValueSource(column="odometer_km"),)),
            "is_active": FieldMapping(sources=(ValueSource(static=True),)),
        },
    )


def _beta_document() -> MappingDocument:
    return MappingDocument(
        entity="Vehicle",
        source=SourceTable(schema="public", table="assets"),
        identity={"vehicle_id": FieldMapping(sources=(ValueSource(column="asset_id"),))},
        fields={
            "registration_number": FieldMapping(sources=(ValueSource(column="tag"),)),
            "current_odometer_km": FieldMapping(sources=(ValueSource(column="total_km"),)),
            "is_active": FieldMapping(sources=(ValueSource(static=True),)),
        },
    )


async def _tenant_context(session: AsyncSession, *, slug: str) -> TenantContext:
    tenant = await TenantRepository(session).add(make_tenant(name=slug, slug=slug))
    user = await UserRepository(session).add(make_user(email=f"{slug}@example.com"))
    await session.commit()
    return TenantContext(tenant_id=tenant.id, principal_id=user.id)


async def _completed_snapshot(
    session: AsyncSession, *, tenant_context: TenantContext, table: TableMetadata
) -> SchemaSnapshot:
    connector = FakeConnector()
    manager = ConnectionLifecycleManager(
        session,
        secret_provider=LocalDevSecretProvider(),
        pool_registry=TenantConnectionPoolRegistry(connector),
        connector=connector,
    )
    connection = await manager.register_connection(
        tenant_context=tenant_context,
        name="db",
        engine=DatabaseEngine.POSTGRESQL,
        host="db.internal",
        port=5432,
        database_name="tenant_db",
        username="app",
        secret_value="unit-test-secret",  # noqa: S106
        schema_allowlist=["public"],
    )
    await session.commit()

    introspection = SchemaIntrospectionResult(schemas=["public"], tables=[table])
    snapshot = await SchemaSnapshotRepository(session).add(
        SchemaSnapshot(
            connection_id=connection.id,
            sequence_number=1,
            status=DiscoveryStatus.COMPLETED,
            metadata_document=introspection.model_dump(mode="json"),
        ),
        tenant_context=tenant_context,
    )
    await session.commit()
    return snapshot


async def test_full_lifecycle_alpha(session: AsyncSession) -> None:
    ctx = await _tenant_context(session, slug="mapping-alpha")
    snapshot = await _completed_snapshot(session, tenant_context=ctx, table=_ALPHA_TABLE)
    service = MappingService(session)

    mapping, _version = await service.create_draft(
        tenant_context=ctx, schema_snapshot_id=snapshot.id, document=_alpha_document()
    )
    await session.commit()
    assert mapping.state == MappingState.DRAFT

    mapping = await service.submit_for_validation(mapping.id, tenant_context=ctx)
    await session.commit()
    assert mapping.state == MappingState.PENDING_VALIDATION

    mapping = await service.approve(mapping.id, tenant_context=ctx)
    await session.commit()
    assert mapping.state == MappingState.APPROVED

    with pytest.raises(MappingNotProductionReadyError):
        await service.get_production_version(mapping.id, tenant_context=ctx)

    mapping = await service.activate(mapping.id, tenant_context=ctx)
    await session.commit()
    assert mapping.state == MappingState.ACTIVE
    assert mapping.active_version_id is not None

    production = await service.get_production_version(mapping.id, tenant_context=ctx)
    document = MappingDocument.model_validate(production.mapping_document)
    assert document.source.table == "fleet_vehicle"


async def test_full_lifecycle_beta_with_different_schema(session: AsyncSession) -> None:
    """Same entity (Vehicle), same lifecycle, completely different source column names —
    proves the mapping lifecycle itself is schema-agnostic."""

    ctx = await _tenant_context(session, slug="mapping-beta")
    snapshot = await _completed_snapshot(session, tenant_context=ctx, table=_BETA_TABLE)
    service = MappingService(session)

    mapping, _version = await service.create_draft(
        tenant_context=ctx, schema_snapshot_id=snapshot.id, document=_beta_document()
    )
    await session.commit()
    mapping = await service.submit_for_validation(mapping.id, tenant_context=ctx)
    mapping = await service.approve(mapping.id, tenant_context=ctx)
    mapping = await service.activate(mapping.id, tenant_context=ctx)
    await session.commit()

    production = await service.get_production_version(mapping.id, tenant_context=ctx)
    document = MappingDocument.model_validate(production.mapping_document)
    assert document.fields["current_odometer_km"].sources[0].column == "total_km"


async def test_invalid_document_fails_validation_and_stays_draft(session: AsyncSession) -> None:
    ctx = await _tenant_context(session, slug="mapping-invalid")
    snapshot = await _completed_snapshot(session, tenant_context=ctx, table=_ALPHA_TABLE)
    service = MappingService(session)

    broken_document = MappingDocument(
        entity="Vehicle",
        source=SourceTable(schema="public", table="fleet_vehicle"),
        identity={"vehicle_id": FieldMapping(sources=(ValueSource(column="does_not_exist"),))},
    )
    mapping, _version = await service.create_draft(
        tenant_context=ctx, schema_snapshot_id=snapshot.id, document=broken_document
    )
    await session.commit()

    from hermes_rpt.mappings.repository import SchemaMappingRepository
    from hermes_rpt.mappings.validation import MappingValidationError

    with pytest.raises(MappingValidationError):
        await service.submit_for_validation(mapping.id, tenant_context=ctx)

    refreshed = await SchemaMappingRepository(session).require(mapping.id, tenant_context=ctx)
    assert refreshed.state == MappingState.DRAFT


async def test_cannot_activate_without_approval(session: AsyncSession) -> None:
    ctx = await _tenant_context(session, slug="mapping-skip-approval")
    snapshot = await _completed_snapshot(session, tenant_context=ctx, table=_ALPHA_TABLE)
    service = MappingService(session)

    mapping, _version = await service.create_draft(
        tenant_context=ctx, schema_snapshot_id=snapshot.id, document=_alpha_document()
    )
    await session.commit()

    with pytest.raises(InvalidMappingStateTransitionError):
        await service.activate(mapping.id, tenant_context=ctx)


async def test_new_version_does_not_mutate_the_active_version(session: AsyncSession) -> None:
    ctx = await _tenant_context(session, slug="mapping-new-version")
    snapshot = await _completed_snapshot(session, tenant_context=ctx, table=_ALPHA_TABLE)
    service = MappingService(session)

    mapping, _v1 = await service.create_draft(
        tenant_context=ctx, schema_snapshot_id=snapshot.id, document=_alpha_document()
    )
    await session.commit()
    mapping = await service.submit_for_validation(mapping.id, tenant_context=ctx)
    mapping = await service.approve(mapping.id, tenant_context=ctx)
    mapping = await service.activate(mapping.id, tenant_context=ctx)
    await session.commit()
    active_version_id = mapping.active_version_id
    assert active_version_id is not None

    revised_document = _alpha_document()
    await service.create_new_version(mapping.id, tenant_context=ctx, document=revised_document)
    await session.commit()

    # The mapping "slot" shows a new draft in progress...
    from hermes_rpt.mappings.repository import SchemaMappingRepository

    refreshed = await SchemaMappingRepository(session).require(mapping.id, tenant_context=ctx)
    assert refreshed.state == MappingState.DRAFT
    # ...but the OLD active version is untouched and still what production reads, because
    # active_version_id still points at it and get_production_version only ever looks there.
    assert refreshed.active_version_id == active_version_id


async def test_deprecate_requires_active_state(session: AsyncSession) -> None:
    ctx = await _tenant_context(session, slug="mapping-deprecate")
    snapshot = await _completed_snapshot(session, tenant_context=ctx, table=_ALPHA_TABLE)
    service = MappingService(session)
    mapping, _version = await service.create_draft(
        tenant_context=ctx, schema_snapshot_id=snapshot.id, document=_alpha_document()
    )
    await session.commit()

    with pytest.raises(InvalidMappingStateTransitionError):
        await service.deprecate(mapping.id, tenant_context=ctx)


async def test_reject_from_draft(session: AsyncSession) -> None:
    ctx = await _tenant_context(session, slug="mapping-reject")
    snapshot = await _completed_snapshot(session, tenant_context=ctx, table=_ALPHA_TABLE)
    service = MappingService(session)
    mapping, _version = await service.create_draft(
        tenant_context=ctx, schema_snapshot_id=snapshot.id, document=_alpha_document()
    )
    await session.commit()

    mapping = await service.reject(mapping.id, tenant_context=ctx)
    await session.commit()
    assert mapping.state == MappingState.REJECTED


async def test_drift_suspends_active_mapping_without_changing_its_state(
    session: AsyncSession,
) -> None:
    ctx = await _tenant_context(session, slug="mapping-drift")
    snapshot = await _completed_snapshot(session, tenant_context=ctx, table=_ALPHA_TABLE)
    service = MappingService(session)

    mapping, _version = await service.create_draft(
        tenant_context=ctx, schema_snapshot_id=snapshot.id, document=_alpha_document()
    )
    await session.commit()
    mapping = await service.submit_for_validation(mapping.id, tenant_context=ctx)
    mapping = await service.approve(mapping.id, tenant_context=ctx)
    mapping = await service.activate(mapping.id, tenant_context=ctx)
    await session.commit()
    assert mapping.suspended_due_to_drift is False

    drifted_snapshot = SchemaSnapshot(
        tenant_id=ctx.tenant_id,
        connection_id=snapshot.connection_id,
        sequence_number=2,
        status=DiscoveryStatus.COMPLETED,
        metadata_document={},
        drift_summary={
            "events": [
                {
                    "event_type": "column_removed",
                    "table": "public.fleet_vehicle",
                    "detail": "Column 'odometer_km' removed",
                }
            ]
        },
    )

    suspended = await service.suspend_affected_by_drift(drifted_snapshot, tenant_context=ctx)
    await session.commit()

    assert len(suspended) == 1
    assert suspended[0].id == mapping.id
    # Suspension is a flag, never a state rewrite — "cannot automatically rewrite" (Phase 7).
    assert suspended[0].state == MappingState.ACTIVE
    assert suspended[0].suspended_due_to_drift is True

    with pytest.raises(MappingNotProductionReadyError):
        await service.get_production_version(mapping.id, tenant_context=ctx)


async def test_mappings_are_tenant_isolated(session: AsyncSession) -> None:
    from hermes_rpt.common.repository import TenantMismatchError

    ctx_a = await _tenant_context(session, slug="mapping-iso-a")
    ctx_b = await _tenant_context(session, slug="mapping-iso-b")
    snapshot_a = await _completed_snapshot(session, tenant_context=ctx_a, table=_ALPHA_TABLE)
    service = MappingService(session)

    mapping, _version = await service.create_draft(
        tenant_context=ctx_a, schema_snapshot_id=snapshot_a.id, document=_alpha_document()
    )
    await session.commit()

    with pytest.raises(TenantMismatchError):
        await service.submit_for_validation(mapping.id, tenant_context=ctx_b)
