"""PostgreSQL connector: the only functional `DatabaseConnector` implementation right now.

Every engine this builds is, by construction:

* **Read-only at the database session level** — `default_transaction_read_only=on` is set as a
  Postgres session default via `server_settings`, so a write statement fails with a Postgres
  error even if it somehow got past the application-layer query guard. This is defense in
  depth on top of `hermes_rpt.connectors.query_guard`, not instead of it.
* **Bounded** — small pool size (concurrency limit), connection and statement timeouts.
* **TLS-aware** — `tls_mode` controls whether/how SSL is required.

One engine is built per `(tenant_id, connection_id)` by
`hermes_rpt.connectors.pool_registry.TenantConnectionPoolRegistry` — this module has no notion
of tenants at all, deliberately, so it cannot itself get tenant scoping wrong.
"""

from __future__ import annotations

import asyncio
import ssl as ssl_module

from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from hermes_rpt.connectors.errors import ConnectionFailedError
from hermes_rpt.connectors.interfaces import ConnectionTarget

_CONNECT_TIMEOUT_SECONDS = 5
_STATEMENT_TIMEOUT_MS = 10_000
_POOL_SIZE = 2
_MAX_OVERFLOW = 3


def _build_ssl_context(tls_mode: str) -> ssl_module.SSLContext | None:
    if tls_mode == "disable":
        return None
    context = ssl_module.create_default_context()
    if tls_mode == "require":
        # Encrypt, but don't require the customer's certificate to chain to a known CA — many
        # on-prem Postgres instances use self-signed certs. Full chain/hostname verification is
        # `verify-full`, below.
        context.check_hostname = False
        context.verify_mode = ssl_module.CERT_NONE
    # "verify-full" (or anything else) keeps the default_context's strict verification.
    return context


class PostgresConnector:
    def build_engine(self, target: ConnectionTarget) -> AsyncEngine:
        url = URL.create(
            drivername="postgresql+asyncpg",
            username=target.username,
            password=target.password,
            host=target.host,
            port=target.port,
            database=target.database,
        )
        return create_async_engine(
            url,
            pool_size=_POOL_SIZE,
            max_overflow=_MAX_OVERFLOW,
            pool_pre_ping=True,
            connect_args={
                "timeout": _CONNECT_TIMEOUT_SECONDS,
                "ssl": _build_ssl_context(target.tls_mode),
                "server_settings": {
                    "default_transaction_read_only": "on",
                    "statement_timeout": str(_STATEMENT_TIMEOUT_MS),
                },
            },
        )

    async def check_connectivity(self, engine: AsyncEngine) -> None:
        try:
            async with asyncio.timeout(_CONNECT_TIMEOUT_SECONDS + 2):
                async with engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
        except TimeoutError as exc:
            raise ConnectionFailedError("connection attempt timed out") from exc
        except Exception as exc:  # noqa: BLE001 - re-raised sanitized below
            raise ConnectionFailedError(_safe_summary(exc)) from exc

    async def dispose(self, engine: AsyncEngine) -> None:
        await engine.dispose()


def _safe_summary(exc: Exception) -> str:
    """Reduces a driver exception to its class name only — never its message, which for some
    drivers/misconfigurations could echo back part of the DSN or a hostname."""

    return f"{type(exc).__module__}.{type(exc).__name__}"
