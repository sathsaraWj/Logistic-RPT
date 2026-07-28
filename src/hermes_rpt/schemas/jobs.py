"""Background execution for schema discovery (Phase 5: "use background jobs for discovery so
HTTP requests do not remain open for long-running work").

The API layer creates a `PENDING` `SchemaSnapshot` synchronously (fast) and returns
immediately; the actual introspection runs here, scheduled via `asyncio.create_task` from the
request handler. This is an in-process job runner, not a durable queue — a job is lost if the
process restarts mid-run (the snapshot would be left `RUNNING` forever, a known gap, see
TASKS.md). A production deployment would run this in `apps/worker` against a durable queue
instead; that infrastructure is not built yet, and this module is written so swapping the
scheduling mechanism (keep `run_discovery_job`, change what calls it) will not require
touching `hermes_rpt.schemas.service`.
"""

from __future__ import annotations

import uuid

from hermes_rpt.common.db import get_sessionmaker
from hermes_rpt.common.logging import get_logger
from hermes_rpt.common.tenant_session import bind_tenant_for_row_level_security
from hermes_rpt.connectors.pool_registry import get_pool_registry
from hermes_rpt.connectors.postgres import PostgresConnector
from hermes_rpt.connectors.service import ConnectionLifecycleManager
from hermes_rpt.schemas.profiling import ProfilingConfig
from hermes_rpt.schemas.service import SchemaDiscoveryService
from hermes_rpt.secrets.provider import get_secret_provider
from hermes_rpt.tenants.context import TenantContext

logger = get_logger(__name__)


async def run_discovery_job(
    snapshot_id: uuid.UUID,
    *,
    tenant_context: TenantContext,
    profiling: ProfilingConfig | None = None,
) -> None:
    session_factory = get_sessionmaker()
    async with session_factory() as session:
        # A Phase 16 security review found this session never bound `app.current_tenant_id` —
        # only the FastAPI request path did (`hermes_rpt.auth.dependencies.get_tenant_context`).
        # This job's own writes (the snapshot, drift summary, audit events) ran with RLS's
        # defense-in-depth layer silently absent on real PostgreSQL, relying entirely on
        # application-layer tenant filtering holding with zero margin for error.
        await bind_tenant_for_row_level_security(session, tenant_context.tenant_id)
        manager = ConnectionLifecycleManager(
            session,
            secret_provider=get_secret_provider(),
            pool_registry=get_pool_registry(),
            connector=PostgresConnector(),
        )
        service = SchemaDiscoveryService(session, connection_manager=manager)
        try:
            await service.run_discovery(
                snapshot_id, tenant_context=tenant_context, profiling=profiling
            )
            await session.commit()
        except Exception:
            await session.rollback()
            logger.error("schema_discovery_job_failed", snapshot_id=str(snapshot_id))
