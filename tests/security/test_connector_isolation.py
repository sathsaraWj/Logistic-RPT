"""Security tests for the connector subsystem (Phase 4): pool isolation, secret handling, and
log cleanliness. Uses a fake connector (no real network) — real-database read-only/allowlist
behaviour is covered by tests/integration/test_customer_db_connections.py.
"""

from __future__ import annotations

import io

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from hermes_rpt.audit.repository import AuditEventRepository
from hermes_rpt.common.logging import configure_logging
from hermes_rpt.common.repository import TenantMismatchError
from hermes_rpt.common.settings import Settings
from hermes_rpt.connectors.enums import ConnectionStatus, DatabaseEngine
from hermes_rpt.connectors.pool_registry import TenantConnectionPoolRegistry
from hermes_rpt.connectors.repository import DatabaseCredentialReferenceRepository
from hermes_rpt.connectors.service import ConnectionLifecycleManager
from hermes_rpt.secrets.provider import LocalDevSecretProvider, SecretNotFoundError
from hermes_rpt.tenants.context import TenantContext
from hermes_rpt.tenants.repository import TenantRepository, UserRepository
from tests.factories import make_tenant, make_user
from tests.fakes import FakeConnector

_ALPHA_SECRET = "alpha-super-secret-password"  # noqa: S105
_BETA_SECRET = "beta-super-secret-password"  # noqa: S105


async def _make_two_tenant_contexts(
    session: AsyncSession,
) -> tuple[TenantContext, TenantContext]:
    tenants = TenantRepository(session)
    users = UserRepository(session)
    tenant_a = await tenants.add(make_tenant(name="Alpha", slug="connector-test-alpha"))
    tenant_b = await tenants.add(make_tenant(name="Beta", slug="connector-test-beta"))
    user_a = await users.add(make_user(email="connector-alpha@example.com"))
    user_b = await users.add(make_user(email="connector-beta@example.com"))
    await session.commit()
    return (
        TenantContext(tenant_id=tenant_a.id, principal_id=user_a.id),
        TenantContext(tenant_id=tenant_b.id, principal_id=user_b.id),
    )


def _make_manager(
    session: AsyncSession, *, secret_provider: LocalDevSecretProvider, connector: FakeConnector
) -> ConnectionLifecycleManager:
    return ConnectionLifecycleManager(
        session,
        secret_provider=secret_provider,
        pool_registry=TenantConnectionPoolRegistry(connector),
        connector=connector,
    )


async def test_pools_are_not_shared_between_tenants(session: AsyncSession) -> None:
    ctx_a, ctx_b = await _make_two_tenant_contexts(session)
    connector = FakeConnector()
    secret_provider = LocalDevSecretProvider()
    manager = _make_manager(session, secret_provider=secret_provider, connector=connector)

    conn_a = await manager.register_connection(
        tenant_context=ctx_a,
        name="alpha-db",
        engine=DatabaseEngine.POSTGRESQL,
        host="alpha.internal",
        port=5432,
        database_name="tenant_alpha",
        username="alpha_app",
        secret_value=_ALPHA_SECRET,
    )
    conn_b = await manager.register_connection(
        tenant_context=ctx_b,
        name="beta-db",
        engine=DatabaseEngine.POSTGRESQL,
        host="beta.internal",
        port=5432,
        database_name="tenant_beta",
        username="beta_app",
        secret_value=_BETA_SECRET,
    )
    await session.commit()

    validated_a = await manager.validate_connection(conn_a.id, tenant_context=ctx_a)
    validated_b = await manager.validate_connection(conn_b.id, tenant_context=ctx_b)

    assert validated_a.status == ConnectionStatus.ACTIVE
    assert validated_b.status == ConnectionStatus.ACTIVE
    # Two independently built engines — the fake connector built exactly two, never reused.
    assert len(connector.built) == 2
    assert connector.built[0] is not connector.built[1]


async def test_tenant_cannot_reach_another_tenants_connection_at_all(
    session: AsyncSession,
) -> None:
    ctx_a, ctx_b = await _make_two_tenant_contexts(session)
    connector = FakeConnector()
    manager = _make_manager(session, secret_provider=LocalDevSecretProvider(), connector=connector)

    conn_a = await manager.register_connection(
        tenant_context=ctx_a,
        name="alpha-db",
        engine=DatabaseEngine.POSTGRESQL,
        host="alpha.internal",
        port=5432,
        database_name="tenant_alpha",
        username="alpha_app",
        secret_value=_ALPHA_SECRET,
    )
    await session.commit()

    with pytest.raises(TenantMismatchError):
        await manager.get_connection(conn_a.id, tenant_context=ctx_b)


