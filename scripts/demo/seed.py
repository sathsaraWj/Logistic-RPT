"""Generates synthetic operational history and writes it directly into a demo tenant's real
PostgreSQL "customer database" (Phase 17 `make demo-seed`).

Uses a plain SQLAlchemy engine talking directly to the tenant database with its own
application-level credentials (`alpha_app`/`beta_app` — the same ones `docker-compose.yml`
provisions), the same way any real customer's own systems would write into their own database.
This is deliberately *not* routed through `hermes_rpt`'s own connector — that connector is
read-only by design (`hermes_rpt.connectors.postgres`, `query_guard`) and is never meant to
write to a customer's database at all; seeding here stands in for "the customer's pre-existing
operational data," which already exists before Hermes-RPT ever connects to it.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

_TRIP_STATUSES_COMPLETE = "completed"
_TRIP_STATUS_CANCELLED = "cancelled"
_TRIP_STATUS_PLANNED = "planned"

_MAINTENANCE_TYPES = ("scheduled", "unscheduled", "inspection")


@dataclass(frozen=True, slots=True)
class SeedSummary:
    tenant_slug: str
    vehicle_count: int
    driver_count: int
    trip_count: int
    delivery_count: int
    maintenance_event_count: int


async def seed_tenant_database(
    *, tenant_slug: str, dsn: str, seed: int, now: datetime | None = None
) -> SeedSummary:
    """`dsn` is a full `postgresql+asyncpg://...` URL for the tenant's own database (e.g.
    `tenant_alpha` via `alpha_app`), not the Hermes-RPT control plane."""

    # Synthetic demo data generation, not a security context.
    rng = random.Random(seed)  # noqa: S311 # nosec B311
    now = now or datetime.now(UTC)
    is_alpha = tenant_slug == "demo-alpha"

    vehicle_ids = [f"{'V' if is_alpha else 'A'}{i:03d}" for i in range(1, 5)]
    driver_ids = [f"{'D' if is_alpha else 'E'}{i:03d}" for i in range(1, 4)]

    engine = create_async_engine(dsn)
    try:
        async with engine.begin() as conn:
            await _seed_vehicles(conn, tenant_slug=tenant_slug, vehicle_ids=vehicle_ids, rng=rng)
            await _seed_drivers(conn, tenant_slug=tenant_slug, driver_ids=driver_ids, rng=rng)
            trip_ids = await _seed_trips(
                conn,
                tenant_slug=tenant_slug,
                vehicle_ids=vehicle_ids,
                driver_ids=driver_ids,
                rng=rng,
                now=now,
            )
            delivery_count = await _seed_deliveries(
                conn, tenant_slug=tenant_slug, trip_ids=trip_ids, rng=rng
            )
            maintenance_count = await _seed_maintenance_events(
                conn, tenant_slug=tenant_slug, vehicle_ids=vehicle_ids, rng=rng, now=now
            )
    finally:
        await engine.dispose()

    return SeedSummary(
        tenant_slug=tenant_slug,
        vehicle_count=len(vehicle_ids),
        driver_count=len(driver_ids),
        trip_count=len(trip_ids),
        delivery_count=delivery_count,
        maintenance_event_count=maintenance_count,
    )


async def _seed_vehicles(
    conn, *, tenant_slug: str, vehicle_ids: list[str], rng: random.Random
) -> None:
    is_alpha = tenant_slug == "demo-alpha"
    makes = (
        ["Ford", "Mercedes", "Volvo", "Scania"] if is_alpha else ["MAN", "Iveco", "DAF", "Renault"]
    )
    rows = [
        {
            "id": vehicle_id,
            "reg": f"{'ALPHA' if is_alpha else 'BETA'}-{i:03d}",
            "make": rng.choice(makes),
            "model": f"Model-{rng.randint(100, 999)}",
            "year": rng.randint(2018, 2024),
            "km": round(rng.uniform(5_000, 120_000), 1),
            "active": True,
            "acquired": datetime.now(UTC) - timedelta(days=rng.randint(200, 2000)),
        }
        for i, vehicle_id in enumerate(vehicle_ids, start=1)
    ]
    if is_alpha:
        stmt = text(
            "INSERT INTO fleet_vehicle "
            "(vehicle_id, registration_no, make, model_name, manufacture_year, odometer_km, "
            "is_active, acquired_at) "
            "VALUES (:id, :reg, :make, :model, :year, :km, :active, :acquired)"
        )
    else:
        stmt = text(
            "INSERT INTO assets "
            "(asset_id, tag, brand, model, year_built, total_km, active_flag, in_service_since) "
            "VALUES (:id, :reg, :make, :model, :year, :km, :active, :acquired)"
        )
    await conn.execute(stmt, rows)


async def _seed_drivers(
    conn, *, tenant_slug: str, driver_ids: list[str], rng: random.Random
) -> None:
    is_alpha = tenant_slug == "demo-alpha"
    first_names = ["Alex", "Sam", "Jordan", "Casey", "Morgan", "Riley"]
    last_names = ["Smith", "Ng", "Garcia", "Muller", "Kowalski", "Dubois"]
    rows = [
        {
            "id": driver_id,
            "name": f"{rng.choice(first_names)} {rng.choice(last_names)}",
            "license": f"LIC-{rng.randint(100000, 999999)}",
            "expiry": (datetime.now(UTC) + timedelta(days=rng.randint(100, 900))).date(),
            "hired": (datetime.now(UTC) - timedelta(days=rng.randint(100, 1500))).date(),
            "active": True,
        }
        for driver_id in driver_ids
    ]
    if is_alpha:
        stmt = text(
            "INSERT INTO fleet_driver "
            "(driver_id, full_name, license_number, license_expiry, hired_at, is_active) "
            "VALUES (:id, :name, :license, :expiry, :hired, :active)"
        )
    else:
        stmt = text(
            "INSERT INTO employees "
            "(employee_id, name, licence_no, licence_expiry, start_date, active) "
            "VALUES (:id, :name, :license, :expiry, :hired, :active)"
        )
    await conn.execute(stmt, rows)


async def _seed_trips(
    conn,
    *,
    tenant_slug: str,
    vehicle_ids: list[str],
    driver_ids: list[str],
    rng: random.Random,
    now: datetime,
) -> list[str]:
    is_alpha = tenant_slug == "demo-alpha"
    prefix = "T" if is_alpha else "J"
    trip_ids: list[str] = []
    rows = []
    for i in range(1, 61):
        trip_id = f"{prefix}{i:04d}"
        trip_ids.append(trip_id)
        days_ago = rng.randint(1, 60)
        departure = now - timedelta(days=days_ago, hours=rng.randint(0, 23))
        distance_km = round(rng.uniform(20, 450), 1)
        planned_duration_minutes = distance_km / 60 * 60 * rng.uniform(0.9, 1.3)
        planned_arrival = departure + timedelta(minutes=planned_duration_minutes)

        roll = rng.random()
        if days_ago <= 1 and roll < 0.3:
            status = _TRIP_STATUS_PLANNED
            actual_departure = None
            actual_arrival = None
        elif roll < 0.08:
            status = _TRIP_STATUS_CANCELLED
            actual_departure = None
            actual_arrival = None
        else:
            status = _TRIP_STATUSES_COMPLETE
            delay_minutes = rng.choice(
                [rng.uniform(-15, 15), rng.uniform(-15, 15), rng.uniform(35, 120)]
            )
            actual_departure = departure + timedelta(minutes=rng.uniform(-10, 10))
            actual_arrival = planned_arrival + timedelta(minutes=delay_minutes)

        rows.append(
            {
                "id": trip_id,
                "vehicle": rng.choice(vehicle_ids),
                "driver": rng.choice(driver_ids),
                "route": f"R{rng.randint(1, 8):02d}",
                "planned_dep": departure,
                "planned_arr": planned_arrival,
                "actual_dep": actual_departure,
                "actual_arr": actual_arrival,
                "distance": distance_km,
                "status": status,
            }
        )
    if is_alpha:
        stmt = text(
            "INSERT INTO transport_trip "
            "(trip_id, vehicle_id, driver_id, route_id, planned_departure_at, "
            "planned_arrival_at, actual_departure_at, actual_arrival_at, planned_distance_km, "
            "status) VALUES (:id, :vehicle, :driver, :route, :planned_dep, :planned_arr, "
            ":actual_dep, :actual_arr, :distance, :status)"
        )
    else:
        stmt = text(
            "INSERT INTO jobs "
            "(job_ref, asset_ref, driver_ref, path_ref, sched_depart, sched_arrive, "
            "real_depart, real_arrive, km_planned, job_status) "
            "VALUES (:id, :vehicle, :driver, :route, :planned_dep, :planned_arr, "
            ":actual_dep, :actual_arr, :distance, :status)"
        )
    await conn.execute(stmt, rows)
    return trip_ids


async def _seed_deliveries(
    conn, *, tenant_slug: str, trip_ids: list[str], rng: random.Random
) -> int:
    is_alpha = tenant_slug == "demo-alpha"
    prefix = "DL" if is_alpha else "CN"
    rows = []
    counter = 1
    for trip_id in trip_ids:
        if rng.random() < 0.85:
            delivery_id = f"{prefix}{counter:04d}"
            counter += 1
            due = datetime.now(UTC) - timedelta(days=rng.randint(1, 60))
            delivered = (
                due + timedelta(minutes=rng.uniform(-20, 90)) if rng.random() < 0.9 else None
            )
            rows.append(
                {
                    "id": delivery_id,
                    "trip": trip_id,
                    "order": f"ORD-{rng.randint(10000, 99999)}",
                    "due": due,
                    "delivered": delivered,
                    "status": "delivered" if delivered is not None else "pending",
                }
            )
    if not rows:
        return 0
    if is_alpha:
        stmt = text(
            "INSERT INTO delivery_record "
            "(delivery_id, trip_id, order_id, planned_at, delivered_at, status) "
            "VALUES (:id, :trip, :order, :due, :delivered, :status)"
        )
    else:
        stmt = text(
            "INSERT INTO consignments "
            "(consignment_id, job_ref, order_ref, due_at, done_at, consignment_status) "
            "VALUES (:id, :trip, :order, :due, :delivered, :status)"
        )
    await conn.execute(stmt, rows)
    return len(rows)


async def _seed_maintenance_events(
    conn, *, tenant_slug: str, vehicle_ids: list[str], rng: random.Random, now: datetime
) -> int:
    is_alpha = tenant_slug == "demo-alpha"
    prefix = "ME" if is_alpha else "SO"
    rows = []
    for i, vehicle_id in enumerate(vehicle_ids, start=1):
        for j in range(rng.randint(1, 3)):
            event_id = f"{prefix}{i:02d}{j:02d}"
            started = now - timedelta(days=rng.randint(5, 300))
            completed = (
                started + timedelta(hours=rng.uniform(2, 48)) if rng.random() < 0.8 else None
            )
            rows.append(
                {
                    "id": event_id,
                    "vehicle": vehicle_id,
                    "wo": f"WO-{rng.randint(1000, 9999)}",
                    "type": rng.choice(_MAINTENANCE_TYPES),
                    "started": started,
                    "completed": completed,
                }
            )
    if not rows:
        return 0
    if is_alpha:
        stmt = text(
            "INSERT INTO maintenance_log "
            "(maintenance_event_id, vehicle_id, work_order_id, event_type, started_at, "
            "completed_at) VALUES (:id, :vehicle, :wo, :type, :started, :completed)"
        )
    else:
        stmt = text(
            "INSERT INTO service_orders "
            "(service_order_id, asset_ref, wo_ref, service_type, opened_at, closed_at) "
            "VALUES (:id, :vehicle, :wo, :type, :started, :completed)"
        )
    await conn.execute(stmt, rows)
    return len(rows)
