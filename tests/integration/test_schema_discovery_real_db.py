"""Integration test: schema discovery against the real Tenant Alpha/Beta PostgreSQL databases
(Phase 5). Skipped automatically if those databases are unreachable (run `make up` first) —
same pattern as tests/integration/test_customer_db_connections.py.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from hermes_rpt.connectors.enums import DatabaseEngine
from hermes_rpt.connectors.pool_registry import TenantConnectionPoolRegistry
from hermes_rpt.connectors.postgres import PostgresConnector
from hermes_rpt.connectors.service import ConnectionLifecycleManager
from hermes_rpt.schemas.enums import DiscoveryStatus
from hermes_rpt.schemas.service import SchemaDiscoveryService
from hermes_rpt.secrets.provider import LocalDevSecretProvider
from hermes_rpt.tenants.context import TenantContext
from hermes_rpt.tenants.repository import TenantRepository, UserRepository
from tests.factories import make_tenant, make_user
from tests.integration.test_customer_db_connections import (
    _ALPHA,
    _BETA,
    _register_kwargs,
    require_tenant_databases,  # noqa: F401 - imported for pytest fixture discovery
)

pytestmark = pytest.mark.asyncio


async def _tenant_context(session: AsyncSession, *, slug: str) -> TenantContext:
    tenant = await TenantRepository(session).add(make_tenant(name=slug, slug=slug))
    user = await UserRepository(session).add(make_user(email=f"{slug}@example.com"))
    await session.commit()
    return TenantContext(tenant_id=tenant.id, principal_id=user.id)


async def test_discovery_against_real_alpha_and_beta_finds_different_schemas(
    require_tenant_databases: None,  # noqa: F811 - fixture param shadows the import; pytest needs both
    session: AsyncSession,
) -> None:
    ctx_alpha = await _tenant_context(session, slug=f"disco-alpha-{uuid.uuid4().hex[:8]}")
    ctx_beta = await _tenant_context(session, slug=f"disco-beta-{uuid.uuid4().hex[:8]}")

    registry = TenantConnectionPoolRegistry(PostgresConnector())
    manager = ConnectionLifecycleManager(
        session,
        secret_provider=LocalDevSecretProvider(),
        pool_registry=registry,
        connector=PostgresConnector(),
    )
    service = SchemaDiscoveryService(session, connection_manager=manager)

    conn_alpha = await manager.register_connection(
        tenant_context=ctx_alpha,
        name="alpha",
        engine=DatabaseEngine.POSTGRESQL,
        schema_allowlist=["public"],
        table_allowlist=["fleet_vehicle"],
        **_register_kwargs(_ALPHA),  # type: ignore[arg-type]
    )
    conn_beta = await manager.register_connection(
        tenant_context=ctx_beta,
        name="beta",
        engine=DatabaseEngine.POSTGRESQL,
        schema_allowlist=["public"],
        table_allowlist=["assets"],
        **_register_kwargs(_BETA),  # type: ignore[arg-type]
    )
    await session.commit()

    snapshot_alpha = await service.start_discovery(conn_alpha.id, tenant_context=ctx_alpha)
    snapshot_alpha = await service.run_discovery(snapshot_alpha.id, tenant_context=ctx_alpha)
    snapshot_beta = await service.start_discovery(conn_beta.id, tenant_context=ctx_beta)
    snapshot_beta = await service.run_discovery(snapshot_beta.id, tenant_context=ctx_beta)
    await session.commit()

    assert snapshot_alpha.status == DiscoveryStatus.COMPLETED
    assert snapshot_beta.status == DiscoveryStatus.COMPLETED

    alpha_tables = {t["name"] for t in snapshot_alpha.metadata_document["tables"]}
    beta_tables = {t["name"] for t in snapshot_beta.metadata_document["tables"]}
    assert "fleet_vehicle" in alpha_tables
    assert "assets" in beta_tables
    # Same underlying fleet concept, deliberately different shapes — the fingerprints must
    # differ, proving discovery captured the real, distinct schemas rather than something
    # cached/shared between tenants.
    assert snapshot_alpha.schema_fingerprint != snapshot_beta.schema_fingerprint

    await registry.evict_all_for_tenant(tenant_id=ctx_alpha.tenant_id)
    await registry.evict_all_for_tenant(tenant_id=ctx_beta.tenant_id)
