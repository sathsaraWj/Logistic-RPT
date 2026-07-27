"""End-to-end feature extraction tests (Phase 8) for Tenant Alpha's and Tenant Beta's
differently-named schemas — "add integration tests for Alpha and Beta." Runs the full pipeline
(connection -> mapping -> resolver -> compiler -> normalizer -> lineage) against a real SQLite
database via `SQLiteConnector` (see tests/fakes.py — `FakeConnector` can't run real SQL, and
this phase's whole job is running real, safe SQL).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from hermes_rpt.connectors.enums import DatabaseEngine
from hermes_rpt.connectors.pool_registry import TenantConnectionPoolRegistry
from hermes_rpt.connectors.service import ConnectionLifecycleManager
from hermes_rpt.features.contract import DELIVERY_DELAY_RISK_CONTRACT
from hermes_rpt.features.resolver import TargetMappingUnavailableError
from hermes_rpt.features.service import FeatureExtractionService
from hermes_rpt.mappings.document import FieldMapping, MappingDocument, SourceTable, ValueSource
from hermes_rpt.mappings.service import MappingService
from hermes_rpt.schemas.enums import DiscoveryStatus
from hermes_rpt.schemas.introspection import SchemaIntrospectionResult
from hermes_rpt.schemas.models import SchemaSnapshot
from hermes_rpt.schemas.repository import SchemaSnapshotRepository
from hermes_rpt.secrets.provider import LocalDevSecretProvider
from hermes_rpt.tenants.context import TenantContext
from hermes_rpt.tenants.repository import TenantRepository, UserRepository
from tests.factories import make_tenant, make_user
from tests.fakes import SQLiteConnector

_PREDICTION_TIME = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)


async def _make_sqlite_engine():  # type: ignore[no-untyped-def]
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    return engine


async def _tenant_context(session: AsyncSession, *, slug: str) -> TenantContext:
    tenant = await TenantRepository(session).add(make_tenant(name=slug, slug=slug))
    user = await UserRepository(session).add(make_user(email=f"{slug}@example.com"))
    await session.commit()
    return TenantContext(tenant_id=tenant.id, principal_id=user.id)


async def _register_and_activate(
    session: AsyncSession,
    *,
    tenant_context: TenantContext,
    manager: ConnectionLifecycleManager,
    connection_name: str,
    document: MappingDocument,
) -> None:
    connection = await manager.register_connection(
        tenant_context=tenant_context,
        name=connection_name,
        engine=DatabaseEngine.POSTGRESQL,
        host="db.internal",
        port=5432,
        database_name="db",
        username="app",
        secret_value="unit-test-secret",  # noqa: S106
        schema_allowlist=["main"],
        table_allowlist=[document.source.table],
    )
    await session.commit()

    introspection = SchemaIntrospectionResult(schemas=["main"], tables=[])
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

    mapping_service = MappingService(session)
    mapping, _version = await mapping_service.create_draft(
        tenant_context=tenant_context, schema_snapshot_id=snapshot.id, document=document
    )
    await session.commit()
    await mapping_service.submit_for_validation(mapping.id, tenant_context=tenant_context)
    await mapping_service.approve(mapping.id, tenant_context=tenant_context)
    await mapping_service.activate(mapping.id, tenant_context=tenant_context)
    await session.commit()


def _trip_document(*, table: str, vehicle_id_column: str) -> MappingDocument:
    return MappingDocument(
        entity="Trip",
        source=SourceTable(schema="main", table=table),
        identity={"trip_id": FieldMapping(sources=(ValueSource(column="trip_id"),))},
        fields={
            "vehicle_id": FieldMapping(sources=(ValueSource(column=vehicle_id_column),)),
            "planned_departure_at": FieldMapping(
                sources=(ValueSource(column="planned_departure_at"),)
            ),
            "planned_distance_km": FieldMapping(
                sources=(ValueSource(column="planned_distance_km"),)
            ),
            # Required by the ontology but irrelevant to the features under test here — mapped
            # via a static value purely to satisfy MappingService.submit_for_validation's
            # required-field check.
            "status": FieldMapping(sources=(ValueSource(static="completed"),)),
        },
    )


def _vehicle_document(*, table: str, id_column: str, acquired_column: str) -> MappingDocument:
    return MappingDocument(
        entity="Vehicle",
        source=SourceTable(schema="main", table=table),
        identity={"vehicle_id": FieldMapping(sources=(ValueSource(column=id_column),))},
        fields={
            "acquired_at": FieldMapping(sources=(ValueSource(column=acquired_column),)),
            "registration_number": FieldMapping(sources=(ValueSource(static="REG-STATIC"),)),
            "is_active": FieldMapping(sources=(ValueSource(static=True),)),
        },
    )


async def test_extraction_end_to_end_for_alpha_schema(session: AsyncSession) -> None:
    """Alpha-shaped schema: table/column names close to the ontology (trip.vehicle_id,
    vehicle.acquired_at)."""

    engine = await _make_sqlite_engine()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE trip (trip_id TEXT PRIMARY KEY, vehicle_id TEXT, "
                "planned_departure_at TIMESTAMP, planned_distance_km REAL)"
            )
        )
        await conn.execute(
            text("CREATE TABLE vehicle (vehicle_id TEXT PRIMARY KEY, acquired_at DATE)")
        )
        await conn.execute(
            text("INSERT INTO trip VALUES ('TRIP-1', 'V-1', '2026-07-27 08:30:00', 120.5)")
        )
        await conn.execute(text("INSERT INTO vehicle VALUES ('V-1', '2024-07-27')"))

    ctx = await _tenant_context(session, slug="feat-alpha")
    connector = SQLiteConnector(engine)
    manager = ConnectionLifecycleManager(
        session,
        secret_provider=LocalDevSecretProvider(),
        pool_registry=TenantConnectionPoolRegistry(connector),
        connector=connector,
    )
    await _register_and_activate(
        session,
        tenant_context=ctx,
        manager=manager,
        connection_name="alpha-trip",
        document=_trip_document(table="trip", vehicle_id_column="vehicle_id"),
    )
    await _register_and_activate(
        session,
        tenant_context=ctx,
        manager=manager,
        connection_name="alpha-vehicle",
        document=_vehicle_document(
            table="vehicle", id_column="vehicle_id", acquired_column="acquired_at"
        ),
    )

    service = FeatureExtractionService(session, connection_manager=manager)
    batch = await service.extract(
        DELIVERY_DELAY_RISK_CONTRACT,
        tenant_context=ctx,
        business_reference="TRIP-1",
        prediction_time=_PREDICTION_TIME,
    )

    assert batch.features["planned_departure_hour"] == 8
    assert batch.features["planned_trip_distance_km"] == 120.5
    assert batch.features["vehicle_age_years"] == pytest.approx(2.0, abs=0.01)
    assert batch.lineage.tenant_id == ctx.tenant_id
    assert batch.lineage.task_key == "delivery-delay-risk"
    assert "Vehicle" in batch.lineage.related_mapping_version_ids

    await engine.dispose()


async def test_extraction_end_to_end_for_beta_schema(session: AsyncSession) -> None:
    """Beta-shaped schema: completely different table/column names (assets.tag-style naming),
    proving the pipeline is schema-agnostic, not just tuned to Alpha's naming."""

    engine = await _make_sqlite_engine()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE jobs (job_ref TEXT PRIMARY KEY, asset_ref TEXT, "
                "depart_at TIMESTAMP, distance_km REAL)"
            )
        )
        await conn.execute(
            text("CREATE TABLE assets (asset_ref TEXT PRIMARY KEY, in_service_since DATE)")
        )
        await conn.execute(
            text("INSERT INTO jobs VALUES ('JOB-9', 'ASSET-9', '2026-07-27 14:00:00', 42.0)")
        )
        await conn.execute(text("INSERT INTO assets VALUES ('ASSET-9', '2020-07-27')"))

    ctx = await _tenant_context(session, slug="feat-beta")
    connector = SQLiteConnector(engine)
    manager = ConnectionLifecycleManager(
        session,
        secret_provider=LocalDevSecretProvider(),
        pool_registry=TenantConnectionPoolRegistry(connector),
        connector=connector,
    )
    trip_document = MappingDocument(
        entity="Trip",
        source=SourceTable(schema="main", table="jobs"),
        identity={"trip_id": FieldMapping(sources=(ValueSource(column="job_ref"),))},
        fields={
            "vehicle_id": FieldMapping(sources=(ValueSource(column="asset_ref"),)),
            "planned_departure_at": FieldMapping(sources=(ValueSource(column="depart_at"),)),
            "planned_distance_km": FieldMapping(sources=(ValueSource(column="distance_km"),)),
            "status": FieldMapping(sources=(ValueSource(static="completed"),)),
        },
    )
    vehicle_document = MappingDocument(
        entity="Vehicle",
        source=SourceTable(schema="main", table="assets"),
        identity={"vehicle_id": FieldMapping(sources=(ValueSource(column="asset_ref"),))},
        fields={
            "acquired_at": FieldMapping(sources=(ValueSource(column="in_service_since"),)),
            "registration_number": FieldMapping(sources=(ValueSource(static="REG-STATIC"),)),
            "is_active": FieldMapping(sources=(ValueSource(static=True),)),
        },
    )
    await _register_and_activate(
        session,
        tenant_context=ctx,
        manager=manager,
        connection_name="beta-jobs",
        document=trip_document,
    )
    await _register_and_activate(
        session,
        tenant_context=ctx,
        manager=manager,
        connection_name="beta-assets",
        document=vehicle_document,
    )

    service = FeatureExtractionService(session, connection_manager=manager)
    batch = await service.extract(
        DELIVERY_DELAY_RISK_CONTRACT,
        tenant_context=ctx,
        business_reference="JOB-9",
        prediction_time=_PREDICTION_TIME,
    )

    assert batch.features["planned_departure_hour"] == 14
    assert batch.features["planned_trip_distance_km"] == 42.0
    assert batch.features["vehicle_age_years"] == pytest.approx(6.0, abs=0.01)

    await engine.dispose()


