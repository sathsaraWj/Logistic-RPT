"""Builds one Hermes-RPT training/inference example — a target `Trip` plus its relational
context (vehicle, driver history, route history, deliveries, maintenance events, fuel events) —
from a tenant's mapped customer database (Phase 11).

Reuses exactly the same safety machinery Phase 8/9 already established
(`hermes_rpt.features.compiler` — allowlist checks, `< prediction_time` cutoffs, row limits,
mapping resolution): there is no second, parallel query path here, only a different *shape* of
result (individual raw records instead of aggregated scalar features), because a relational
transformer needs the former and a tabular baseline needs the latter.

Tenant isolation (Phase 11 requirements 1-2 — "do not encode tenant identity as a predictive
feature," "never combine relational contexts from different tenants") is structural, not a
separate check: every fetch here goes through one `TenantContext`, which gates which mappings
resolve and which connection pool is used (Phase 4/7), exactly as it does everywhere else in the
platform. `tenant_id` itself is never placed into a `RelationalRecord`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from hermes_rpt.connectors.models import CustomerDatabaseConnection
from hermes_rpt.connectors.repository import CustomerDatabaseConnectionRepository
from hermes_rpt.connectors.service import ConnectionLifecycleManager
from hermes_rpt.features.compiler import fetch_related_records, fetch_target_row
from hermes_rpt.features.resolver import MappingResolver, ResolvedMapping
from hermes_rpt.models.transformer.schema import ENTITY_FIELD_SCHEMAS
from hermes_rpt.schemas.repository import SchemaSnapshotRepository
from hermes_rpt.tenants.context import TenantContext


class TargetRecordNotFoundError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class RelationalRecord:
    entity: str
    relationship: str  # one of hermes_rpt.models.transformer.schema.RELATIONSHIP_VOCAB
    fields: dict[str, Any]  # raw values, keyed by ontology field name — never includes tenant_id


@dataclass(frozen=True, slots=True)
class RelationalExample:
    business_reference: str
    prediction_time: datetime
    label: int | None  # None at inference time
    target: RelationalRecord
    related: tuple[RelationalRecord, ...]


@dataclass(frozen=True, slots=True)
class _RelationSpec:
    relationship: str
    entity: str
    join_field: str
    from_target_field: str
    timestamp_field: str | None


_RELATION_SPECS: tuple[_RelationSpec, ...] = (
    _RelationSpec("uses_vehicle", "Vehicle", "vehicle_id", "vehicle_id", None),
    _RelationSpec(
        "assigned_driver_history", "Trip", "driver_id", "driver_id", "planned_departure_at"
    ),
    _RelationSpec("follows_route_history", "Trip", "route_id", "route_id", "planned_departure_at"),
    _RelationSpec("has_deliveries", "Delivery", "trip_id", "trip_id", None),
    _RelationSpec(
        "has_maintenance_events", "MaintenanceEvent", "vehicle_id", "vehicle_id", "started_at"
    ),
    _RelationSpec("has_fuel_events", "FuelEvent", "vehicle_id", "vehicle_id", "occurred_at"),
)


class RelationalContextBuilder:
    def __init__(
        self, session: AsyncSession, *, connection_manager: ConnectionLifecycleManager
    ) -> None:
        self._session = session
        self._connection_manager = connection_manager
        self._resolver = MappingResolver(session)
        self._connections = CustomerDatabaseConnectionRepository(session)
        self._snapshots = SchemaSnapshotRepository(session)

    async def build_example(
        self,
        *,
        business_reference: str,
        prediction_time: datetime,
        label: int | None,
        tenant_context: TenantContext,
        max_records_per_relation: int,
    ) -> RelationalExample:
        target_mapping = await self._resolver.resolve_target("Trip", tenant_context=tenant_context)
        target_engine, target_connection = await self._engine_for(
            target_mapping, tenant_context=tenant_context
        )
        target_schema = ENTITY_FIELD_SCHEMAS["Trip"]
        needed = (
            set(target_schema.numeric_fields)
            | set(target_schema.categorical_fields)
            | set(target_schema.datetime_fields)
        )
        raw_target = await fetch_target_row(
            target_engine,
            target_mapping.document,
            business_reference=business_reference,
            needed_fields=needed,
            schema_allowlist=target_connection.schema_allowlist,
            table_allowlist=target_connection.table_allowlist,
        )
        if raw_target is None:
            raise TargetRecordNotFoundError(
                f"No Trip row found for business_reference={business_reference!r}"
            )
        target = RelationalRecord(entity="Trip", relationship="__target__", fields=raw_target)

        related: list[RelationalRecord] = []
        for spec in _RELATION_SPECS:
            join_value = raw_target.get(spec.from_target_field)
            if join_value is None:
                continue  # e.g. a trip with no driver_id yet — nothing to fetch, not an error
            resolved = await self._resolver.resolve(spec.entity, tenant_context=tenant_context)
            if resolved is None:
                continue  # "support missing tables" — this tenant has no mapping for this entity
            engine, connection = await self._engine_for(resolved, tenant_context=tenant_context)
            rows = await fetch_related_records(
                engine,
                resolved.document,
                join_field=spec.join_field,
                join_value=join_value,
                timestamp_field=spec.timestamp_field,
                prediction_time=prediction_time,
                limit=max_records_per_relation,
                schema_allowlist=connection.schema_allowlist,
                table_allowlist=connection.table_allowlist,
            )
            related.extend(
                RelationalRecord(entity=spec.entity, relationship=spec.relationship, fields=row)
                for row in rows
            )

        return RelationalExample(
            business_reference=business_reference,
            prediction_time=prediction_time,
            label=label,
            target=target,
            related=tuple(related),
        )

    async def _engine_for(
        self, resolved: ResolvedMapping, *, tenant_context: TenantContext
    ) -> tuple[AsyncEngine, CustomerDatabaseConnection]:
        snapshot = await self._snapshots.require(
            resolved.schema_mapping.schema_snapshot_id, tenant_context=tenant_context
        )
        connection = await self._connections.require(
            snapshot.connection_id, tenant_context=tenant_context
        )
        _, engine = await self._connection_manager.get_or_create_engine(
            connection.id, tenant_context=tenant_context
        )
        return engine, connection
