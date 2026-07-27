"""Tenant-Alpha- and Tenant-Beta-shaped table schemas and mapping documents for the synthetic
fleet entities (Phase 9) — "different source schemas, similar canonical concepts," the same
principle Phase 7/8's Alpha (`vehicle`/`trip`) and Beta (`assets`/`jobs`) fixtures already
established, extended here to all seven entities the delivery-delay-risk feature contract
touches.
"""

from __future__ import annotations

from dataclasses import dataclass

from hermes_rpt.mappings.document import FieldMapping, MappingDocument, SourceTable, ValueSource


@dataclass(frozen=True, slots=True)
class EntitySchema:
    entity: str
    identity_field: str
    alpha_table: str
    beta_table: str
    # ontology field name -> SQL type, shared across tenants (only the column *name* differs).
    column_types: dict[str, str]
    alpha_columns: dict[str, str]  # ontology field name -> Alpha column name
    beta_columns: dict[str, str]  # ontology field name -> Beta column name

    def table(self, tenant_slug: str) -> str:
        return self.alpha_table if tenant_slug == "alpha" else self.beta_table

    def columns(self, tenant_slug: str) -> dict[str, str]:
        return self.alpha_columns if tenant_slug == "alpha" else self.beta_columns

    def ddl(self, tenant_slug: str) -> str:
        columns = self.columns(tenant_slug)
        column_defs = ", ".join(f"{columns[field]} {self.column_types[field]}" for field in columns)
        return f"CREATE TABLE {self.table(tenant_slug)} ({column_defs})"

    def mapping_document(self, tenant_slug: str) -> MappingDocument:
        columns = self.columns(tenant_slug)
        identity = {
            self.identity_field: FieldMapping(
                sources=(ValueSource(column=columns[self.identity_field]),)
            )
        }
        fields = {
            field: FieldMapping(sources=(ValueSource(column=column),))
            for field, column in columns.items()
            if field != self.identity_field
        }
        return MappingDocument(
            entity=self.entity,
            source=SourceTable(schema="main", table=self.table(tenant_slug)),
            identity=identity,
            fields=fields,
        )


VEHICLE = EntitySchema(
    entity="Vehicle",
    identity_field="vehicle_id",
    alpha_table="vehicle",
    beta_table="assets",
    column_types={
        "vehicle_id": "TEXT",
        "registration_number": "TEXT",
        "model_name": "TEXT",
        "manufacture_year": "INTEGER",
        "acquired_at": "TIMESTAMP",
        "is_active": "INTEGER",
    },
    alpha_columns={
        "vehicle_id": "vehicle_id",
        "registration_number": "registration_number",
        "model_name": "model_name",
        "manufacture_year": "manufacture_year",
        "acquired_at": "acquired_at",
        "is_active": "is_active",
    },
    beta_columns={
        "vehicle_id": "asset_id",
        "registration_number": "tag",
        "model_name": "model",
        "manufacture_year": "year_built",
        "acquired_at": "in_service_since",
        "is_active": "active_flag",
    },
)

TRIP = EntitySchema(
    entity="Trip",
    identity_field="trip_id",
    alpha_table="trip",
    beta_table="jobs",
    column_types={
        "trip_id": "TEXT",
        "vehicle_id": "TEXT",
        "driver_id": "TEXT",
        "route_id": "TEXT",
        "planned_departure_at": "TIMESTAMP",
        "planned_arrival_at": "TIMESTAMP",
        "actual_departure_at": "TIMESTAMP",
        "actual_arrival_at": "TIMESTAMP",
        "planned_distance_km": "REAL",
        "status": "TEXT",
    },
    alpha_columns={
        "trip_id": "trip_id",
        "vehicle_id": "vehicle_id",
        "driver_id": "driver_id",
        "route_id": "route_id",
        "planned_departure_at": "planned_departure_at",
        "planned_arrival_at": "planned_arrival_at",
        "actual_departure_at": "actual_departure_at",
        "actual_arrival_at": "actual_arrival_at",
        "planned_distance_km": "planned_distance_km",
        "status": "status",
    },
    beta_columns={
        "trip_id": "job_ref",
        "vehicle_id": "asset_ref",
        "driver_id": "driver_ref",
        "route_id": "path_ref",
        "planned_departure_at": "sched_depart",
        "planned_arrival_at": "sched_arrive",
        "actual_departure_at": "real_depart",
        "actual_arrival_at": "real_arrive",
        "planned_distance_km": "km_planned",
        "status": "job_status",
    },
)

