"""Feature contract for the `delivery-delay-risk` prediction task (Phase 8 — the platform's
first `PredictionTaskRegistry` entry, per docs/IMPLEMENTATION_PLAN.md §4).

Every `FeatureSpec` here is data, not code: the actual extraction logic
(`hermes_rpt.features.compiler`) is a small, fixed set of safe query "recipes" keyed by
`FeatureKind`, never a per-feature SQL fragment or generated code — "the model must not
generate or execute arbitrary SQL" (Phase 8 requirement).

Every `RELATED_*` feature joins the target entity (Trip) to one related ontology entity through
a single shared field name — e.g. `Trip.vehicle_id` and `MaintenanceEvent.vehicle_id`, or
`Trip.trip_id` (Trip's own identity) and `Delivery.trip_id`. This is a deliberate scope
boundary for this phase: a *single* hop, never an arbitrary join chain. Two features that would
naturally need a multi-hop join in the full ontology (delivery-level lateness attributed to a
driver; package-level load weight) are implemented here as single-hop proxies instead — see
their `leakage_note`/`description` for exactly what was simplified and why, rather than
silently claiming a fidelity the extractor doesn't have.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FeatureDataType(StrEnum):
    FLOAT = "float"
    INTEGER = "integer"
    BOOLEAN = "boolean"


class MissingValueBehavior(StrEnum):
    """How to fill a feature's value when it cannot be extracted — e.g. a tenant with no
    mapping for the related entity, or a target/related row with no qualifying data yet.
    Never silently a magic sentinel baked into `data_type` — see docs/ONTOLOGY.md's "missing
    values are explicitly represented" principle, applied here to features."""

    NULL = "null"  # left unset (None) — the training/inference pipeline must handle it
    ZERO = "zero"
    CONSTANT = "constant"  # uses `default_value`


class LeakageRisk(StrEnum):
    """Every feature is reviewed for whether it could see information from *after* the
    prediction timestamp. NONE features use data inherently available at planning time;
    MITIGATED features had a real leakage risk that the `kind`'s window/cutoff handles —
    `leakage_note` says how."""

    NONE = "none"
    MITIGATED = "mitigated"


class FeatureKind(StrEnum):
    """The fixed set of safe extraction recipes `hermes_rpt.features.compiler` implements.
    A `FeatureSpec` only ever selects one of these — there is no way to express a feature that
    isn't one of these shapes."""

    DIRECT_FIELD = "direct_field"  # a mapped field straight off the target entity
    DERIVED_FROM_TARGET = "derived_from_target"  # computed in Python from target-entity fields
    RELATED_COUNT = "related_count"  # count of related rows in an optional window, optional filter
    RELATED_RATIO = "related_ratio"  # fraction of related rows matching a filter, in a window
    RELATED_RECENCY_DAYS = "related_recency_days"  # days since the most recent related row
    RELATED_LATEST_VALUE = "related_latest_value"  # most recent (or only) value of a related field


class FeatureSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    canonical_source: str
    data_type: FeatureDataType
    missing_value_behavior: MissingValueBehavior = MissingValueBehavior.NULL
    default_value: float | None = None
    leakage_risk: LeakageRisk
    leakage_note: str = ""
    historical_window_days: int | None = None
    availability_timestamp: str
    required: bool = False
    kind: FeatureKind

    # DIRECT_FIELD / DERIVED_FROM_TARGET
    target_field: str | None = None
    target_field_secondary: str | None = None
    derive: str | None = None

    # RELATED_* — `related_fk_field_on_target` is a field name that exists on *both* the
    # target's mapping and the related entity's mapping (as a plain value match, not
    # necessarily a "real" foreign key direction) — e.g. "vehicle_id" on both Trip and
    # MaintenanceEvent, or "trip_id" (Trip's own identity) on both Trip and Delivery.
    related_entity: str | None = None
    related_fk_field_on_target: str | None = None
    related_timestamp_field: str | None = None
    related_filter_field: str | None = None
    related_filter_equals: str | None = None
    related_value_field: str | None = None

    @model_validator(mode="after")
    def _validate_kind_specific_fields(self) -> FeatureSpec:
        if self.kind in (FeatureKind.DIRECT_FIELD, FeatureKind.DERIVED_FROM_TARGET):
            if not self.target_field:
                raise ValueError(f"{self.name}: target_field is required for kind={self.kind}")
            if self.kind == FeatureKind.DERIVED_FROM_TARGET and not self.derive:
                raise ValueError(f"{self.name}: derive is required for DERIVED_FROM_TARGET")
            return self

        if not self.related_entity or not self.related_fk_field_on_target:
            raise ValueError(
                f"{self.name}: related_entity/related_fk_field_on_target are required for "
                f"kind={self.kind}"
            )
        needs_timestamp = (
            self.kind
            in (
                FeatureKind.RELATED_RECENCY_DAYS,
                FeatureKind.RELATED_RATIO,
            )
            or self.historical_window_days is not None
        )
        if needs_timestamp and not self.related_timestamp_field:
            raise ValueError(
                f"{self.name}: related_timestamp_field is required for kind={self.kind}"
            )
        if self.kind == FeatureKind.RELATED_RATIO and (
            self.related_filter_field is None or self.related_filter_equals is None
        ):
            raise ValueError(
                f"{self.name}: related_filter_field/related_filter_equals are required for "
                "RELATED_RATIO"
            )
        if self.kind == FeatureKind.RELATED_LATEST_VALUE and self.related_value_field is None:
            raise ValueError(
                f"{self.name}: related_value_field is required for RELATED_LATEST_VALUE"
            )
        return self


class FeatureContract(BaseModel):
    model_config = ConfigDict(frozen=True)

    task_key: str
    version: str
    target_entity: str
    features: tuple[FeatureSpec, ...] = Field(min_length=1)

    def get_feature(self, name: str) -> FeatureSpec:
        for feature in self.features:
            if feature.name == name:
                return feature
        raise KeyError(f"Unknown feature: {name!r}")


# ---------------------------------------------------------------------------------------------
# The delivery-delay-risk contract (Phase 8's one task — docs/IMPLEMENTATION_PLAN.md §4).
# "Do not assume all customers have every feature": only the three features taken directly off
# the target Trip record are `required=True`; everything derived from a related entity a
# tenant might not have mapped is optional.
# ---------------------------------------------------------------------------------------------

