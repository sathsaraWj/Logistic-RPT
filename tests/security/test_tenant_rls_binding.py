"""Proves `hermes_rpt.auth.dependencies.get_tenant_context` actually calls
`bind_tenant_for_row_level_security` during a real request — the wiring gap this was added to
close (see tests/unit/test_tenant_session.py for the helper's own behavior). The test fixture's
session is SQLite, so the RLS statement itself is a no-op there (dialect-gated); what's under
test here is that the *call* happens with the right tenant, not the SQL execution.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from hermes_rpt.auth import dependencies as auth_dependencies
from tests.security.conftest import AuthFixture


async def test_get_tenant_context_binds_the_tenant_for_row_level_security(
    auth_fixture: AuthFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = AsyncMock()
    monkeypatch.setattr(auth_dependencies, "bind_tenant_for_row_level_security", spy)

    token = auth_fixture.token_for(scopes=["tenant:read"])
    response = auth_fixture.client.get("/v1/memberships", headers=auth_fixture.auth_headers(token))

    assert response.status_code == 200
    spy.assert_awaited_once()
    (_session, tenant_id), _kwargs = spy.call_args
    assert tenant_id == auth_fixture.tenant_a.id
