"""Shared fixtures for the auth security tests: a FastAPI TestClient wired to the same
in-memory SQLite session `tests/conftest.py` sets up (so routes see the same seeded data the
test itself created), plus a token-issuing helper."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterable

import pytest_asyncio
from apps.api.main import create_app
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from hermes_rpt.auth.dev_tokens import issue_dev_token
from hermes_rpt.auth.enums import PrincipalType
from hermes_rpt.common.db import get_session
from hermes_rpt.common.settings import get_settings
from hermes_rpt.tenants.enums import RoleName
from hermes_rpt.tenants.models import Role, Tenant, User
from hermes_rpt.tenants.repository import TenantRepository, UserRepository
from tests.factories import make_tenant, make_user


class AuthFixture:
    def __init__(
        self, client: TestClient, tenant_a: Tenant, tenant_b: Tenant, user_a: User
    ) -> None:
        self.client = client
        self.tenant_a = tenant_a
        self.tenant_b = tenant_b
        self.user_a = user_a

    def token_for(
        self,
        *,
        tenant_id: uuid.UUID | None = None,
        principal_id: uuid.UUID | None = None,
        roles: Iterable[str] = (),
        scopes: Iterable[str] = (),
        principal_type: PrincipalType = PrincipalType.HUMAN,
        **overrides: object,
    ) -> str:
        return issue_dev_token(
            settings=get_settings(),
            tenant_id=tenant_id or self.tenant_a.id,
            principal_id=principal_id or self.user_a.id,
            roles=roles,
            scopes=scopes,
            principal_type=principal_type,
            **overrides,  # type: ignore[arg-type]
        )

    def auth_headers(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}


class _SingleSessionFactory:
    """Stands in for `hermes_rpt.common.db.get_sessionmaker()`'s return value
    (`async_sessionmaker`), but always hands back the same already-open test session instead of
    opening a new one against the real control-plane database — used for
    `app.state.db_sessionmaker`, which exception handlers use for best-effort audit writes and
    which (unlike route dependencies) `app.dependency_overrides` cannot reach."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def __call__(self) -> _SingleSessionFactory:
        return self

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, *exc_info: object) -> None:
        return None


@pytest_asyncio.fixture
async def auth_fixture(
    session: AsyncSession, seeded_roles: dict[RoleName, Role]
) -> AsyncIterator[AuthFixture]:
    tenants = TenantRepository(session)
    users = UserRepository(session)
    tenant_a = await tenants.add(make_tenant(name="Tenant Alpha", slug="auth-test-alpha"))
    tenant_b = await tenants.add(make_tenant(name="Tenant Beta", slug="auth-test-beta"))
    user_a = await users.add(make_user(email="alpha-auth@example.com"))
    await session.commit()

    app = create_app()

    async def _override_get_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _override_get_session
    app.state.db_sessionmaker = _SingleSessionFactory(session)

    with TestClient(app) as client:
        yield AuthFixture(client, tenant_a, tenant_b, user_a)
