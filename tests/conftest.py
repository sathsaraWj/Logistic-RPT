"""Shared pytest fixtures for the control-plane data model tests.

Uses an in-memory SQLite database (via aiosqlite) rather than PostgreSQL so these tests run
fast, everywhere, with no external services — this is intentional for `tests/unit` and
`tests/security`. It means PostgreSQL-specific behaviour (Row-Level Security) is **not**
exercised here; RLS is validated separately against a real PostgreSQL instance
(`tests/integration`, requires `make up`), consistent with RLS being defense in depth on top
of, not a replacement for, the application-layer checks these tests do cover.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from hermes_rpt.common import model_registry  # noqa: F401  (populates Base.metadata)
from hermes_rpt.common.db import Base
from hermes_rpt.tenants.enums import RoleName
from hermes_rpt.tenants.models import Role


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    # StaticPool: a bare `sqlite+aiosqlite:///:memory:` engine can otherwise hand out more than
    # one connection, and each connection to `:memory:` is its own empty database — StaticPool
    # pins the engine to a single connection so everything in a test actually shares one DB.
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with session_factory() as db_session:
        yield db_session

    await engine.dispose()


@pytest_asyncio.fixture
async def seeded_roles(session: AsyncSession) -> dict[RoleName, Role]:
    """Mirrors migrations/versions/96b4009ff8d0_seed_roles_and_delivery_delay_task.py without
    actually running Alembic — tests exercise the ORM layer directly."""

    roles = {name: Role(name=name, description=name.value) for name in RoleName}
    session.add_all(roles.values())
    await session.flush()
    return roles