async def test_missing_related_mapping_marks_features_missing_not_an_error(
    session: AsyncSession,
) -> None:
    """ "Do not assume all customers have every feature" — only Trip is mapped; every feature
    that needs Vehicle/MaintenanceEvent/etc. must come back as a normalized missing value, and
    extraction must still succeed rather than raising."""

    engine = await _make_sqlite_engine()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE trip (trip_id TEXT PRIMARY KEY, vehicle_id TEXT, "
                "planned_departure_at TIMESTAMP, planned_distance_km REAL)"
            )
        )
        await conn.execute(
            text("INSERT INTO trip VALUES ('TRIP-1', 'V-1', '2026-07-27 08:30:00', 120.5)")
        )

    ctx = await _tenant_context(session, slug="feat-partial")
    connector = SQLiteConnector(engine)
    manager = ConnectionLifecycleManager(
        session,
        secret_provider=LocalDevSecretProvider(),
        pool_registry=TenantConnectionPoolRegistry(connector),
        connector=connector,
    )
    await _register_and_activate(
        session,
        tenant_context=ctx,
        manager=manager,
        connection_name="partial-trip",
        document=_trip_document(table="trip", vehicle_id_column="vehicle_id"),
    )

    service = FeatureExtractionService(session, connection_manager=manager)
    batch = await service.extract(
        DELIVERY_DELAY_RISK_CONTRACT,
        tenant_context=ctx,
        business_reference="TRIP-1",
        prediction_time=_PREDICTION_TIME,
    )

    assert batch.features["planned_departure_hour"] == 8  # still present — no Vehicle needed
    assert batch.features["vehicle_age_years"] is None  # missing — no Vehicle mapping
    assert batch.features["odometer_km"] is None
    missing_names = {m.feature_name for m in batch.lineage.missing_features}
    # Every related-entity feature is recorded as missing (regardless of `required`) since its
    # entity has no active mapping at all — "do not assume all customers have every feature."
    assert "vehicle_age_years" in missing_names
    assert "odometer_km" in missing_names
    # Trip itself resolves (it's the target, and some features self-join Trip -> Trip), but no
    # *other* entity does, since only Trip has an active mapping in this tenant.
    assert set(batch.lineage.related_mapping_version_ids) == {"Trip"}

    await engine.dispose()


