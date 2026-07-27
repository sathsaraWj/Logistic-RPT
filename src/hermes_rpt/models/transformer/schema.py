"""Fixed per-entity record schemas (Phase 11) — which ontology fields Hermes-RPT-0.1 reads off
each of the seven synthetic entities (`hermes_rpt.synthetic.schemas.ALL_ENTITY_SCHEMAS`), split
into numeric / categorical / datetime slots. Fixed and small deliberately: every record, of any
entity type, encodes into the *same* fixed-width tensor shape (`NUMERIC_SLOTS` /
`CATEGORICAL_SLOTS` / `DATETIME_SLOTS` columns, unused slots masked as missing) — this is what
lets `hermes_rpt.models.transformer.encoding` batch records of different entity types into one
tensor at all.

Business identifiers (`trip_id`, `vehicle_id`, ...) are deliberately included as categorical
fields — hashed, not looked up by exact identity (`hermes_rpt.models.transformer.encoding` uses
a fixed hash trick, never a per-tenant vocabulary) — because "the same id occurring on two
records" is itself a real relational signal (e.g. two `MaintenanceEvent` rows sharing a
`vehicle_id`) worth letting the model see, distinct from the *identity relationship* already
captured explicitly by relationship embeddings.
"""

from __future__ import annotations

from dataclasses import dataclass

NUMERIC_SLOTS = 2
CATEGORICAL_SLOTS = 4
DATETIME_SLOTS = 4


@dataclass(frozen=True, slots=True)
class EntityFieldSchema:
    entity: str
    numeric_fields: tuple[str, ...]
    categorical_fields: tuple[str, ...]
    datetime_fields: tuple[str, ...]

    def __post_init__(self) -> None:
        if len(self.numeric_fields) > NUMERIC_SLOTS:
            raise ValueError(f"{self.entity}: too many numeric fields for NUMERIC_SLOTS")
        if len(self.categorical_fields) > CATEGORICAL_SLOTS:
            raise ValueError(f"{self.entity}: too many categorical fields for CATEGORICAL_SLOTS")
        if len(self.datetime_fields) > DATETIME_SLOTS:
            raise ValueError(f"{self.entity}: too many datetime fields for DATETIME_SLOTS")


ENTITY_FIELD_SCHEMAS: dict[str, EntityFieldSchema] = {
    "Trip": EntityFieldSchema(
        entity="Trip",
        numeric_fields=("planned_distance_km",),
        categorical_fields=("status", "driver_id", "route_id", "vehicle_id"),
        datetime_fields=(
            "planned_departure_at",
            "planned_arrival_at",
            "actual_departure_at",
            "actual_arrival_at",
        ),
    ),
    "Vehicle": EntityFieldSchema(
        entity="Vehicle",
        numeric_fields=("manufacture_year",),
        categorical_fields=("model_name", "registration_number"),
        datetime_fields=("acquired_at",),
    ),
    "MaintenanceEvent": EntityFieldSchema(
        entity="MaintenanceEvent",
        numeric_fields=(),
        categorical_fields=("event_type", "vehicle_id"),
        datetime_fields=("started_at", "completed_at"),
    ),
    "OdometerReading": EntityFieldSchema(
        entity="OdometerReading",
        numeric_fields=("reading_km",),
        categorical_fields=("source", "vehicle_id"),
        datetime_fields=("recorded_at",),
    ),
    "FuelEvent": EntityFieldSchema(
        entity="FuelEvent",
        numeric_fields=("quantity_litres",),
        categorical_fields=("vehicle_id", "trip_id"),
        datetime_fields=("occurred_at",),
    ),
    "RouteStop": EntityFieldSchema(
        entity="RouteStop",
        numeric_fields=("sequence_number",),
        categorical_fields=("route_id", "location_label"),
        datetime_fields=(),
    ),
    "Delivery": EntityFieldSchema(
        entity="Delivery",
        numeric_fields=(),
        categorical_fields=("status", "trip_id"),
        datetime_fields=("planned_at", "delivered_at"),
    ),
}

# Fixed vocabulary of entity types — an nn.Embedding index, never a free-form string at the
# tensor level. Index 0 is reserved for padding (hermes_rpt.models.transformer.encoding).
ENTITY_TYPE_VOCAB: tuple[str, ...] = ("__pad__", *ENTITY_FIELD_SCHEMAS.keys())

# Fixed vocabulary of explicit relationships (Phase 11 requirement: "represent relationships
# explicitly") — the target record itself uses "__target__"; every related record is tagged
# with exactly one of these, matching the relation it was fetched through
# (hermes_rpt.models.transformer.context.RELATION_SPECS).
RELATIONSHIP_VOCAB: tuple[str, ...] = (
    "__pad__",
    "__target__",
    "uses_vehicle",
    "assigned_driver_history",
    "follows_route_history",
    "has_deliveries",
    "has_maintenance_events",
    "has_fuel_events",
)
