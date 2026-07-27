"""Per-tenant connection pool registry.

The single place engines for customer database connections live. Every entry is keyed by
`(tenant_id, connection_id)` — never by `connection_id` alone — so there is no way to look up a
pool while only being sure of the connection id: the caller must also supply (and therefore
already trust) a `tenant_id`, which in practice always comes from a verified `TenantContext`.
This is the concrete mechanism behind "never reuse one tenant's connection pool for another
tenant" and "bind every pool to both tenant_id and connection ID" (Phase 4 requirements).
"""

from __future__ import annotations

import asyncio
import uuid
from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncEngine

from hermes_rpt.connectors.interfaces import ConnectionTarget, DatabaseConnector
from hermes_rpt.connectors.postgres import PostgresConnector

PoolKey = tuple[uuid.UUID, uuid.UUID]


class TenantConnectionPoolRegistry:
    def __init__(self, connector: DatabaseConnector) -> None:
        self._connector = connector
        self._engines: dict[PoolKey, AsyncEngine] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(
        self, *, tenant_id: uuid.UUID, connection_id: uuid.UUID, target: ConnectionTarget
    ) -> AsyncEngine:
        key: PoolKey = (tenant_id, connection_id)
        async with self._lock:
            engine = self._engines.get(key)
            if engine is None:
                engine = self._connector.build_engine(target)
                self._engines[key] = engine
            return engine

    def get_existing(self, *, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> AsyncEngine | None:
        return self._engines.get((tenant_id, connection_id))

    async def evict(self, *, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> None:
        """Disposes and removes the pool for one (tenant, connection) pair. Called whenever a
        connection's credential is rotated or the connection is disabled/deleted — "evict pools
        safely when credentials change or a connection is disabled" (Phase 4 requirement)."""

        key: PoolKey = (tenant_id, connection_id)
        async with self._lock:
            engine = self._engines.pop(key, None)
        if engine is not None:
            await self._connector.dispose(engine)

    async def evict_all_for_tenant(self, *, tenant_id: uuid.UUID) -> None:
        async with self._lock:
            keys = [key for key in self._engines if key[0] == tenant_id]
        for key in keys:
            await self.evict(tenant_id=key[0], connection_id=key[1])

    def pool_count(self) -> int:
        return len(self._engines)


@lru_cache
def get_pool_registry() -> TenantConnectionPoolRegistry:
    return TenantConnectionPoolRegistry(PostgresConnector())
