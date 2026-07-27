"""Async SQLAlchemy engine/session plumbing for the Hermes-RPT **control-plane** database only.

This module must never be used to reach a tenant's customer database — those connections are
created per-tenant by the connector service (Phase 4) through the secret-provider abstraction,
with their own pools, timeouts and read-only transactions. Mixing the two here would defeat the
"separate connection pool per tenant" invariant (see ADR-0004) by construction, so this module
intentionally has no notion of `tenant_id` at all.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from hermes_rpt.common.settings import Settings, get_settings


class Base(DeclarativeBase):
    """Declarative base for control-plane ORM entities (Phase 2 onward)."""


@lru_cache
def get_engine(settings: Settings | None = None) -> AsyncEngine:
    settings = settings or get_settings()
    return create_async_engine(str(settings.database_url), pool_pre_ping=True)


def get_sessionmaker(
    settings: Settings | None = None,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=get_engine(settings), expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a control-plane session. Handlers should not use this
    directly for tenant-owned data — go through a repository/service (Phase 2) so the
    tenant_id filter lives in exactly one place."""

    session_factory = get_sessionmaker()
    async with session_factory() as session:
        yield session
