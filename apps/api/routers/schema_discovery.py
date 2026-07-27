"""Schema discovery endpoints (Phase 5).

`POST .../discovery` returns as soon as a `PENDING` snapshot exists — the actual introspection
runs in a background task (`hermes_rpt.schemas.jobs.run_discovery_job`) so the HTTP request
never blocks on a potentially slow database scan. Clients poll `GET .../discovery/{id}` for
status.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Coroutine
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel

from apps.api.deps import DbSessionDep, SchemaDiscoveryServiceDep
from hermes_rpt.auth.dependencies import require_scopes
from hermes_rpt.auth.enums import ScopeName
from hermes_rpt.schemas.drift import DriftEvent
from hermes_rpt.schemas.enums import DiscoveryStatus
from hermes_rpt.schemas.jobs import run_discovery_job
from hermes_rpt.schemas.models import SchemaSnapshot
from hermes_rpt.schemas.profiling import ProfilingConfig
from hermes_rpt.tenants.context import TenantContext

router = APIRouter(prefix="/v1/connections/{connection_id}/discovery", tags=["schema-discovery"])

_DiscoverScope = Annotated[TenantContext, Depends(require_scopes(ScopeName.SCHEMA_DISCOVER))]

# asyncio.create_task() only holds a weak reference to the task it returns — without keeping a
# strong reference somewhere, the task can be garbage-collected mid-run. This module-level set
# is that strong reference, standard practice for "fire and forget" tasks; each task removes
# itself once done.
_background_tasks: set[asyncio.Task[None]] = set()


def _run_in_background(coro: Coroutine[Any, Any, None]) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


class SnapshotSummaryResponse(BaseModel):
    id: uuid.UUID
    connection_id: uuid.UUID
    sequence_number: int
    status: DiscoveryStatus
    schema_fingerprint: str | None
    has_unacknowledged_drift: bool
    created_at: datetime

    @classmethod
    def from_orm_snapshot(cls, snapshot: SchemaSnapshot) -> SnapshotSummaryResponse:
        drift_events = (snapshot.drift_summary or {}).get("events", [])
        return cls(
            id=snapshot.id,
            connection_id=snapshot.connection_id,
            sequence_number=snapshot.sequence_number,
            status=snapshot.status,
            schema_fingerprint=snapshot.schema_fingerprint,
            has_unacknowledged_drift=bool(drift_events) and snapshot.drift_acknowledged_at is None,
            created_at=snapshot.created_at,
        )


class SnapshotDetailResponse(SnapshotSummaryResponse):
    metadata_document: dict[str, Any]
    table_fingerprints: dict[str, str]
    drift_summary: dict[str, Any] | None
    drift_acknowledged_at: datetime | None
    error: str | None

    @classmethod
    def from_orm_snapshot(cls, snapshot: SchemaSnapshot) -> SnapshotDetailResponse:
        drift_events = (snapshot.drift_summary or {}).get("events", [])
        return cls(
            id=snapshot.id,
            connection_id=snapshot.connection_id,
            sequence_number=snapshot.sequence_number,
            status=snapshot.status,
            schema_fingerprint=snapshot.schema_fingerprint,
            has_unacknowledged_drift=bool(drift_events) and snapshot.drift_acknowledged_at is None,
            created_at=snapshot.created_at,
            metadata_document=snapshot.metadata_document,
            table_fingerprints=snapshot.table_fingerprints,
            drift_summary=snapshot.drift_summary,
            drift_acknowledged_at=snapshot.drift_acknowledged_at,
            error=snapshot.error,
        )


class StartDiscoveryRequest(BaseModel):
    profiling: ProfilingConfig = ProfilingConfig()


@router.post("", response_model=SnapshotSummaryResponse, status_code=status.HTTP_202_ACCEPTED)
async def start_discovery(
    connection_id: uuid.UUID,
    body: StartDiscoveryRequest,
    service: SchemaDiscoveryServiceDep,
    session: DbSessionDep,
    tenant_context: _DiscoverScope,
) -> SnapshotSummaryResponse:
    snapshot = await service.start_discovery(connection_id, tenant_context=tenant_context)
    await session.commit()

    profiling = body.profiling if body.profiling.enabled else None
    _run_in_background(
        run_discovery_job(snapshot.id, tenant_context=tenant_context, profiling=profiling)
    )
    return SnapshotSummaryResponse.from_orm_snapshot(snapshot)


@router.get("", response_model=list[SnapshotSummaryResponse])
async def list_snapshots(
    connection_id: uuid.UUID, service: SchemaDiscoveryServiceDep, tenant_context: _DiscoverScope
) -> list[SnapshotSummaryResponse]:
    snapshots = await service.list_snapshots(connection_id, tenant_context=tenant_context)
    return [SnapshotSummaryResponse.from_orm_snapshot(s) for s in snapshots]


@router.get("/{snapshot_id}", response_model=SnapshotDetailResponse)
async def get_snapshot(
    connection_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    service: SchemaDiscoveryServiceDep,
    tenant_context: _DiscoverScope,
) -> SnapshotDetailResponse:
    snapshot = await service.get_snapshot(snapshot_id, tenant_context=tenant_context)
    return SnapshotDetailResponse.from_orm_snapshot(snapshot)


@router.get("/{snapshot_id}/compare", response_model=list[DriftEvent])
async def compare_snapshots_endpoint(
    connection_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    against: uuid.UUID,
    service: SchemaDiscoveryServiceDep,
    tenant_context: _DiscoverScope,
) -> list[DriftEvent]:
    return await service.compare(snapshot_id, against, tenant_context=tenant_context)


@router.post("/{snapshot_id}/acknowledge-drift", response_model=SnapshotDetailResponse)
async def acknowledge_drift(
    connection_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    service: SchemaDiscoveryServiceDep,
    session: DbSessionDep,
    tenant_context: _DiscoverScope,
) -> SnapshotDetailResponse:
    snapshot = await service.acknowledge_drift(snapshot_id, tenant_context=tenant_context)
    await session.commit()
    return SnapshotDetailResponse.from_orm_snapshot(snapshot)
