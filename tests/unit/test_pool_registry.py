"""TenantConnectionPoolRegistry tests using a fake connector (no real network I/O)."""

from __future__ import annotations

import uuid

from hermes_rpt.connectors.interfaces import ConnectionTarget
from hermes_rpt.connectors.pool_registry import TenantConnectionPoolRegistry
from tests.fakes import FakeConnector

_TARGET = ConnectionTarget(
    host="localhost", port=5432, database="db", username="u", password="p", tls_mode="require"
)


async def test_get_or_create_returns_the_same_engine_on_repeated_calls() -> None:
    connector = FakeConnector()
    registry = TenantConnectionPoolRegistry(connector)
    tenant_id, connection_id = uuid.uuid4(), uuid.uuid4()

    first = await registry.get_or_create(
        tenant_id=tenant_id, connection_id=connection_id, target=_TARGET
    )
    second = await registry.get_or_create(
        tenant_id=tenant_id, connection_id=connection_id, target=_TARGET
    )

    assert first is second
    assert len(connector.built) == 1  # only built once, not once per call


async def test_different_tenants_never_share_an_engine_even_for_the_same_connection_id() -> None:
    """The registry keys on (tenant_id, connection_id) — this proves the tenant_id half of
    that key actually matters, not just the connection_id half."""

    connector = FakeConnector()
    registry = TenantConnectionPoolRegistry(connector)
    connection_id = uuid.uuid4()
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()

    engine_a = await registry.get_or_create(
        tenant_id=tenant_a, connection_id=connection_id, target=_TARGET
    )
    engine_b = await registry.get_or_create(
        tenant_id=tenant_b, connection_id=connection_id, target=_TARGET
    )

    assert engine_a is not engine_b
    assert registry.get_existing(tenant_id=tenant_a, connection_id=connection_id) is engine_a
    assert registry.get_existing(tenant_id=tenant_b, connection_id=connection_id) is engine_b


async def test_evict_disposes_and_removes_the_engine() -> None:
    connector = FakeConnector()
    registry = TenantConnectionPoolRegistry(connector)
    tenant_id, connection_id = uuid.uuid4(), uuid.uuid4()

    engine = await registry.get_or_create(
        tenant_id=tenant_id, connection_id=connection_id, target=_TARGET
    )
    await registry.evict(tenant_id=tenant_id, connection_id=connection_id)

    assert engine.disposed is True
    assert registry.get_existing(tenant_id=tenant_id, connection_id=connection_id) is None


async def test_evicting_one_tenant_connection_does_not_touch_another() -> None:
    connector = FakeConnector()
    registry = TenantConnectionPoolRegistry(connector)
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    conn_a, conn_b = uuid.uuid4(), uuid.uuid4()

    engine_a = await registry.get_or_create(
        tenant_id=tenant_a, connection_id=conn_a, target=_TARGET
    )
    engine_b = await registry.get_or_create(
        tenant_id=tenant_b, connection_id=conn_b, target=_TARGET
    )

    await registry.evict(tenant_id=tenant_a, connection_id=conn_a)

    assert engine_a.disposed is True
    assert engine_b.disposed is False
    assert registry.get_existing(tenant_id=tenant_b, connection_id=conn_b) is engine_b


async def test_evict_all_for_tenant_only_evicts_that_tenants_pools() -> None:
    connector = FakeConnector()
    registry = TenantConnectionPoolRegistry(connector)
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()

    await registry.get_or_create(tenant_id=tenant_a, connection_id=uuid.uuid4(), target=_TARGET)
    await registry.get_or_create(tenant_id=tenant_a, connection_id=uuid.uuid4(), target=_TARGET)
    engine_b = await registry.get_or_create(
        tenant_id=tenant_b, connection_id=uuid.uuid4(), target=_TARGET
    )

    await registry.evict_all_for_tenant(tenant_id=tenant_a)

    assert registry.pool_count() == 1
    assert engine_b.disposed is False