async def test_credential_reference_is_never_the_plaintext_secret(session: AsyncSession) -> None:
    ctx_a, _ = await _make_two_tenant_contexts(session)
    manager = _make_manager(
        session, secret_provider=LocalDevSecretProvider(), connector=FakeConnector()
    )
    connection = await manager.register_connection(
        tenant_context=ctx_a,
        name="alpha-db",
        engine=DatabaseEngine.POSTGRESQL,
        host="alpha.internal",
        port=5432,
        database_name="tenant_alpha",
        username="alpha_app",
        secret_value=_ALPHA_SECRET,
    )
    await session.commit()

    credential = await DatabaseCredentialReferenceRepository(session).require(
        connection.credential_reference_id, tenant_context=ctx_a
    )
    assert _ALPHA_SECRET not in credential.reference_key


async def test_rotate_secret_invalidates_the_old_reference_and_evicts_the_pool(
    session: AsyncSession,
) -> None:
    ctx_a, _ = await _make_two_tenant_contexts(session)
    connector = FakeConnector()
    secret_provider = LocalDevSecretProvider()
    manager = _make_manager(session, secret_provider=secret_provider, connector=connector)

    connection = await manager.register_connection(
        tenant_context=ctx_a,
        name="alpha-db",
        engine=DatabaseEngine.POSTGRESQL,
        host="alpha.internal",
        port=5432,
        database_name="tenant_alpha",
        username="alpha_app",
        secret_value=_ALPHA_SECRET,
    )
    await session.commit()
    await manager.validate_connection(connection.id, tenant_context=ctx_a)
    old_engine = connector.built[0]

    old_credential = await DatabaseCredentialReferenceRepository(session).require(
        connection.credential_reference_id, tenant_context=ctx_a
    )
    old_reference_key = old_credential.reference_key

    rotated = await manager.rotate_secret(
        connection.id, tenant_context=ctx_a, new_secret_value="rotated-new-secret"
    )
    await session.commit()

    assert rotated.status == ConnectionStatus.PENDING_VALIDATION
    assert old_engine.disposed is True  # pool evicted on rotation

    with pytest.raises(SecretNotFoundError):
        await secret_provider.resolve(old_reference_key)


async def test_delete_connection_removes_metadata_and_secret(session: AsyncSession) -> None:
    ctx_a, _ = await _make_two_tenant_contexts(session)
    secret_provider = LocalDevSecretProvider()
    manager = _make_manager(session, secret_provider=secret_provider, connector=FakeConnector())

    connection = await manager.register_connection(
        tenant_context=ctx_a,
        name="alpha-db",
        engine=DatabaseEngine.POSTGRESQL,
        host="alpha.internal",
        port=5432,
        database_name="tenant_alpha",
        username="alpha_app",
        secret_value=_ALPHA_SECRET,
    )
    await session.commit()

    credential = await DatabaseCredentialReferenceRepository(session).require(
        connection.credential_reference_id, tenant_context=ctx_a
    )
    reference_key = credential.reference_key

    await manager.delete_connection(connection.id, tenant_context=ctx_a)
    await session.commit()

    with pytest.raises(TenantMismatchError):
        await manager.get_connection(connection.id, tenant_context=ctx_a)
    with pytest.raises(SecretNotFoundError):
        await secret_provider.resolve(reference_key)


async def test_connection_lifecycle_writes_audit_events(session: AsyncSession) -> None:
    ctx_a, _ = await _make_two_tenant_contexts(session)
    manager = _make_manager(
        session, secret_provider=LocalDevSecretProvider(), connector=FakeConnector()
    )
    connection = await manager.register_connection(
        tenant_context=ctx_a,
        name="alpha-db",
        engine=DatabaseEngine.POSTGRESQL,
        host="alpha.internal",
        port=5432,
        database_name="tenant_alpha",
        username="alpha_app",
        secret_value=_ALPHA_SECRET,
    )
    await session.commit()

    events = await AuditEventRepository(session).list_for_tenant(tenant_context=ctx_a)
    assert any(e.action == "connection.register" and e.resource_id == connection.id for e in events)


async def test_logs_never_contain_the_plaintext_secret(session: AsyncSession) -> None:
    """End-to-end log-cleanliness check across register/validate/rotate — Phase 4's explicit
    "logs contain no passwords" requirement, exercised against real log output rather than
    just the redaction processor in isolation (see tests/unit/test_logging_redaction.py)."""

    stream = io.StringIO()
    configure_logging(Settings())
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=stream))

    ctx_a, _ = await _make_two_tenant_contexts(session)
    manager = _make_manager(
        session, secret_provider=LocalDevSecretProvider(), connector=FakeConnector()
    )
    connection = await manager.register_connection(
        tenant_context=ctx_a,
        name="alpha-db",
        engine=DatabaseEngine.POSTGRESQL,
        host="alpha.internal",
        port=5432,
        database_name="tenant_alpha",
        username="alpha_app",
        secret_value=_ALPHA_SECRET,
    )
    await session.commit()
    await manager.validate_connection(connection.id, tenant_context=ctx_a)
    await manager.rotate_secret(
        connection.id, tenant_context=ctx_a, new_secret_value="rotated-secret-value"
    )
    await session.commit()

    captured = stream.getvalue()
    assert _ALPHA_SECRET not in captured
    assert "rotated-secret-value" not in captured
