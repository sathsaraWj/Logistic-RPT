"""Service-level tests for schema discovery (Phase 5): status transitions, fingerprinting,
drift computation, tenant isolation, and permission-relevant behaviour. Uses `FakeConnector` /
`FakeIntrospector` (no real network) — real introspection SQL is covered by
tests/integration/test_customer_db_connections.py-style real-database tests (see
docs/IMPLEMENTATION_PLAN.md Phase 5 follow-up in TASKS.md).
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from hermes_rpt.audit.repository import AuditEventRepository
from hermes_rpt.common.repository import TenantMismatchError
from hermes_rpt.connectors.enums import DatabaseEngine
from hermes_rpt.connectors.pool_registry import TenantConnectionPoolRegistry
from hermes_rpt.connectors.service import ConnectionLifecycleManager
from hermes_rpt.schemas.enums import DiscoveryStatus
from hermes_rpt.schemas.introspection import (
    ColumnMetadata,
    SchemaIntrospectionResult,
    TableMetadata,
)
from hermes_rpt.schemas.service import SchemaDiscoveryService
from hermes_rpt.secrets.provider import LocalDevSecretProvider
from hermes_rpt.tenants.context import TenantContext
from hermes_rpt.tenants.repository import TenantRepository, UserRepository
from tests.factories import make_tenant, make_user
from tests.fakes import FailingIntrospector, FakeConnector, FakeIntrospector

_TABLE_V1 = TableMetadata(
    schema_name="public",
    name="fleet_vehicle",
    kind="table",
    columns=[
        ColumnMetadata(
            name="vehicle_id", sql_type="integer", is_nullable=False, ordinal_position=1
        ),
        ColumnMetadata(
            name="registration_no", sql_type="text", is_nullable=False, ordinal_position=2
        ),
    ],
    primary_key_columns=["vehicle_id"],
)
_RESULT_V1 = SchemaIntrospectionResult(schemas=["public"], tables=[_TABLE_V1])

_TABLE_V2 = _TABLE_V1.model_copy(deep=True)
_TABLE_V2.columns.append(
    ColumnMetadata(name="odometer_km", sql_type="numeric", is_nullable=True, ordinal_position=3)
)
_RESULT_V2 = SchemaIntrospectionResult(schemas=["public"], tables=[_TABLE_V2])


async def _tenant_context(session: AsyncSession, *, slug: str) -> TenantContext:
    tenant = await TenantRepository(session).add(make_tenant(name=slug, slug=slug))
    user = await UserRepository(session).add(make_user(email=f"{slug}@example.com"))
    await session.commit()
    return TenantContext(tenant_id=tenant.id, principal_id=user.id)


async def _register_connection(
    session: AsyncSession, *, tenant_context: TenantContext, connector: FakeConnector
) -> tuple[ConnectionLifecycleManager, str]:
    manager = ConnectionLifecycleManager(
        session,
        secret_provider=LocalDevSecretProvider(),
        pool_registry=TenantConnectionPoolRegistry(connector),
        connector=connector,
    )
    connection = await manager.register_connection(
        tenant_context=tenant_context,
        name="alpha-db",
        engine=DatabaseEngine.POSTGRESQL,
        host="alpha.internal",
        port=5432,
        database_name="tenant_alpha",
        username="alpha_app",
        secret_value="unit-test-secret",  # noqa: S106
        schema_allowlist=["public"],
    )
    await session.commit()
    return manager, connection.id


async def test_discovery_completes_and_computes_fingerprints(session: AsyncSession) -> None:
    ctx = await _tenant_context(session, slug="discovery-happy")
    manager, connection_id = await _register_connection(
        session, tenant_context=ctx, connector=FakeConnector()
    )
    service = SchemaDiscoveryService(
        session, connection_manager=manager, introspector=FakeIntrospector([_RESULT_V1])
    )

    snapshot = await service.start_discovery(connection_id, tenant_context=ctx)
    assert snapshot.status == DiscoveryStatus.PENDING

    completed = await service.run_discovery(snapshot.id, tenant_context=ctx)
    await session.commit()

    assert completed.status == DiscoveryStatus.COMPLETED
    assert completed.schema_fingerprint is not None
    assert "public.fleet_vehicle" in completed.table_fingerprints
    assert completed.drift_summary is None  # first snapshot — nothing to compare against


async def test_second_run_detects_drift_and_first_run_has_none(session: AsyncSession) -> None:
    ctx = await _tenant_context(session, slug="discovery-drift")
    manager, connection_id = await _register_connection(
        session, tenant_context=ctx, connector=FakeConnector()
    )
    service = SchemaDiscoveryService(
        session,
        connection_manager=manager,
        introspector=FakeIntrospector([_RESULT_V1, _RESULT_V2]),
    )

    first = await service.start_discovery(connection_id, tenant_context=ctx)
    first = await service.run_discovery(first.id, tenant_context=ctx)
    await session.commit()
    assert first.drift_summary is None

    second = await service.start_discovery(connection_id, tenant_context=ctx)
    second = await service.run_discovery(second.id, tenant_context=ctx)
    await session.commit()

    assert second.drift_summary is not None
    events = second.drift_summary["events"]
    assert any(e["event_type"] == "column_added" for e in events)
    assert second.schema_fingerprint != first.schema_fingerprint


async def test_failed_introspection_sets_status_failed_with_safe_error(
    session: AsyncSession,
) -> None:
    ctx = await _tenant_context(session, slug="discovery-failure")
    manager, connection_id = await _register_connection(
        session, tenant_context=ctx, connector=FakeConnector()
    )
    service = SchemaDiscoveryService(
        session, connection_manager=manager, introspector=FailingIntrospector()
    )

    snapshot = await service.start_discovery(connection_id, tenant_context=ctx)
    with pytest.raises(RuntimeError):
        await service.run_discovery(snapshot.id, tenant_context=ctx)
    await session.commit()

    failed = await service.get_snapshot(snapshot.id, tenant_context=ctx)
    assert failed.status == DiscoveryStatus.FAILED
    assert failed.error == "builtins.RuntimeError"  # class name only, never the raw message
    assert "simulated introspection failure" not in (failed.error or "")


async def test_snapshots_are_tenant_isolated(session: AsyncSession) -> None:
    ctx_a = await _tenant_context(session, slug="discovery-iso-a")
    ctx_b = await _tenant_context(session, slug="discovery-iso-b")
    manager_a, connection_a = await _register_connection(
        session, tenant_context=ctx_a, connector=FakeConnector()
    )
    service_a = SchemaDiscoveryService(
        session, connection_manager=manager_a, introspector=FakeIntrospector([_RESULT_V1])
    )
    snapshot = await service_a.start_discovery(connection_a, tenant_context=ctx_a)
    await service_a.run_discovery(snapshot.id, tenant_context=ctx_a)
    await session.commit()

    manager_b, _ = await _register_connection(
        session, tenant_context=ctx_b, connector=FakeConnector()
    )
    service_b = SchemaDiscoveryService(session, connection_manager=manager_b)

    with pytest.raises(TenantMismatchError):
        await service_b.get_snapshot(snapshot.id, tenant_context=ctx_b)


async def test_acknowledge_drift_records_who_and_when(session: AsyncSession) -> None:
    ctx = await _tenant_context(session, slug="discovery-ack")
    manager, connection_id = await _register_connection(
        session, tenant_context=ctx, connector=FakeConnector()
    )
    service = SchemaDiscoveryService(
        session,
        connection_manager=manager,
        introspector=FakeIntrospector([_RESULT_V1, _RESULT_V2]),
    )
    first = await service.start_discovery(connection_id, tenant_context=ctx)
    await service.run_discovery(first.id, tenant_context=ctx)
    second = await service.start_discovery(connection_id, tenant_context=ctx)
    second = await service.run_discovery(second.id, tenant_context=ctx)
    await session.commit()
    assert second.drift_acknowledged_at is None

    acknowledged = await service.acknowledge_drift(second.id, tenant_context=ctx)
    await session.commit()

    assert acknowledged.drift_acknowledged_at is not None
    assert acknowledged.drift_acknowledged_by_principal_id == ctx.principal_id


async def test_discovery_run_writes_audit_events(session: AsyncSession) -> None:
    ctx = await _tenant_context(session, slug="discovery-audit")
    manager, connection_id = await _register_connection(
        session, tenant_context=ctx, connector=FakeConnector()
    )
    service = SchemaDiscoveryService(
        session, connection_manager=manager, introspector=FakeIntrospector([_RESULT_V1])
    )
    snapshot = await service.start_discovery(connection_id, tenant_context=ctx)
    await service.run_discovery(snapshot.id, tenant_context=ctx)
    await session.commit()

    events = await AuditEventRepository(session).list_for_tenant(tenant_context=ctx)
    actions = {e.action for e in events}
    assert "schema_discovery.start" in actions
    assert "schema_discovery.run" in actions
