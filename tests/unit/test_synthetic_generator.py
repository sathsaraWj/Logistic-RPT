"""Tests for the synthetic fleet data generator (Phase 9) — determinism, realistic missing
values/imbalance/history, and the deliberately injected defects `hermes_rpt.datasets.quality`'s
checks are meant to catch.
"""

from __future__ import annotations

from datetime import UTC, datetime

from hermes_rpt.synthetic.generator import generate_fleet_data

_START = datetime(2026, 1, 1, tzinfo=UTC)
_END = datetime(2026, 3, 1, tzinfo=UTC)


def test_generation_is_deterministic_for_the_same_seed() -> None:
    a = generate_fleet_data(tenant_slug="alpha", seed=1, start=_START, end=_END)
    b = generate_fleet_data(tenant_slug="alpha", seed=1, start=_START, end=_END)
    assert a.trips == b.trips
    assert a.vehicles == b.vehicles


def test_different_seeds_produce_different_data() -> None:
    a = generate_fleet_data(tenant_slug="alpha", seed=1, start=_START, end=_END)
    b = generate_fleet_data(tenant_slug="alpha", seed=2, start=_START, end=_END)
    assert a.trips != b.trips


def test_alpha_and_beta_tenant_slugs_produce_independent_id_namespaces() -> None:
    alpha = generate_fleet_data(tenant_slug="alpha", seed=1, start=_START, end=_END)
    beta = generate_fleet_data(tenant_slug="beta", seed=1, start=_START, end=_END)
    alpha_ids = {v["vehicle_id"] for v in alpha.vehicles}
    beta_ids = {v["vehicle_id"] for v in beta.vehicles}
    assert alpha_ids.isdisjoint(beta_ids)


def test_generates_all_seven_entities() -> None:
    data = generate_fleet_data(tenant_slug="alpha", seed=1, start=_START, end=_END)
    assert data.vehicles
    assert data.trips
    assert data.maintenance_events
    assert data.odometer_readings
    assert data.fuel_events
    assert data.route_stops
    assert data.deliveries


def test_trip_status_includes_completed_and_a_minority_of_cancelled() -> None:
    data = generate_fleet_data(tenant_slug="alpha", seed=1, start=_START, end=_END)
    statuses = [t["status"] for t in data.trips]
    completed = statuses.count("completed")
    cancelled = statuses.count("cancelled")
    assert completed > 0
    assert cancelled > 0
    assert cancelled < completed  # cancellation is the minority outcome


def test_some_trips_near_the_end_of_the_range_have_no_outcome_yet() -> None:
    """ "Avoid placing future events into earlier training rows" — trips planned close to `end`
    must not have a fabricated actual_arrival_at."""

    data = generate_fleet_data(tenant_slug="alpha", seed=1, start=_START, end=_END)
    unresolved = [t for t in data.trips if t["status"] == "planned"]
    assert unresolved
    assert all(t["actual_arrival_at"] is None for t in unresolved)


def test_odometer_readings_have_realistic_missing_source_values() -> None:
    data = generate_fleet_data(tenant_slug="alpha", seed=1, start=_START, end=_END)
    missing_source = sum(1 for r in data.odometer_readings if r["source"] is None)
    assert 0 < missing_source < len(data.odometer_readings)


def test_defect_injection_produces_a_duplicate_vehicle_id() -> None:
    data = generate_fleet_data(
        tenant_slug="alpha", seed=1, start=_START, end=_END, inject_defects=True
    )
    ids = [v["vehicle_id"] for v in data.vehicles]
    assert len(ids) != len(set(ids))


def test_defect_injection_produces_a_negative_fuel_quantity() -> None:
    data = generate_fleet_data(
        tenant_slug="alpha", seed=1, start=_START, end=_END, inject_defects=True
    )
    assert any(f["quantity_litres"] < 0 for f in data.fuel_events)


def test_no_defects_when_disabled() -> None:
    data = generate_fleet_data(
        tenant_slug="alpha", seed=1, start=_START, end=_END, inject_defects=False
    )
    ids = [v["vehicle_id"] for v in data.vehicles]
    assert len(ids) == len(set(ids))
    assert all(f["quantity_litres"] >= 0 for f in data.fuel_events)