MAINTENANCE_EVENT = EntitySchema(
    entity="MaintenanceEvent",
    identity_field="maintenance_event_id",
    alpha_table="maintenance_event",
    beta_table="upkeep",
    column_types={
        "maintenance_event_id": "TEXT",
        "vehicle_id": "TEXT",
        "event_type": "TEXT",
        "started_at": "TIMESTAMP",
        "completed_at": "TIMESTAMP",
    },
    alpha_columns={
        "maintenance_event_id": "maintenance_event_id",
        "vehicle_id": "vehicle_id",
        "event_type": "event_type",
        "started_at": "started_at",
        "completed_at": "completed_at",
    },
    beta_columns={
        "maintenance_event_id": "upkeep_id",
        "vehicle_id": "asset_ref",
        "event_type": "kind",
        "started_at": "begin_at",
        "completed_at": "end_at",
    },
)

ODOMETER_READING = EntitySchema(
    entity="OdometerReading",
    identity_field="odometer_reading_id",
    alpha_table="odometer_reading",
    beta_table="odometer_log",
    column_types={
        "odometer_reading_id": "TEXT",
        "vehicle_id": "TEXT",
        "reading_km": "REAL",
        "recorded_at": "TIMESTAMP",
        "source": "TEXT",
    },
    alpha_columns={
        "odometer_reading_id": "odometer_reading_id",
        "vehicle_id": "vehicle_id",
        "reading_km": "reading_km",
        "recorded_at": "recorded_at",
        "source": "source",
    },
    beta_columns={
        "odometer_reading_id": "log_id",
        "vehicle_id": "asset_ref",
        "reading_km": "km_value",
        "recorded_at": "logged_at",
        "source": "src",
    },
)

FUEL_EVENT = EntitySchema(
    entity="FuelEvent",
    identity_field="fuel_event_id",
    alpha_table="fuel_event",
    beta_table="fuel_log",
    column_types={
        "fuel_event_id": "TEXT",
        "vehicle_id": "TEXT",
        "trip_id": "TEXT",
        "quantity_litres": "REAL",
        "occurred_at": "TIMESTAMP",
    },
    alpha_columns={
        "fuel_event_id": "fuel_event_id",
        "vehicle_id": "vehicle_id",
        "trip_id": "trip_id",
        "quantity_litres": "quantity_litres",
        "occurred_at": "occurred_at",
    },
    beta_columns={
        "fuel_event_id": "fuel_id",
        "vehicle_id": "asset_ref",
        "trip_id": "job_ref",
        "quantity_litres": "litres",
        "occurred_at": "logged_at",
    },
)

ROUTE_STOP = EntitySchema(
    entity="RouteStop",
    identity_field="route_stop_id",
    alpha_table="route_stop",
    beta_table="stops",
    column_types={
        "route_stop_id": "TEXT",
        "route_id": "TEXT",
        "sequence_number": "INTEGER",
        "location_label": "TEXT",
    },
    alpha_columns={
        "route_stop_id": "route_stop_id",
        "route_id": "route_id",
        "sequence_number": "sequence_number",
        "location_label": "location_label",
    },
    beta_columns={
        "route_stop_id": "stop_id",
        "route_id": "path_ref",
        "sequence_number": "stop_no",
        "location_label": "label",
    },
)

DELIVERY = EntitySchema(
    entity="Delivery",
    identity_field="delivery_id",
    alpha_table="delivery",
    beta_table="parcels",
    column_types={
        "delivery_id": "TEXT",
        "trip_id": "TEXT",
        "planned_at": "TIMESTAMP",
        "delivered_at": "TIMESTAMP",
        "status": "TEXT",
    },
    alpha_columns={
        "delivery_id": "delivery_id",
        "trip_id": "trip_id",
        "planned_at": "planned_at",
        "delivered_at": "delivered_at",
        "status": "status",
    },
    beta_columns={
        "delivery_id": "parcel_id",
        "trip_id": "job_ref",
        "planned_at": "due_at",
        "delivered_at": "done_at",
        "status": "parcel_status",
    },
)

ALL_ENTITY_SCHEMAS: tuple[EntitySchema, ...] = (
    VEHICLE,
    TRIP,
    MAINTENANCE_EVENT,
    ODOMETER_READING,
    FUEL_EVENT,
    ROUTE_STOP,
    DELIVERY,
)
