"""Adversarial test for "cross-tenant worker jobs" (prompts.txt Prompt 16): the schema-discovery
background job (`hermes_rpt.schemas.jobs.run_discovery_job`) opens its own DB session, separate
from the HTTP request's — a Phase 16 security review found that session never bound
`app.current_tenant_id` for PostgreSQL RLS, unlike the request path
(`hermes_rpt.auth.dependencies.get_tenant_context`). This proves the fix: the job's session gets
bound with the *caller's authenticated* tenant_id before it does anything else, so RLS's
defense-in-depth layer is not silently absent for background-job writes.

The snapshot_id passed in deliberately doesn't exist, so `SchemaDiscoveryService.run_discovery`
raises past the binding call — `run_discovery_job` swallows that (logs + rolls back, matching
its existing "don't crash the event loop over a failed background job" contract). What's under
test is that the RLS bind happens, with the right tenant, *before* that failure — not the
discovery outcome itself.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from hermes_rpt.schemas import jobs as jobs_module
from hermes_rpt.tenants.context import TenantContext


class _SingleSessionFactory:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def __call__(self) -> _SingleSessionFactory:
        return self

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, *exc_info: object) -> None:
        return None


async def test_run_discovery_job_binds_the_caller_tenant_before_touching_the_database(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(jobs_module, "get_sessionmaker", lambda: _SingleSessionFactory(session))

    spy = AsyncMock()
    monkeypatch.setattr(jobs_module, "bind_tenant_for_row_level_security", spy)

    caller_tenant_id = uuid.uuid4()
    tenant_context = TenantContext(tenant_id=caller_tenant_id, principal_id=uuid.uuid4())

    # The snapshot doesn't exist, so this raises internally — run_discovery_job swallows it.
    await jobs_module.run_discovery_job(uuid.uuid4(), tenant_context=tenant_context)

    spy.assert_awaited_once_with(session, caller_tenant_id)
