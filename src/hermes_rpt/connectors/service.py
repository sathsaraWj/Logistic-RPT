"""Connection lifecycle manager: register / validate / enable / disable / rotate / delete.

This is the only code path allowed to touch a resolved secret. It never returns one — not from
`register_connection`, not from any other method — and it is the sole caller of
`SecretProvider.resolve` outside the connector itself. Every mutating method records an
`AuditEvent` (Phase 4 requirement: "record access in audit logs").
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from hermes_rpt.audit.enums import AuditOutcome
from hermes_rpt.audit.service import AuditService
from hermes_rpt.common.repository import TenantMismatchError
from hermes_rpt.connectors.enums import ConnectionStatus, DatabaseEngine
from hermes_rpt.connectors.health import ConnectionHealthChecker
from hermes_rpt.connectors.interfaces import ConnectionTarget, DatabaseConnector
from hermes_rpt.connectors.models import CustomerDatabaseConnection, DatabaseCredentialReference
from hermes_rpt.connectors.pool_registry import TenantConnectionPoolRegistry
from hermes_rpt.connectors.repository import (
    CustomerDatabaseConnectionRepository,
    DatabaseCredentialReferenceRepository,
)
from hermes_rpt.monitoring import metrics
from hermes_rpt.secrets.provider import SecretNotFoundError, SecretProvider
from hermes_rpt.tenants.context import TenantContext


class ConnectionNotReadyError(Exception):
    """Raised by `enable_connection` when a connection has never been successfully validated."""


class ConnectionDisabledError(Exception):
    """Raised by `get_or_create_engine` for a connection whose `status == DISABLED` — a Phase 16
    security review found `disable_connection` evicted the pool but nothing stopped the very
    next request from calling `get_or_create_engine` and simply rebuilding it, silently
    re-enabling a connection an admin had explicitly turned off. `validate_connection` does not
    call `get_or_create_engine` (it builds its own engine inline, specifically so it can probe a
    not-yet-`ACTIVE` connection), so this guard cannot block the one legitimate path that needs
    to reach a non-`ACTIVE` connection."""


class ConnectionLifecycleManager:
    def __init__(
        self,
        session: AsyncSession,
        *,
        secret_provider: SecretProvider,
        pool_registry: TenantConnectionPoolRegistry,
        connector: DatabaseConnector,
    ) -> None:
        self._session = session
        self._connections = CustomerDatabaseConnectionRepository(session)
        self._credentials = DatabaseCredentialReferenceRepository(session)
        self._secret_provider = secret_provider
        self._pool_registry = pool_registry
        self._health_checker = ConnectionHealthChecker(connector)
        self._audit = AuditService(session)

    async def register_connection(
        self,
        *,
        tenant_context: TenantContext,
        name: str,
        engine: DatabaseEngine,
        host: str,
        port: int,
        database_name: str,
        username: str,
        secret_value: str,
        tls_mode: str = "require",
        schema_allowlist: list[str] | None = None,
        table_allowlist: list[str] | None = None,
    ) -> CustomerDatabaseConnection:
        reference_key = await self._secret_provider.store(secret_value=secret_value)
        credential = await self._credentials.add(
            DatabaseCredentialReference(provider="local_dev", reference_key=reference_key),
            tenant_context=tenant_context,
        )
        connection = await self._connections.add(
            CustomerDatabaseConnection(
                name=name,
                engine=engine,
                host=host,
                port=port,
                database_name=database_name,
                username=username,
                tls_mode=tls_mode,
                credential_reference_id=credential.id,
                status=ConnectionStatus.PENDING_VALIDATION,
                schema_allowlist=schema_allowlist or [],
                table_allowlist=table_allowlist or [],
            ),
            tenant_context=tenant_context,
        )
        await self._audit.record(
            action="connection.register",
            outcome=AuditOutcome.SUCCESS,
            tenant_id=tenant_context.tenant_id,
            principal_id=tenant_context.principal_id,
            resource_type="CustomerDatabaseConnection",
            resource_id=connection.id,
            correlation_id=tenant_context.correlation_id,
            details={"name": name, "engine": engine, "host": host, "database": database_name},
        )
        return connection

    async def _resolve_target(
        self, connection: CustomerDatabaseConnection, *, tenant_context: TenantContext
    ) -> ConnectionTarget:
        credential = await self._credentials.require(
            connection.credential_reference_id, tenant_context=tenant_context
        )
        try:
            password = await self._secret_provider.resolve(credential.reference_key)
        except SecretNotFoundError:
            metrics.secret_resolution_failures_total.labels(
                tenant_id=str(tenant_context.tenant_id)
            ).inc()
            raise
        return ConnectionTarget(
            host=connection.host,
            port=connection.port,
            database=connection.database_name,
            username=connection.username,
            password=password,
            tls_mode=connection.tls_mode,
        )

    async def get_or_create_engine(
        self, connection_id: uuid.UUID, *, tenant_context: TenantContext
    ) -> tuple[CustomerDatabaseConnection, AsyncEngine]:
        """Resolves the pooled engine for an already-registered connection, for callers (e.g.
        schema discovery, Phase 5) that need to run their own queries rather than a plain
        health check. Returns the connection row too, since callers typically need its
        `schema_allowlist`/`table_allowlist`."""

        connection = await self._connections.require(connection_id, tenant_context=tenant_context)
        if connection.status == ConnectionStatus.DISABLED:
            metrics.unexpected_connection_usage_total.labels(
                tenant_id=str(tenant_context.tenant_id), reason="disabled_connection_reused"
            ).inc()
            raise ConnectionDisabledError(
                f"Connection {connection_id} is disabled and cannot be used"
            )
        target = await self._resolve_target(connection, tenant_context=tenant_context)
        engine = await self._pool_registry.get_or_create(
            tenant_id=tenant_context.tenant_id, connection_id=connection.id, target=target
        )
        return connection, engine

    async def validate_connection(
        self, connection_id: uuid.UUID, *, tenant_context: TenantContext
    ) -> CustomerDatabaseConnection:
        connection = await self._connections.require(connection_id, tenant_context=tenant_context)
        target = await self._resolve_target(connection, tenant_context=tenant_context)
        engine = await self._pool_registry.get_or_create(
            tenant_id=tenant_context.tenant_id, connection_id=connection.id, target=target
        )
        result = await self._health_checker.check(engine)

        connection.last_validated_at = result.checked_at
        if result.healthy:
            connection.status = ConnectionStatus.ACTIVE
            connection.last_error = None
        else:
            connection.status = ConnectionStatus.ERROR
            connection.last_error = result.error_summary
            metrics.unexpected_connection_usage_total.labels(
                tenant_id=str(tenant_context.tenant_id), reason="validation_failed"
            ).inc()

        await self._audit.record(
            action="connection.validate",
            outcome=AuditOutcome.SUCCESS if result.healthy else AuditOutcome.ERROR,
            tenant_id=tenant_context.tenant_id,
            principal_id=tenant_context.principal_id,
            resource_type="CustomerDatabaseConnection",
            resource_id=connection.id,
            correlation_id=tenant_context.correlation_id,
            details={"healthy": result.healthy, "error_summary": result.error_summary},
        )
        return connection

    async def enable_connection(
        self, connection_id: uuid.UUID, *, tenant_context: TenantContext
    ) -> CustomerDatabaseConnection:
        connection = await self._connections.require(connection_id, tenant_context=tenant_context)
        if connection.last_validated_at is None:
            metrics.unexpected_connection_usage_total.labels(
                tenant_id=str(tenant_context.tenant_id), reason="enable_before_validation"
            ).inc()
            raise ConnectionNotReadyError(
                "Connection must be successfully validated at least once before it can be enabled"
            )
        connection.status = ConnectionStatus.ACTIVE
        await self._audit.record(
            action="connection.enable",
            outcome=AuditOutcome.SUCCESS,
            tenant_id=tenant_context.tenant_id,
            principal_id=tenant_context.principal_id,
            resource_type="CustomerDatabaseConnection",
            resource_id=connection.id,
            correlation_id=tenant_context.correlation_id,
        )
        return connection

    async def disable_connection(
        self, connection_id: uuid.UUID, *, tenant_context: TenantContext
    ) -> CustomerDatabaseConnection:
        connection = await self._connections.require(connection_id, tenant_context=tenant_context)
        connection.status = ConnectionStatus.DISABLED
        await self._pool_registry.evict(
            tenant_id=tenant_context.tenant_id, connection_id=connection.id
        )
        await self._audit.record(
            action="connection.disable",
            outcome=AuditOutcome.SUCCESS,
            tenant_id=tenant_context.tenant_id,
            principal_id=tenant_context.principal_id,
            resource_type="CustomerDatabaseConnection",
            resource_id=connection.id,
            correlation_id=tenant_context.correlation_id,
        )
        return connection

    async def rotate_secret(
        self, connection_id: uuid.UUID, *, tenant_context: TenantContext, new_secret_value: str
    ) -> CustomerDatabaseConnection:
        connection = await self._connections.require(connection_id, tenant_context=tenant_context)
        old_credential = await self._credentials.require(
            connection.credential_reference_id, tenant_context=tenant_context
        )

        new_reference_key = await self._secret_provider.store(secret_value=new_secret_value)
        new_credential = await self._credentials.add(
            DatabaseCredentialReference(
                provider=old_credential.provider, reference_key=new_reference_key
            ),
            tenant_context=tenant_context,
        )

        old_reference_key = old_credential.reference_key
        old_credential.is_active = False
        old_credential.rotated_at = datetime.now(UTC)
        connection.credential_reference_id = new_credential.id
        # Force re-authentication with the new secret on next use.
        connection.status = ConnectionStatus.PENDING_VALIDATION

        await self._pool_registry.evict(
            tenant_id=tenant_context.tenant_id, connection_id=connection.id
        )
        await self._secret_provider.delete(old_reference_key)

        await self._audit.record(
            action="connection.rotate_secret",
            outcome=AuditOutcome.SUCCESS,
            tenant_id=tenant_context.tenant_id,
            principal_id=tenant_context.principal_id,
            resource_type="CustomerDatabaseConnection",
            resource_id=connection.id,
            correlation_id=tenant_context.correlation_id,
        )
        return connection

    async def delete_connection(
        self, connection_id: uuid.UUID, *, tenant_context: TenantContext
    ) -> None:
        connection = await self._connections.require(connection_id, tenant_context=tenant_context)
        credential = await self._credentials.get(
            connection.credential_reference_id, tenant_context=tenant_context
        )

        await self._pool_registry.evict(
            tenant_id=tenant_context.tenant_id, connection_id=connection.id
        )
        await self._connections.delete(connection, tenant_context=tenant_context)
        if credential is not None:
            await self._credentials.delete(credential, tenant_context=tenant_context)
            await self._secret_provider.delete(credential.reference_key)

        await self._audit.record(
            action="connection.delete",
            outcome=AuditOutcome.SUCCESS,
            tenant_id=tenant_context.tenant_id,
            principal_id=tenant_context.principal_id,
            resource_type="CustomerDatabaseConnection",
            resource_id=connection_id,
            correlation_id=tenant_context.correlation_id,
        )

    async def get_connection(
        self, connection_id: uuid.UUID, *, tenant_context: TenantContext
    ) -> CustomerDatabaseConnection:
        return await self._connections.require(connection_id, tenant_context=tenant_context)

    async def list_connections(
        self, *, tenant_context: TenantContext
    ) -> list[CustomerDatabaseConnection]:
        return await self._connections.list_for_tenant(tenant_context=tenant_context)


__all__ = ["ConnectionLifecycleManager", "ConnectionNotReadyError", "TenantMismatchError"]
