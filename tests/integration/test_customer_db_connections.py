"""Integration tests against real Tenant Alpha / Tenant Beta PostgreSQL databases (Phase 4).

Requires `make up` (see docker-compose.yml's `tenant-alpha-db` / `tenant-beta-db` services,
seeded by docker/postgres-fixtures/{alpha,beta}/init.sql). Skipped automatically if either
database is unreachable, so `make test-unit` / CI's default job never depends on Docker.
"""

from __future__ import annotations

import io
import uuid

import pytest
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from hermes_rpt.common.logging import configure_logging
from hermes_rpt.common.settings import Settings
from hermes_rpt.connectors.enums import ConnectionStatus, DatabaseEngine
from hermes_rpt.connectors.pool_registry import TenantConnectionPoolRegistry
from hermes_rpt.connectors.postgres import PostgresConnector
from hermes_rpt.connectors.service import ConnectionLifecycleManager
from hermes_rpt.secrets.provider import LocalDevSecretProvider
from hermes_rpt.tenants.context import TenantContext
from hermes_rpt.tenants.repository import TenantRepository, UserRepository
from tests.factories import make_tenant, make_user

pytestmark = pytest.mark.asyncio

_ALPHA = {
    "host": "localhost",
    "port": 5433,
    "database": "tenant_alpha",
    "username": "alpha_app",
    "password": "alpha-dev-password",  # noqa: S105 - docker-compose dev fixture, not a real secret
}
_BETA = {
    "host": "localhost",
    "port": 5434,
    "database": "tenant_beta",
    "username": "beta_app",
    "password": "beta-dev-password",  # noqa: S105 - docker-compose dev fixture, not a real secret
}


async def _reachable(target: dict[str, object]) -> bool:
    from sqlalchemy.engine import URL

    url = URL.create(drivername="postgresql+asyncpg", **target)
    try:
        engine = create_async_engine(url, connect_args={"timeout": 2})
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        await engine.dispose()
        return True
    except Exception:  # noqa: BLE001 - availability probe, any failure means "skip"
        return False


@pytest.fixture
async def require_tenant_databases() -> None:
    if not (await _reachable(_ALPHA) and await _reachable(_BETA)):
        pytest.skip(
            "Tenant Alpha/Beta PostgreSQL databases are not reachable (run `make up` first)"
        )


def _register_kwargs(target: dict[str, object]) -> dict[str, object]:
    """Adapts the {host, port, database, username, password} shape (matches
    sqlalchemy.engine.URL.create's parameter names, used by `_reachable`) to
    `ConnectionLifecycleManager.register_connection`'s parameter names."""

    return {
        "host": target["host"],
        "port": target["port"],
        "database_name": target["database"],
        "username": target["username"],
        "secret_value": target["password"],
    }


async def _tenant_context(session: AsyncSession, *, slug: str) -> TenantContext:
    tenant = await TenantRepository(session).add(make_tenant(name=slug, slug=slug))
    user = await UserRepository(session).add(make_user(email=f"{slug}@example.com"))
    await session.commit()
    return TenantContext(tenant_id=tenant.id, principal_id=user.id)


async def test_validate_against_real_databases_succeeds(
    require_tenant_databases: None, session: AsyncSession
) -> None:
    ctx_alpha = await _tenant_context(session, slug=f"alpha-{uuid.uuid4().hex[:8]}")
    ctx_beta = await _tenant_context(session, slug=f"beta-{uuid.uuid4().hex[:8]}")
    registry = TenantConnectionPoolRegistry(PostgresConnector())
    manager = ConnectionLifecycleManager(
        session,
        secret_provider=LocalDevSecretProvider(),
        pool_registry=registry,
        connector=PostgresConnector(),
    )

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

    validated_alpha = await manager.validate_connection(conn_alpha.id, tenant_context=ctx_alpha)
    validated_beta = await manager.validate_connection(conn_beta.id, tenant_context=ctx_beta)

    assert validated_alpha.status == ConnectionStatus.ACTIVE
    assert validated_beta.status == ConnectionStatus.ACTIVE

    # Real engines, still correctly tenant-keyed — Tenant Beta cannot obtain Alpha's engine.
    assert registry.get_existing(tenant_id=ctx_beta.tenant_id, connection_id=conn_alpha.id) is None
    assert (
        registry.get_existing(tenant_id=ctx_alpha.tenant_id, connection_id=conn_alpha.id)
        is not None
    )

    await registry.evict_all_for_tenant(tenant_id=ctx_alpha.tenant_id)
    await registry.evict_all_for_tenant(tenant_id=ctx_beta.tenant_id)


async def test_write_statement_is_rejected_by_the_database(
    require_tenant_databases: None, session: AsyncSession
) -> None:
    """Proves the database-level `default_transaction_read_only` setting holds even if a write
    somehow reached the connection directly — defense in depth beneath the application-layer
    query guard (hermes_rpt.connectors.query_guard), not a replacement for it."""

    ctx_alpha = await _tenant_context(session, slug=f"alpha-write-{uuid.uuid4().hex[:8]}")
    registry = TenantConnectionPoolRegistry(PostgresConnector())
    manager = ConnectionLifecycleManager(
        session,
        secret_provider=LocalDevSecretProvider(),
        pool_registry=registry,
        connector=PostgresConnector(),
    )
    connection = await manager.register_connection(
        tenant_context=ctx_alpha,
        name="alpha",
        engine=DatabaseEngine.POSTGRESQL,
        schema_allowlist=["public"],
        table_allowlist=["fleet_vehicle"],
        **_register_kwargs(_ALPHA),  # type: ignore[arg-type]
    )
    await session.commit()
    await manager.validate_connection(connection.id, tenant_context=ctx_alpha)

    engine = registry.get_existing(tenant_id=ctx_alpha.tenant_id, connection_id=connection.id)
    assert engine is not None

    with pytest.raises(Exception, match="(?i)read.only"):
        async with engine.connect() as conn:
            await conn.execute(text("INSERT INTO fleet_vehicle (registration_no) VALUES ('X')"))

    await registry.evict_all_for_tenant(tenant_id=ctx_alpha.tenant_id)


async def test_logs_contain_no_passwords_against_a_real_connection(
    require_tenant_databases: None, session: AsyncSession
) -> None:
    stream = io.StringIO()
    configure_logging(Settings())
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=stream))

    ctx_alpha = await _tenant_context(session, slug=f"alpha-logs-{uuid.uuid4().hex[:8]}")
    registry = TenantConnectionPoolRegistry(PostgresConnector())
    manager = ConnectionLifecycleManager(
        session,
        secret_provider=LocalDevSecretProvider(),
        pool_registry=registry,
        connector=PostgresConnector(),
    )
    connection = await manager.register_connection(
        tenant_context=ctx_alpha,
        name="alpha",
        engine=DatabaseEngine.POSTGRESQL,
        schema_allowlist=["public"],
        table_allowlist=["fleet_vehicle"],
        **_register_kwargs(_ALPHA),  # type: ignore[arg-type]
    )
    await session.commit()
    await manager.validate_connection(connection.id, tenant_context=ctx_alpha)
    await registry.evict_all_for_tenant(tenant_id=ctx_alpha.tenant_id)

    assert _ALPHA["password"] not in stream.getvalue()