DELIVERY_DELAY_RISK_CONTRACT = FeatureContract(
    task_key="delivery-delay-risk",
    version="1",
    target_entity="Trip",
    features=(
        FeatureSpec(
            name="planned_departure_hour",
            description="Hour of day (0-23) the trip was planned to depart.",
            canonical_source="Trip.planned_departure_at",
            data_type=FeatureDataType.INTEGER,
            leakage_risk=LeakageRisk.NONE,
            availability_timestamp="At trip planning time — always known before departure.",
            required=True,
            kind=FeatureKind.DERIVED_FROM_TARGET,
            target_field="planned_departure_at",
            derive="hour_of_day",
        ),
        FeatureSpec(
            name="day_of_week",
            description="Day of week (0=Monday) the trip was planned to depart.",
            canonical_source="Trip.planned_departure_at",
            data_type=FeatureDataType.INTEGER,
            leakage_risk=LeakageRisk.NONE,
            availability_timestamp="At trip planning time.",
            required=True,
            kind=FeatureKind.DERIVED_FROM_TARGET,
            target_field="planned_departure_at",
            derive="day_of_week",
        ),
        FeatureSpec(
            name="planned_trip_distance_km",
            description="Planned trip distance in kilometres.",
            canonical_source="Trip.planned_distance_km",
            data_type=FeatureDataType.FLOAT,
            leakage_risk=LeakageRisk.NONE,
            availability_timestamp="At trip planning time.",
            required=True,
            kind=FeatureKind.DIRECT_FIELD,
            target_field="planned_distance_km",
        ),
        FeatureSpec(
            name="number_of_stops",
            description="Number of planned stops on the trip's route.",
            canonical_source="RouteStop (joined on route_id)",
            data_type=FeatureDataType.INTEGER,
            missing_value_behavior=MissingValueBehavior.ZERO,
            leakage_risk=LeakageRisk.NONE,
            availability_timestamp="At trip planning time — route stops are planned in advance.",
            kind=FeatureKind.RELATED_COUNT,
            related_entity="RouteStop",
            related_fk_field_on_target="route_id",
        ),
        FeatureSpec(
            name="vehicle_age_years",
            description="Vehicle age in years at the prediction timestamp.",
            canonical_source="Vehicle.acquired_at (joined on vehicle_id)",
            data_type=FeatureDataType.FLOAT,
            leakage_risk=LeakageRisk.NONE,
            availability_timestamp="Known as soon as the vehicle record exists.",
            kind=FeatureKind.RELATED_LATEST_VALUE,
            related_entity="Vehicle",
            related_fk_field_on_target="vehicle_id",
            related_value_field="acquired_at",
            derive="age_years_at_prediction",
        ),
        FeatureSpec(
            name="odometer_km",
            description="Vehicle odometer reading closest to (and before) the prediction time.",
            canonical_source="OdometerReading (joined on vehicle_id)",
            data_type=FeatureDataType.FLOAT,
            leakage_risk=LeakageRisk.MITIGATED,
            leakage_note="Uses the latest reading strictly before prediction_time.",
            availability_timestamp="As of the most recent odometer reading before prediction_time.",
            kind=FeatureKind.RELATED_LATEST_VALUE,
            related_entity="OdometerReading",
            related_fk_field_on_target="vehicle_id",
            related_timestamp_field="recorded_at",
            related_value_field="reading_km",
        ),
        FeatureSpec(
            name="days_since_last_maintenance",
            description="Days since the vehicle's most recent completed maintenance event.",
            canonical_source="MaintenanceEvent (joined on vehicle_id)",
            data_type=FeatureDataType.FLOAT,
            leakage_risk=LeakageRisk.MITIGATED,
            leakage_note="Only maintenance events completed strictly before prediction_time count.",
            availability_timestamp=(
                "As of the most recent maintenance event before prediction_time."
            ),
            kind=FeatureKind.RELATED_RECENCY_DAYS,
            related_entity="MaintenanceEvent",
            related_fk_field_on_target="vehicle_id",
            related_timestamp_field="completed_at",
        ),
        FeatureSpec(
            name="previous_breakdown_count",
            description=(
                "Count of unscheduled maintenance events for the vehicle in the last 180 days."
            ),
            canonical_source="MaintenanceEvent (joined on vehicle_id)",
            data_type=FeatureDataType.INTEGER,
            missing_value_behavior=MissingValueBehavior.ZERO,
            leakage_risk=LeakageRisk.MITIGATED,
            leakage_note="Window is strictly before prediction_time.",
            availability_timestamp="As of prediction_time, looking back 180 days.",
            historical_window_days=180,
            kind=FeatureKind.RELATED_COUNT,
            related_entity="MaintenanceEvent",
            related_fk_field_on_target="vehicle_id",
            related_timestamp_field="started_at",
            related_filter_field="event_type",
            related_filter_equals="unscheduled",
        ),
        FeatureSpec(
            name="driver_recent_trip_count",
            description="Count of the driver's trips in the last 30 days.",
            canonical_source="Trip (self-joined on driver_id)",
            data_type=FeatureDataType.INTEGER,
            missing_value_behavior=MissingValueBehavior.ZERO,
            leakage_risk=LeakageRisk.MITIGATED,
            leakage_note="Window is strictly before prediction_time.",
            availability_timestamp="As of prediction_time, looking back 30 days.",
            historical_window_days=30,
            kind=FeatureKind.RELATED_COUNT,
            related_entity="Trip",
            related_fk_field_on_target="driver_id",
            related_timestamp_field="planned_departure_at",
        ),
        FeatureSpec(
            name="driver_late_delivery_ratio",
            description="Fraction of the driver's trips in the last 90 days that were cancelled "
            "— a single-hop proxy for delivery-level lateness, which would need a multi-hop "
            "join (Trip -> Driver, Delivery -> Trip) beyond this phase's scope; see leakage_note.",
            canonical_source="Trip (self-joined on driver_id)",
            data_type=FeatureDataType.FLOAT,
            leakage_risk=LeakageRisk.MITIGATED,
            leakage_note=(
                "Window is strictly before prediction_time. Proxy metric — see description."
            ),
            availability_timestamp="As of prediction_time, looking back 90 days.",
            historical_window_days=90,
            kind=FeatureKind.RELATED_RATIO,
            related_entity="Trip",
            related_fk_field_on_target="driver_id",
            related_timestamp_field="planned_departure_at",
            related_filter_field="status",
            related_filter_equals="cancelled",
        ),
        FeatureSpec(
            name="route_historical_delay_ratio",
            description=(
                "Fraction of trips on the same route in the last 90 days that were cancelled."
            ),
            canonical_source="Trip (self-joined on route_id)",
            data_type=FeatureDataType.FLOAT,
            leakage_risk=LeakageRisk.MITIGATED,
            leakage_note="Window is strictly before prediction_time.",
            availability_timestamp="As of prediction_time, looking back 90 days.",
            historical_window_days=90,
            kind=FeatureKind.RELATED_RATIO,
            related_entity="Trip",
            related_fk_field_on_target="route_id",
            related_timestamp_field="planned_departure_at",
            related_filter_field="status",
            related_filter_equals="cancelled",
        ),
        FeatureSpec(
            name="planned_delivery_count",
            description="Number of deliveries planned for this trip — a single-hop proxy for "
            "load quantity; per-package weight (Package.weight_kg) needs a multi-hop join "
            "(Package -> Order -> Delivery -> Trip) beyond this phase's scope.",
            canonical_source="Delivery (joined on trip_id)",
            data_type=FeatureDataType.INTEGER,
            missing_value_behavior=MissingValueBehavior.ZERO,
            leakage_risk=LeakageRisk.NONE,
            availability_timestamp="At trip planning time — deliveries are planned in advance.",
            kind=FeatureKind.RELATED_COUNT,
            related_entity="Delivery",
            related_fk_field_on_target="trip_id",
        ),
        FeatureSpec(
            name="loading_start_delay_minutes",
            description="Minutes between planned and actual departure, if the trip has already "
            "departed as of prediction_time (0 otherwise).",
            canonical_source="Trip.actual_departure_at, Trip.planned_departure_at",
            data_type=FeatureDataType.FLOAT,
            missing_value_behavior=MissingValueBehavior.ZERO,
            leakage_risk=LeakageRisk.MITIGATED,
            leakage_note="The compiler only uses actual_departure_at when it is strictly "
            "before prediction_time — never a value from after it.",
            availability_timestamp="As of prediction_time, only if the trip has already departed.",
            kind=FeatureKind.DERIVED_FROM_TARGET,
            target_field="actual_departure_at",
            target_field_secondary="planned_departure_at",
            derive="loading_start_delay_minutes",
        ),
        FeatureSpec(
            name="recent_fuel_anomaly_count",
            description="Count of the vehicle's fuel events in the last 30 days (proxy for "
            "anomalous refuelling activity — a simple count until a real anomaly model exists).",
            canonical_source="FuelEvent (joined on vehicle_id)",
            data_type=FeatureDataType.INTEGER,
            missing_value_behavior=MissingValueBehavior.ZERO,
            leakage_risk=LeakageRisk.MITIGATED,
            leakage_note="Window is strictly before prediction_time.",
            availability_timestamp="As of prediction_time, looking back 30 days.",
            historical_window_days=30,
            kind=FeatureKind.RELATED_COUNT,
            related_entity="FuelEvent",
            related_fk_field_on_target="vehicle_id",
            related_timestamp_field="occurred_at",
        ),
    ),
)
