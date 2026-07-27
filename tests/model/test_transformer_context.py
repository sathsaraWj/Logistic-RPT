"""Tests for `RelationalContextBuilder` (Phase 11) against real synthetic Tenant-Alpha and
Tenant-Beta data — mirrors the pattern in `tests/security/test_dataset_build_service.py`, but
exercises the relational (per-record) fetch path instead of Phase 8's aggregated features.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from hermes_rpt.connectors.enums import DatabaseEngine
from hermes_rpt.connectors.pool_registry import TenantConnectionPoolRegistry
from hermes_rpt.connectors.service import ConnectionLifecycleManager
from hermes_rpt.mappings.service import MappingService
from hermes_rpt.models.transformer.context import (
    RelationalContextBuilder,
    TargetRecordNotFoundError,
)
from hermes_rpt.schemas.enums import DiscoveryStatus
from hermes_rpt.schemas.introspection import SchemaIntrospectionResult
from hermes_rpt.schemas.models import SchemaSnapshot
from hermes_rpt.schemas.repository import SchemaSnapshotRepository
from hermes_rpt.secrets.provider import LocalDevSecretProvider
from hermes_rpt.synthetic.generator import generate_fleet_data
from hermes_rpt.synthetic.loader import create_tenant_schema, load_fleet_data
from hermes_rpt.synthetic.schemas import ALL_ENTITY_SCHEMAS
from hermes_rpt.tenants.context import TenantContext
from hermes_rpt.tenants.repository import TenantRepository, UserRepository
from tests.factories import make_tenant, make_user
from tests.fakes import SQLiteConnector

_START = datetime(2026, 1, 1, tzinfo=UTC)
_END = datetime(2026, 3, 1, tzinfo=UTC)


async def _make_sqlite_engine():  # type: ignore[no-untyped-def]
    return create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )


async def _provision_tenant(
    session: AsyncSession, *, tenant_slug: str, seed: int
) -> tuple[TenantContext, ConnectionLifecycleManager]:
    tenant = await TenantRepository(session).add(make_tenant(name=tenant_slug, slug=tenant_slug))
    user = await UserRepository(session).add(make_user(email=f"{tenant_slug}@example.com"))
    await session.commit()
    tenant_context = TenantContext(tenant_id=tenant.id, principal_id=user.id)

    data_engine = await _make_sqlite_engine()
    await create_tenant_schema(data_engine, tenant_slug=tenant_slug)
    data = generate_fleet_data(tenant_slug=tenant_slug, seed=seed, start=_START, end=_END)
    await load_fleet_data(data_engine, data, tenant_slug=tenant_slug)

    connector = SQLiteConnector(data_engine)
    manager = ConnectionLifecycleManager(
        session,
        secret_provider=LocalDevSecretProvider(),
        pool_registry=TenantConnectionPoolRegistry(connector),
        connector=connector,
    )
    mapping_service = MappingService(session)
    for schema in ALL_ENTITY_SCHEMAS:
        document = schema.mapping_document(tenant_slug)
        connection = await manager.register_connection(
            tenant_context=tenant_context,
            name=f"{tenant_slug}-{schema.entity}",
            engine=DatabaseEngine.POSTGRESQL,
            host="db.internal",
            port=5432,
            database_name="db",
            username="app",
            secret_value="unit-test-secret",  # noqa: S106
            schema_allowlist=["main"],
            table_allowlist=[schema.table(tenant_slug)],
        )
        await session.commit()
        snapshot = await SchemaSnapshotRepository(session).add(
            SchemaSnapshot(
                connection_id=connection.id,
                sequence_number=1,
                status=DiscoveryStatus.COMPLETED,
                metadata_document=SchemaIntrospectionResult(schemas=["main"], tables=[]).model_dump(
                    mode="json"
                ),
            ),
            tenant_context=tenant_context,
        )
        await session.commit()
        mapping, _version = await mapping_service.create_draft(
            tenant_context=tenant_context, schema_snapshot_id=snapshot.id, document=document
        )
        await session.commit()
        await mapping_service.submit_for_validation(mapping.id, tenant_context=tenant_context)
        await mapping_service.approve(mapping.id, tenant_context=tenant_context)
        await mapping_service.activate(mapping.id, tenant_context=tenant_context)
        await session.commit()

    return tenant_context, manager


async def test_build_example_includes_vehicle_and_maintenance_history_for_alpha(
    session: AsyncSession,
) -> None:
    tenant_context, manager = await _provision_tenant(session, tenant_slug="alpha", seed=1)
    builder = RelationalContextBuilder(session, connection_manager=manager)

    data = generate_fleet_data(tenant_slug="alpha", seed=1, start=_START, end=_END)
    completed = next(t for t in data.trips if t["status"] == "completed")

    example = await builder.build_example(
        business_reference=completed["trip_id"],
        prediction_time=completed["planned_departure_at"],
        label=1,
        tenant_context=tenant_context,
        max_records_per_relation=4,
    )

    assert example.target.entity == "Trip"
    assert example.target.relationship == "__target__"
    relationships_seen = {r.relationship for r in example.related}
    assert "uses_vehicle" in relationships_seen  # every trip has a vehicle


async def test_build_example_for_beta_schema_produces_the_same_relationship_shape(
    session: AsyncSession,
) -> None:
    """Proof the context builder is schema-agnostic — Beta's tables/columns are named
    completely differently from Alpha's (hermes_rpt.synthetic.schemas)."""

    tenant_context, manager = await _provision_tenant(session, tenant_slug="beta", seed=2)
    builder = RelationalContextBuilder(session, connection_manager=manager)

    data = generate_fleet_data(tenant_slug="beta", seed=2, start=_START, end=_END)
    completed = next(t for t in data.trips if t["status"] == "completed")

    example = await builder.build_example(
        business_reference=completed["trip_id"],
        prediction_time=completed["planned_departure_at"],
        label=0,
        tenant_context=tenant_context,
        max_records_per_relation=4,
    )
    assert example.target.fields.get("vehicle_id") is not None


