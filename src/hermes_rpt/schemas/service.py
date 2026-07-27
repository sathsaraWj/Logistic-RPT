"""Schema discovery orchestration: create a snapshot, introspect, fingerprint, diff against the
previous completed snapshot, optionally profile, and record the result. `start_discovery` is
fast (just creates a PENDING row) so it can run inside an HTTP request;
`run_discovery` is the actual work and is meant to run in a background task
(`hermes_rpt.schemas.jobs`) — Phase 5 requirement "use background jobs ... so HTTP requests do
not remain open for long-running work."
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from hermes_rpt.audit.enums import AuditOutcome
from hermes_rpt.audit.service import AuditService
from hermes_rpt.connectors.service import ConnectionLifecycleManager
from hermes_rpt.schemas.drift import DriftEvent, compare_snapshots
from hermes_rpt.schemas.enums import DiscoveryStatus
from hermes_rpt.schemas.fingerprint import compute_fingerprints
from hermes_rpt.schemas.introspection import PostgresSchemaIntrospector, SchemaIntrospectionResult
from hermes_rpt.schemas.models import SchemaSnapshot
from hermes_rpt.schemas.profiling import ProfilingConfig, TableProfiler
from hermes_rpt.schemas.repository import SchemaSnapshotRepository
from hermes_rpt.tenants.context import TenantContext


def _safe_error_summary(exc: Exception) -> str:
    """Class name only — never `str(exc)`, which for a driver-level failure could echo back
    connection details (see hermes_rpt.connectors.postgres._safe_summary, the same policy)."""

    return f"{type(exc).__module__}.{type(exc).__name__}"


class SchemaDiscoveryService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        connection_manager: ConnectionLifecycleManager,
        introspector: PostgresSchemaIntrospector | None = None,
        profiler: TableProfiler | None = None,
    ) -> None:
        self._session = session
        self._snapshots = SchemaSnapshotRepository(session)
        self._connection_manager = connection_manager
        self._introspector = introspector or PostgresSchemaIntrospector()
        self._profiler = profiler or TableProfiler()
        self._audit = AuditService(session)

    async def start_discovery(
        self, connection_id: uuid.UUID, *, tenant_context: TenantContext
    ) -> SchemaSnapshot:
        sequence_number = await self._snapshots.next_sequence_number(
            connection_id, tenant_context=tenant_context
        )
        snapshot = await self._snapshots.add(
            SchemaSnapshot(
                connection_id=connection_id,
                sequence_number=sequence_number,
                status=DiscoveryStatus.PENDING,
            ),
            tenant_context=tenant_context,
        )
        await self._audit.record(
            action="schema_discovery.start",
            outcome=AuditOutcome.SUCCESS,
            tenant_id=tenant_context.tenant_id,
            principal_id=tenant_context.principal_id,
            resource_type="SchemaSnapshot",
            resource_id=snapshot.id,
            correlation_id=tenant_context.correlation_id,
        )
        return snapshot

    async def run_discovery(
        self,
        snapshot_id: uuid.UUID,
        *,
        tenant_context: TenantContext,
        profiling: ProfilingConfig | None = None,
    ) -> SchemaSnapshot:
        snapshot = await self._snapshots.require(snapshot_id, tenant_context=tenant_context)
        snapshot.status = DiscoveryStatus.RUNNING

        try:
            connection, engine = await self._connection_manager.get_or_create_engine(
                snapshot.connection_id, tenant_context=tenant_context
            )
            result = await self._introspector.introspect(
                engine, schema_allowlist=connection.schema_allowlist
            )
            schema_fingerprint, table_fingerprints = compute_fingerprints(result)

            previous = await self._snapshots.latest_completed(
                snapshot.connection_id,
                tenant_context=tenant_context,
                before_sequence=snapshot.sequence_number,
            )
            drift_events: list[DriftEvent] = []
            if previous is not None and previous.metadata_document:
                previous_result = SchemaIntrospectionResult.model_validate(
                    previous.metadata_document
                )
                drift_events = compare_snapshots(previous_result, result)

            metadata_document = result.model_dump(mode="json")

            if profiling is not None and profiling.enabled:
                profiles = []
                for table in result.tables[: profiling.max_tables]:
                    summary = await self._profiler.profile_table(engine, table, profiling)
                    profiles.append(summary.model_dump(mode="json"))
                metadata_document["profiling"] = profiles
                await self._audit.record(
                    action="schema_discovery.profiling",
                    outcome=AuditOutcome.SUCCESS,
                    tenant_id=tenant_context.tenant_id,
                    principal_id=tenant_context.principal_id,
                    resource_type="SchemaSnapshot",
                    resource_id=snapshot.id,
                    correlation_id=tenant_context.correlation_id,
                    details={"tables_profiled": len(profiles)},
                )

            snapshot.metadata_document = metadata_document
            snapshot.table_fingerprints = table_fingerprints
            snapshot.schema_fingerprint = schema_fingerprint
            snapshot.drift_summary = (
                {"events": [e.model_dump(mode="json") for e in drift_events]}
                if previous is not None
                else None
            )
            snapshot.status = DiscoveryStatus.COMPLETED
            snapshot.error = None
        except Exception as exc:  # noqa: BLE001 - captured as a safe summary, then re-raised
            snapshot.status = DiscoveryStatus.FAILED
            snapshot.error = _safe_error_summary(exc)
            await self._audit.record(
                action="schema_discovery.run",
                outcome=AuditOutcome.ERROR,
                tenant_id=tenant_context.tenant_id,
                principal_id=tenant_context.principal_id,
                resource_type="SchemaSnapshot",
                resource_id=snapshot.id,
                correlation_id=tenant_context.correlation_id,
                details={"error": snapshot.error},
            )
            raise

        await self._audit.record(
            action="schema_discovery.run",
            outcome=AuditOutcome.SUCCESS,
            tenant_id=tenant_context.tenant_id,
            principal_id=tenant_context.principal_id,
            resource_type="SchemaSnapshot",
            resource_id=snapshot.id,
            correlation_id=tenant_context.correlation_id,
            details={"tables_found": len(result.tables), "drift_event_count": len(drift_events)},
        )
        return snapshot

    async def acknowledge_drift(
        self, snapshot_id: uuid.UUID, *, tenant_context: TenantContext
    ) -> SchemaSnapshot:
        snapshot = await self._snapshots.require(snapshot_id, tenant_context=tenant_context)
        snapshot.drift_acknowledged_at = datetime.now(UTC)
        snapshot.drift_acknowledged_by_principal_id = tenant_context.principal_id
        await self._audit.record(
            action="schema_discovery.acknowledge_drift",
            outcome=AuditOutcome.SUCCESS,
            tenant_id=tenant_context.tenant_id,
            principal_id=tenant_context.principal_id,
            resource_type="SchemaSnapshot",
            resource_id=snapshot.id,
            correlation_id=tenant_context.correlation_id,
        )
        return snapshot

    async def compare(
        self,
        snapshot_id_a: uuid.UUID,
        snapshot_id_b: uuid.UUID,
        *,
        tenant_context: TenantContext,
    ) -> list[DriftEvent]:
        snapshot_a = await self._snapshots.require(snapshot_id_a, tenant_context=tenant_context)
        snapshot_b = await self._snapshots.require(snapshot_id_b, tenant_context=tenant_context)
        result_a = SchemaIntrospectionResult.model_validate(snapshot_a.metadata_document)
        result_b = SchemaIntrospectionResult.model_validate(snapshot_b.metadata_document)
        return compare_snapshots(result_a, result_b)

    async def get_snapshot(
        self, snapshot_id: uuid.UUID, *, tenant_context: TenantContext
    ) -> SchemaSnapshot:
        return await self._snapshots.require(snapshot_id, tenant_context=tenant_context)

    async def list_snapshots(
        self, connection_id: uuid.UUID, *, tenant_context: TenantContext
    ) -> list[SchemaSnapshot]:
        return await self._snapshots.list_for_connection(
            connection_id, tenant_context=tenant_context
        )
