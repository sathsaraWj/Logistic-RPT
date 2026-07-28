"""`DatabaseConnector`: the seam between the connection lifecycle/registry and a specific
database engine's driver. `PostgresConnector` is the only functional implementation right now;
`MySQLConnector`, `MSSQLConnector` and `OracleConnector` are reserved (Phase 4 requirement —
"prepare interfaces... do not require those additional drivers to be functional yet").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncEngine


@dataclass(frozen=True, slots=True)
class ConnectionTarget:
    """Everything needed to open a connection, assembled just-in-time from safe metadata
    (`CustomerDatabaseConnection`) plus a resolved secret (`SecretProvider.resolve`). Never
    logged, never persisted, never returned via any API — held only long enough to build an
    engine.

    `password` is `field(repr=False)` — a Phase 16 security review found the "never logged"
    claim above was only true by convention: nothing stopped `repr(target)`/`f"{target}"` (e.g.
    an accidental `logger.info("...", target=target)`) from emitting the plaintext password,
    since dataclass `__repr__` includes every field by default and `hermes_rpt.common.
    logging`'s redaction only descends into `str`/`dict`/`list` values, never into an arbitrary
    object's own `repr()`. Excluding the field from `__repr__` closes that specific path
    structurally rather than relying on every call site remembering not to log this object.
    """

    host: str
    port: int
    database: str
    username: str
    password: str = field(repr=False)
    tls_mode: str


class DatabaseConnector(Protocol):
    def build_engine(self, target: ConnectionTarget) -> AsyncEngine:
        """Synchronous on purpose: building a SQLAlchemy engine does not itself open a network
        connection (that happens lazily on first checkout), so there is nothing to await."""
        ...

    async def check_connectivity(self, engine: AsyncEngine) -> None:
        """Raises on failure. Callers are responsible for translating driver-specific
        exceptions into safe, DSN-free messages before they reach a log line or API response —
        see `hermes_rpt.connectors.errors.sanitize_connection_error`."""
        ...

    async def dispose(self, engine: AsyncEngine) -> None: ...


class UnsupportedEngineError(Exception):
    def __init__(self, engine: str) -> None:
        super().__init__(f"Database engine {engine!r} is not yet supported")


class MySQLConnector:
    """Reserved interface — not functional yet (Phase 4)."""

    def build_engine(self, target: ConnectionTarget) -> AsyncEngine:
        raise UnsupportedEngineError("mysql")

    async def check_connectivity(self, engine: AsyncEngine) -> None:
        raise UnsupportedEngineError("mysql")

    async def dispose(self, engine: AsyncEngine) -> None:
        raise UnsupportedEngineError("mysql")


class MSSQLConnector:
    """Reserved interface — not functional yet (Phase 4)."""

    def build_engine(self, target: ConnectionTarget) -> AsyncEngine:
        raise UnsupportedEngineError("mssql")

    async def check_connectivity(self, engine: AsyncEngine) -> None:
        raise UnsupportedEngineError("mssql")

    async def dispose(self, engine: AsyncEngine) -> None:
        raise UnsupportedEngineError("mssql")


class OracleConnector:
    """Reserved interface — not functional yet (Phase 4)."""

    def build_engine(self, target: ConnectionTarget) -> AsyncEngine:
        raise UnsupportedEngineError("oracle")

    async def check_connectivity(self, engine: AsyncEngine) -> None:
        raise UnsupportedEngineError("oracle")

    async def dispose(self, engine: AsyncEngine) -> None:
        raise UnsupportedEngineError("oracle")