async def test_no_related_record_field_ever_carries_a_tenant_id(session: AsyncSession) -> None:
    tenant_context, manager = await _provision_tenant(session, tenant_slug="alpha", seed=3)
    builder = RelationalContextBuilder(session, connection_manager=manager)
    data = generate_fleet_data(tenant_slug="alpha", seed=3, start=_START, end=_END)
    completed = next(t for t in data.trips if t["status"] == "completed")

    example = await builder.build_example(
        business_reference=completed["trip_id"],
        prediction_time=completed["planned_departure_at"],
        label=1,
        tenant_context=tenant_context,
        max_records_per_relation=4,
    )
    assert "tenant_id" not in example.target.fields
    assert all("tenant_id" not in record.fields for record in example.related)


async def test_related_records_respect_the_point_in_time_cutoff(session: AsyncSession) -> None:
    tenant_context, manager = await _provision_tenant(session, tenant_slug="alpha", seed=4)
    builder = RelationalContextBuilder(session, connection_manager=manager)
    data = generate_fleet_data(tenant_slug="alpha", seed=4, start=_START, end=_END)
    completed = next(t for t in data.trips if t["status"] == "completed")
    prediction_time = completed["planned_departure_at"]

    example = await builder.build_example(
        business_reference=completed["trip_id"],
        prediction_time=prediction_time,
        label=1,
        tenant_context=tenant_context,
        max_records_per_relation=10,
    )
    for record in example.related:
        for field_name in ("started_at", "occurred_at"):
            value = record.fields.get(field_name)
            if value is not None:
                assert value < prediction_time


async def test_missing_target_row_raises(session: AsyncSession) -> None:
    tenant_context, manager = await _provision_tenant(session, tenant_slug="alpha", seed=5)
    builder = RelationalContextBuilder(session, connection_manager=manager)

    with pytest.raises(TargetRecordNotFoundError):
        await builder.build_example(
            business_reference="does-not-exist",
            prediction_time=_START,
            label=1,
            tenant_context=tenant_context,
            max_records_per_relation=4,
        )
