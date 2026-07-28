"""Alpha/Beta table shapes and mapping documents for the five demo entities (Phase 17).

Both tenants' tables represent the same five real ontology entities (`configs/ontology/v1.yaml`
— `Vehicle`, `Driver`, `Trip`, `Delivery`, `MaintenanceEvent`) but with deliberately different
table and column names, matching prompts.txt Prompt 17's literal table lists:
`fleet_vehicle`/`fleet_driver`/`transport_trip`/`delivery_record`/`maintenance_log` for Alpha,
`assets`/`employees`/`jobs`/`consignments`/`service_orders` for Beta. DDL for both lives in
`docker/postgres-fixtures/{alpha,beta}/init.sql`; this module only builds the `MappingDocument`
each tenant needs to point their physical schema at the shared canonical ontology.
"""

from __future__ import annotations

from dataclasses import dataclass

from hermes_rpt.mappings.document import FieldMapping, MappingDocument, SourceTable, ValueSource


@dataclass(frozen=True, slots=True)
class DemoEntity:
    entity: str
    identity_field: str
    alpha_table: str
    beta_table: str
    alpha_columns: dict[str, str]  # ontology field name -> Alpha column name
    beta_columns: dict[str, str]  # ontology field name -> Beta column name

    def table(self, tenant_slug: str) -> str:
        return self.alpha_table if tenant_slug == "demo-alpha" else self.beta_table

    def columns(self, tenant_slug: str) -> dict[str, str]:
        return self.alpha_columns if tenant_slug == "demo-alpha" else self.beta_columns

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
            source=SourceTable(schema="public", table=self.table(tenant_slug)),
            identity=identity,
            fields=fields,
        )


VEHICLE = DemoEntity(
    entity="Vehicle",
    identity_field="vehicle_id",
    alpha_table="fleet_vehicle",
    beta_table="assets",
    alpha_columns={
        "vehicle_id": "vehicle_id",
        "registration_number": "registration_no",
        "make": "make",
        "model_name": "model_name",
        "manufacture_year": "manufacture_year",
        "current_odometer_km": "odometer_km",
        "is_active": "is_active",
        "acquired_at": "acquired_at",
    },
    beta_columns={
        "vehicle_id": "asset_id",
        "registration_number": "tag",
        "make": "brand",
        "model_name": "model",
        "manufacture_year": "year_built",
        "current_odometer_km": "total_km",
        "is_active": "active_flag",
        "acquired_at": "in_service_since",
    },
)

DRIVER = DemoEntity(
    entity="Driver",
    identity_field="driver_id",
    alpha_table="fleet_driver",
    beta_table="employees",
    alpha_columns={
        "driver_id": "driver_id",
        "full_name": "full_name",
        "license_number": "license_number",
        "license_expiry": "license_expiry",
        "hired_at": "hired_at",
        "is_active": "is_active",
    },
    beta_columns={
        "driver_id": "employee_id",
        "full_name": "name",
        "license_number": "licence_no",
        "license_expiry": "licence_expiry",
        "hired_at": "start_date",
        "is_active": "active",
    },
)

TRIP = DemoEntity(
    entity="Trip",
    identity_field="trip_id",
    alpha_table="transport_trip",
    beta_table="jobs",
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

DELIVERY = DemoEntity(
    entity="Delivery",
    identity_field="delivery_id",
    alpha_table="delivery_record",
    beta_table="consignments",
    alpha_columns={
        "delivery_id": "delivery_id",
        "trip_id": "trip_id",
        "order_id": "order_id",
        "planned_at": "planned_at",
        "delivered_at": "delivered_at",
        "status": "status",
    },
    beta_columns={
        "delivery_id": "consignment_id",
        "trip_id": "job_ref",
        "order_id": "order_ref",
        "planned_at": "due_at",
        "delivered_at": "done_at",
        "status": "consignment_status",
    },
)

MAINTENANCE_EVENT = DemoEntity(
    entity="MaintenanceEvent",
    identity_field="maintenance_event_id",
    alpha_table="maintenance_log",
    beta_table="service_orders",
    alpha_columns={
        "maintenance_event_id": "maintenance_event_id",
        "vehicle_id": "vehicle_id",
        "work_order_id": "work_order_id",
        "event_type": "event_type",
        "started_at": "started_at",
        "completed_at": "completed_at",
    },
    beta_columns={
        "maintenance_event_id": "service_order_id",
        "vehicle_id": "asset_ref",
        "work_order_id": "wo_ref",
        "event_type": "service_type",
        "started_at": "opened_at",
        "completed_at": "closed_at",
    },
)

ALL_DEMO_ENTITIES: tuple[DemoEntity, ...] = (VEHICLE, DRIVER, TRIP, DELIVERY, MAINTENANCE_EVENT)
