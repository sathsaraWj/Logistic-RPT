"""Deterministic synthetic fleet data (Phase 9) — realistic missing values, class imbalance,
delayed deliveries, and vehicle/route histories, plus a small fixed set of deliberately injected
defects so `hermes_rpt.datasets.quality`'s checks have something real to catch. Every row is
keyed by ontology field name; `hermes_rpt.synthetic.schemas` renames these into Tenant-Alpha- and
Tenant-Beta-shaped column names before insertion.

Entirely synthetic: no real customer data anywhere in this module or its output.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

_VEHICLE_COUNT = 15
_ROUTE_COUNT = 5
_DRIVER_COUNT = 8
_TRIPS_PER_DAY_RANGE = (2, 5)
_ON_TIME_DELAY_PROBABILITY = 0.88
_CANCELLED_PROBABILITY = 0.03


@dataclass(frozen=True, slots=True)
class SyntheticFleetData:
    vehicles: list[dict[str, Any]] = field(default_factory=list)
    trips: list[dict[str, Any]] = field(default_factory=list)
    maintenance_events: list[dict[str, Any]] = field(default_factory=list)
    odometer_readings: list[dict[str, Any]] = field(default_factory=list)
    fuel_events: list[dict[str, Any]] = field(default_factory=list)
    route_stops: list[dict[str, Any]] = field(default_factory=list)
    deliveries: list[dict[str, Any]] = field(default_factory=list)


def generate_fleet_data(
    *, tenant_slug: str, seed: int, start: datetime, end: datetime, inject_defects: bool = True
) -> SyntheticFleetData:
    """Deterministic for a given `(tenant_slug, seed, start, end)` — the same call always
    produces the same rows, which is what makes `hermes_rpt.datasets.manifest.
    compute_dataset_checksum` a meaningful reproducibility check end to end."""

    seed_key = f"{tenant_slug}:{seed}"
    rng = random.Random(seed_key)  # noqa: S311  # nosec B311 - synthetic data, not security-sensitive
    data = SyntheticFleetData()

    vehicle_ids = _generate_vehicles(data, rng, tenant_slug=tenant_slug, start=start)
    route_ids = _generate_routes_and_stops(data, rng, tenant_slug=tenant_slug)
    driver_ids = [f"{tenant_slug}-driver-{i:03d}" for i in range(_DRIVER_COUNT)]
    _generate_maintenance_and_odometer(
        data, rng, tenant_slug=tenant_slug, vehicle_ids=vehicle_ids, start=start, end=end
    )
    _generate_trips_deliveries_fuel(
        data,
        rng,
        tenant_slug=tenant_slug,
        vehicle_ids=vehicle_ids,
        route_ids=route_ids,
        driver_ids=driver_ids,
        start=start,
        end=end,
    )

    if inject_defects:
        _inject_defects(data, rng, end=end)

    return data


def _generate_vehicles(
    data: SyntheticFleetData, rng: random.Random, *, tenant_slug: str, start: datetime
) -> list[str]:
    vehicle_ids = [f"{tenant_slug}-veh-{i:03d}" for i in range(_VEHICLE_COUNT)]
    for i, vehicle_id in enumerate(vehicle_ids):
        acquired_at = start - timedelta(days=rng.randint(180, 365 * 6))
        data.vehicles.append(
            {
                "vehicle_id": vehicle_id,
                "registration_number": f"{tenant_slug.upper()}-{1000 + i}",
                "model_name": rng.choice(
                    ["Transit 350", "Sprinter", "Hilux", "Actros", "Cascadia"]
                ),
                "manufacture_year": acquired_at.year,
                "acquired_at": acquired_at,
                "is_active": True,
            }
        )
    return vehicle_ids


def _generate_routes_and_stops(
    data: SyntheticFleetData, rng: random.Random, *, tenant_slug: str
) -> list[str]:
    route_ids = [f"{tenant_slug}-route-{i:02d}" for i in range(_ROUTE_COUNT)]
    for route_id in route_ids:
        stop_count = rng.randint(2, 6)
        for seq in range(stop_count):
            data.route_stops.append(
                {
                    "route_stop_id": f"{route_id}-stop-{seq:02d}",
                    "route_id": route_id,
                    "sequence_number": seq,
                    "location_label": f"Stop {seq}",
                }
            )
    return route_ids


def _generate_maintenance_and_odometer(
    data: SyntheticFleetData,
    rng: random.Random,
    *,
    tenant_slug: str,
    vehicle_ids: list[str],
    start: datetime,
    end: datetime,
) -> None:
    window_days = max((end - start).days, 1)
    for vehicle_id in vehicle_ids:
        odometer_km = rng.uniform(5_000, 120_000)
        for week in range(0, window_days, 7):
            recorded_at = start + timedelta(days=week, hours=rng.uniform(0, 23))
            odometer_km += rng.uniform(200, 900)
            data.odometer_readings.append(
                {
                    "odometer_reading_id": f"{vehicle_id}-odo-{week:04d}",
                    "vehicle_id": vehicle_id,
                    "reading_km": round(odometer_km, 1),
                    "recorded_at": recorded_at,
                    # ~15% of readings have no recorded source — realistic missingness.
                    "source": None if rng.random() < 0.15 else rng.choice(["telematics", "manual"]),
                }
            )

        for m in range(rng.randint(2, 5)):
            started_at = start + timedelta(
                days=rng.randint(0, window_days - 1), hours=rng.uniform(0, 23)
            )
            event_type = rng.choices(["scheduled", "unscheduled", "inspection"], weights=[5, 2, 3])[
                0
            ]
            duration_hours = rng.uniform(1, 48)
            completed_at: datetime | None = started_at + timedelta(hours=duration_hours)
            if completed_at is not None and completed_at > end:
                completed_at = None  # still in progress as of the dataset's time range
            data.maintenance_events.append(
                {
                    "maintenance_event_id": f"{vehicle_id}-maint-{m:03d}",
                    "vehicle_id": vehicle_id,
                    "event_type": event_type,
                    "started_at": started_at,
                    "completed_at": completed_at,
                }
            )


def _generate_trips_deliveries_fuel(
    data: SyntheticFleetData,
    rng: random.Random,
    *,
    tenant_slug: str,
    vehicle_ids: list[str],
    route_ids: list[str],
    driver_ids: list[str],
    start: datetime,
    end: datetime,
) -> None:
    trip_counter = 0
    day = start
    while day < end:
        for _ in range(rng.randint(*_TRIPS_PER_DAY_RANGE)):
            trip_id = f"{tenant_slug}-trip-{trip_counter:05d}"
            trip_counter += 1
            vehicle_id = rng.choice(vehicle_ids)
            planned_departure_at = day + timedelta(hours=rng.uniform(5, 20))
            planned_distance_km = round(rng.uniform(15, 450), 1)
            planned_duration_hours = max(planned_distance_km / 60, 0.5)
            planned_arrival_at = planned_departure_at + timedelta(hours=planned_duration_hours)

            status, actual_departure_at, actual_arrival_at = _resolve_trip_outcome(
                rng,
                planned_departure_at=planned_departure_at,
                planned_arrival_at=planned_arrival_at,
                end=end,
            )

            data.trips.append(
                {
                    "trip_id": trip_id,
                    "vehicle_id": vehicle_id,
                    "driver_id": rng.choice(driver_ids),
                    "route_id": rng.choice(route_ids),
                    "planned_departure_at": planned_departure_at,
                    "planned_arrival_at": planned_arrival_at,
                    "actual_departure_at": actual_departure_at,
                    "actual_arrival_at": actual_arrival_at,
                    "planned_distance_km": planned_distance_km,
                    "status": status,
                }
            )

            if status in ("completed", "cancelled"):
                delivery_status = "delivered" if status == "completed" else "failed"
                for d in range(rng.randint(1, 3)):
                    data.deliveries.append(
                        {
                            "delivery_id": f"{trip_id}-del-{d}",
                            "trip_id": trip_id,
                            "planned_at": planned_arrival_at,
                            "delivered_at": actual_arrival_at,
                            "status": delivery_status,
                        }
                    )

            if status != "planned" and rng.random() < 0.7:
                data.fuel_events.append(
                    {
                        "fuel_event_id": f"{trip_id}-fuel",
                        "vehicle_id": vehicle_id,
                        "trip_id": trip_id,
                        "quantity_litres": round(rng.uniform(20, 180), 1),
                        "occurred_at": planned_departure_at
                        + timedelta(minutes=rng.uniform(-30, 10)),
                    }
                )
        day += timedelta(days=1)


def _resolve_trip_outcome(
    rng: random.Random,
    *,
    planned_departure_at: datetime,
    planned_arrival_at: datetime,
    end: datetime,
) -> tuple[str, datetime | None, datetime | None]:
    if planned_departure_at > end - timedelta(hours=18):
        # Too close to the dataset's time-range cutoff for an outcome to exist yet — this is
        # exactly the "avoid placing future events into earlier training rows" case:
        # hermes_rpt.datasets.label.delivery_delay_label returns None for these. 18 hours (not a
        # tighter cutoff) so this reliably covers most of the final day's trips regardless of
        # the day's random departure-hour spread, not just a lucky few.
        return "planned", None, None

    if rng.random() < _CANCELLED_PROBABILITY:
        return "cancelled", None, None

    actual_departure_at = planned_departure_at + timedelta(minutes=rng.uniform(-5, 25))
    if rng.random() < _ON_TIME_DELAY_PROBABILITY:
        delay_minutes = rng.uniform(
            -10, 25
        )  # on time or a little early/late, under the 30min threshold
    else:
        delay_minutes = rng.uniform(35, 240)  # a real delay — the positive class
    actual_arrival_at = planned_arrival_at + timedelta(minutes=delay_minutes)
    return "completed", actual_departure_at, actual_arrival_at


def _inject_defects(data: SyntheticFleetData, rng: random.Random, *, end: datetime) -> None:
    """A small, fixed set of deliberately bad rows — proof that
    `hermes_rpt.datasets.quality`'s checks catch real problems, not just a hypothetical.

    The "future timestamp" defect is relative to wall-clock `now`, not `end` — a customer's
    source database can contain a bad future-dated row regardless of what time range a dataset
    build asks for, which is exactly the scenario `check_future_timestamps` guards against. For
    this defect to actually be observed by a dataset build, the caller's `end` must extend past
    `now` (see `scripts/build_synthetic_dataset.py`); otherwise the row falls outside the
    build's own `[start, end)` query range and is silently never fetched at all — a config
    choice, not a gap in the check itself.
    """

    if data.vehicles:
        data.vehicles.append(dict(data.vehicles[0]))  # duplicate vehicle_id

    completed_trips = [t for t in data.trips if t["status"] == "completed"]
    if len(completed_trips) >= 4:
        bad = completed_trips[0]
        bad["actual_arrival_at"] = bad["planned_departure_at"] - timedelta(hours=1)  # invalid order

        bad = completed_trips[1]
        bad["planned_distance_km"] = -42.0  # negative distance

        bad = completed_trips[2]
        bad["status"] = "delayed_indefinitely"  # unrecognised status

        now = datetime.now(UTC)
        bad = completed_trips[3]
        bad["planned_departure_at"] = min(now + timedelta(hours=6), end - timedelta(minutes=1))

    if data.fuel_events:
        data.fuel_events[0]["quantity_litres"] = -15.0  # impossible fuel quantity