async def test_extraction_fails_closed_when_target_mapping_is_unavailable(
    session: AsyncSession,
) -> None:
    ctx = await _tenant_context(session, slug="feat-no-target")
    engine = await _make_sqlite_engine()
    connector = SQLiteConnector(engine)
    manager = ConnectionLifecycleManager(
        session,
        secret_provider=LocalDevSecretProvider(),
        pool_registry=TenantConnectionPoolRegistry(connector),
        connector=connector,
    )
    service = FeatureExtractionService(session, connection_manager=manager)

    with pytest.raises(TargetMappingUnavailableError):
        await service.extract(
            DELIVERY_DELAY_RISK_CONTRACT,
            tenant_context=ctx,
            business_reference="TRIP-1",
            prediction_time=_PREDICTION_TIME,
        )
    await engine.dispose()


async def test_dry_run_plan_never_touches_the_database(session: AsyncSession) -> None:
    """No connection, no engine, no tables exist at all — `plan()` must still work because it
    never opens a real connection (Phase 8: "a dry-run mode that displays safe query plans
    without exposing secrets")."""

    ctx = await _tenant_context(session, slug="feat-dry-run")
    engine = await _make_sqlite_engine()
    connector = SQLiteConnector(engine)
    manager = ConnectionLifecycleManager(
        session,
        secret_provider=LocalDevSecretProvider(),
        pool_registry=TenantConnectionPoolRegistry(connector),
        connector=connector,
    )
    await _register_and_activate(
        session,
        tenant_context=ctx,
        manager=manager,
        connection_name="dry-run-trip",
        document=_trip_document(table="trip", vehicle_id_column="vehicle_id"),
    )

    service = FeatureExtractionService(session, connection_manager=manager)
    report = await service.plan(DELIVERY_DELAY_RISK_CONTRACT, tenant_context=ctx)

    assert report.target_source_table == "main.trip"
    entry_by_name = {e.feature_name: e for e in report.entries}
    assert entry_by_name["planned_trip_distance_km"].available is True
    assert entry_by_name["vehicle_age_years"].available is False  # no Vehicle mapping

    await engine.dispose()
