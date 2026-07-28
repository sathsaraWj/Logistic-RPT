"""Integration tests for the real FastAPI app running against a real PostgreSQL **control
plane** (not the ephemeral SQLite `session` fixture every other test file in this project uses
— see `tests/conftest.py`'s own docstring on why SQLite is the default there).

This is the one place a genuine bug was found and fixed in Phase 17: `TimestampMixin.updated_at`
uses `onupdate=func.now()` (a server-computed column). For a row freshly `INSERT`ed in the same
flush, SQLAlchemy's asyncpg dialect populates it via an implicit `RETURNING` clause with no
problem. But for a row that was `SELECT`ed earlier and then mutated (the "load, mutate, commit"
shape every lifecycle-transition endpoint uses — `validate`/`enable`/`disable`/`rotate-secret`
on connections; `submit-for-validation`/`approve`/`activate`/`deprecate`/`reject` on mappings;
`acknowledge-drift` on schema snapshots), SQLAlchemy instead marks that column *expired* after
the flush and expects a later attribute access to trigger a fresh `SELECT` — which, under the
async ORM, requires being inside a `greenlet_spawn`-wrapped context. A plain synchronous
attribute access (exactly what building a Pydantic response model does) is not, so it raised
`sqlalchemy.exc.MissingGreenlet` — turning every one of those endpoints into a 500 the instant
they ran against real PostgreSQL. Every existing test exercised either TestClient+SQLite (no
`onupdate`-expiry-vs-greenlet interaction there) or real-Postgres+direct-service-calls (which
never serialize the mutated object into a response afterward) — never both together, so this
had never been caught. Fixed by explicitly `await session.refresh(obj)` after `commit()` and
before serializing, in every affected router handler.

Known, harmless artifact on Windows: after the actual test body (and its assertions) completes
successfully, asyncio's own event-loop-closing sequence can surface an unrelated
`AttributeError` from `asyncio.proactor_events` while tearing down a connection that lived
alongside `TestClient`'s own separate background-thread event loop — a `ProactorEventLoop`
quirk, not something this test's own code can catch (it happens in the loop's internal
bookkeeping, not in an awaited coroutine). It does not affect the pass/fail signal for the
actual assertions (visible as `1 passed` even when this secondary teardown error is also
reported) and only shows up at all when a real, already-migrated control-plane Postgres is
present locally (`TEST_CONTROL_PLANE_DATABASE_URL` set, or migrations applied to the default
`hermes_control` DB) — the default `make up`/`make test-integration` path never migrates that
database, so this test (and the artifact) simply skips there.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from hermes_rpt.auth.dev_tokens import issue_dev_token
from hermes_rpt.auth.enums import PrincipalType, ScopeName
from hermes_rpt.common import model_registry  # noqa: F401 - populates Base.metadata
from hermes_rpt.common.db import get_session
from hermes_rpt.common.settings import Settings, get_settings
from hermes_rpt.tenants.repository import TenantRepository, UserRepository
from tests.factories import make_tenant, make_user

pytestmark = pytest.mark.asyncio

_CONTROL_PLANE_DSN = os.environ.get(
    "TEST_CONTROL_PLANE_DATABASE_URL",
    "postgresql+asyncpg://hermes:hermes@localhost:5432/hermes_control",
)
_TENANT_ALPHA_DB_PORT = int(os.environ.get("TENANT_ALPHA_DB_PORT", "5433"))


async def _migrated_and_reachable() -> bool:
    """Never creates or drops schema here — this may point at a real, already-populated
    database (e.g. the Phase 17 demo stack). Only ever proceeds if migrations have already been
    applied by something else (`alembic upgrade head` / `make demo-up`); this test cleans up
    only the specific rows it creates, at teardown (see `real_postgres_session` below)."""

    try:
        engine = create_async_engine(_CONTROL_PLANE_DSN, connect_args={"timeout": 2})
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1 FROM alembic_version LIMIT 1"))
        await engine.dispose()
        return True
    except Exception:  # noqa: BLE001 - availability/migration probe, any failure means "skip"
        return False


@pytest_asyncio.fixture
async def real_postgres_session() -> AsyncIterator[AsyncSession]:
    if not await _migrated_and_reachable():
        pytest.skip(
            "Real, migrated control-plane PostgreSQL is not reachable at "
            f"{_CONTROL_PLANE_DSN} (set TEST_CONTROL_PLANE_DATABASE_URL, or run "
            "`make demo-up` — see docs/DEMO.md)"
        )
    # NullPool: this fixture's own connection is short-lived and explicitly disposed — it must
    # never be confused with the app-under-test's own separately cached engine (see this
    # module's docstring on why TestClient's app must get its *own* session via
    # `dependency_overrides`, not share a pooled connection across event loops).
    engine = create_async_engine(_CONTROL_PLANE_DSN, poolclass=NullPool)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    # A connection that was alive while a `TestClient` in this test ran its own separate event
    # loop (a background thread) sometimes can't close cleanly afterward on Windows'
    # ProactorEventLoop — cosmetic only, the test's own assertions already ran and reported by
    # this point; suppressing this avoids a spurious fixture-teardown error obscuring a real
    # pass/fail result.
    with contextlib.suppress(Exception):
        await engine.dispose()


async def test_validate_connection_over_http_against_real_postgres_does_not_crash(
    real_postgres_session: AsyncSession,
) -> None:
    """Regression test for the `MissingGreenlet` bug described in this module's docstring —
    drives `POST /v1/connections` then `POST /v1/connections/{id}/validate` through the real
    HTTP app, against a real PostgreSQL control plane, and asserts the response actually
    contains a valid `updated_at` rather than the request blowing up mid-serialization."""

    from apps.api.main import create_app

    session = real_postgres_session
    slug = f"pg-http-regression-{uuid.uuid4().hex[:8]}"
    tenant = await TenantRepository(session).add(make_tenant(name="pg-http-test", slug=slug))
    user = await UserRepository(session).add(make_user(email=f"{uuid.uuid4().hex[:8]}@example.com"))
    await session.commit()

    try:
        settings: Settings = get_settings()
        token = issue_dev_token(
            settings=settings,
            tenant_id=tenant.id,
            principal_id=user.id,
            scopes=[s.value for s in ScopeName],
            principal_type=PrincipalType.HUMAN,
        )
        headers = {"Authorization": f"Bearer {token}"}

        async def _override_get_session() -> AsyncIterator[AsyncSession]:
            yield session

        app = create_app()
        app.dependency_overrides[get_session] = _override_get_session

        with TestClient(app) as client:
            register = client.post(
                "/v1/connections",
                headers=headers,
                json={
                    "name": "primary",
                    "engine": "postgresql",
                    "host": "localhost",
                    "port": _TENANT_ALPHA_DB_PORT,
                    "database_name": "tenant_alpha",
                    "username": "alpha_app",
                    "secret_value": "alpha-dev-password",  # noqa: S106 - docker-compose fixture
                    "tls_mode": "disable",
                    "schema_allowlist": ["public"],
                    "table_allowlist": ["fleet_vehicle"],
                },
            )
            assert register.status_code == 201, register.text
            connection_id = register.json()["id"]
            assert register.json()["updated_at"] is not None

            validate = client.post(f"/v1/connections/{connection_id}/validate", headers=headers)

        assert validate.status_code == 200, validate.text
        body = validate.json()
        assert body["updated_at"] is not None
        assert body["status"] in ("active", "error")
    finally:
        # Cleanup only what this test created — deleting the tenant cascades
        # (ondelete=CASCADE on every TenantOwnedMixin table) to its own connection/credential
        # rows; the user row is separate (not tenant-owned) and deleted explicitly. Never drops
        # or truncates a table — this database is not necessarily exclusively owned by this
        # test. Uses a brand-new, short-lived engine/connection rather than the fixture's own
        # `session` — by this point `TestClient` has spun up and torn down its own separate
        # event loop in a background thread, and re-using a connection that lived through that
        # is exactly the kind of cross-loop asyncpg state this module's docstring warns about.
        cleanup_engine = create_async_engine(_CONTROL_PLANE_DSN, poolclass=NullPool)
        async with cleanup_engine.begin() as conn:
            await conn.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": tenant.id})
            await conn.execute(text("DELETE FROM users WHERE id = :id"), {"id": user.id})
        await cleanup_engine.dispose()
