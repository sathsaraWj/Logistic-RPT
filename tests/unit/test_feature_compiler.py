"""Compiler-level tests (Phase 8) against a real SQLite database — `FakeConnector`-style
placeholder engines can't run real SQL, and the compiler's whole job is running real,
parameterized SQL, so these tests use `sqlite+aiosqlite` directly rather than mocking it away.
Covers point-in-time correctness ("a feature must only use information that would have existed
at the prediction timestamp") for every `FeatureKind`.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from hermes_rpt.connectors.query_guard import DisallowedObjectError
from hermes_rpt.features.compiler import (
    ColumnNotDirectlyMappedError,
    compile_and_run_related_feature,
    fetch_target_row,
)
from hermes_rpt.features.contract import DELIVERY_DELAY_RISK_CONTRACT
from hermes_rpt.mappings.document import FieldMapping, MappingDocument, SourceTable, ValueSource

_PREDICTION_TIME = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)


@pytest.fixture
async def sqlite_engine():  # type: ignore[no-untyped-def]
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE trip (trip_id TEXT PRIMARY KEY, vehicle_id TEXT, "
                "planned_departure_at TIMESTAMP, planned_distance_km REAL)"
            )
        )
        await conn.execute(
            text(
                "CREATE TABLE maintenance_event (vehicle_id TEXT, event_type TEXT, "
                "started_at TIMESTAMP, completed_at TIMESTAMP)"
            )
        )
        await conn.execute(
            text("INSERT INTO trip VALUES ('TRIP-1', 'V-1', '2026-07-27 08:00:00', 120.5)")
        )
    yield engine
    await engine.dispose()


def _trip_document() -> MappingDocument:
    return MappingDocument(
        entity="Trip",
        source=SourceTable(schema="main", table="trip"),
        identity={"trip_id": FieldMapping(sources=(ValueSource(column="trip_id"),))},
        fields={
            "vehicle_id": FieldMapping(sources=(ValueSource(column="vehicle_id"),)),
            "planned_departure_at": FieldMapping(
                sources=(ValueSource(column="planned_departure_at"),)
            ),
            "planned_distance_km": FieldMapping(
                sources=(ValueSource(column="planned_distance_km"),)
            ),
        },
    )


def _maintenance_document() -> MappingDocument:
    return MappingDocument(
        entity="MaintenanceEvent",
        source=SourceTable(schema="main", table="maintenance_event"),
        identity={"vehicle_id": FieldMapping(sources=(ValueSource(column="vehicle_id"),))},
        fields={
            "event_type": FieldMapping(sources=(ValueSource(column="event_type"),)),
            "started_at": FieldMapping(sources=(ValueSource(column="started_at"),)),
            "completed_at": FieldMapping(sources=(ValueSource(column="completed_at"),)),
        },
    )


async def test_fetch_target_row_returns_labeled_ontology_fields(sqlite_engine) -> None:  # type: ignore[no-untyped-def]
    row = await fetch_target_row(
        sqlite_engine,
        _trip_document(),
        business_reference="TRIP-1",
        needed_fields={"planned_departure_at", "planned_distance_km", "vehicle_id"},
        schema_allowlist=["main"],
        table_allowlist=["trip"],
    )
    assert row is not None
    assert row["vehicle_id"] == "V-1"
    assert row["planned_distance_km"] == 120.5


async def test_fetch_target_row_returns_none_for_unknown_identity(sqlite_engine) -> None:  # type: ignore[no-untyped-def]
    row = await fetch_target_row(
        sqlite_engine,
        _trip_document(),
        business_reference="DOES-NOT-EXIST",
        needed_fields={"vehicle_id"},
        schema_allowlist=["main"],
        table_allowlist=["trip"],
    )
    assert row is None


async def test_fetch_target_row_rejects_disallowed_table(sqlite_engine) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(DisallowedObjectError):
        await fetch_target_row(
            sqlite_engine,
            _trip_document(),
            business_reference="TRIP-1",
            needed_fields=set(),
            schema_allowlist=["main"],
            table_allowlist=["some_other_table"],  # "trip" deliberately not allowlisted
        )


async def test_related_count_only_counts_rows_before_prediction_time(sqlite_engine) -> None:  # type: ignore[no-untyped-def]
    async with sqlite_engine.begin() as conn:
        # Two events before prediction_time, one after — the "after" one must not be counted.
        await conn.execute(
            text(
                "INSERT INTO maintenance_event VALUES "
                "('V-1', 'unscheduled', '2026-06-01 00:00:00', '2026-06-01 00:00:00'), "
                "('V-1', 'unscheduled', '2026-07-01 00:00:00', '2026-07-01 00:00:00'), "
                "('V-1', 'unscheduled', '2026-08-01 00:00:00', '2026-08-01 00:00:00')"
            )
        )

    feature = DELIVERY_DELAY_RISK_CONTRACT.get_feature("previous_breakdown_count")
    count = await compile_and_run_related_feature(
        sqlite_engine,
        feature,
        _maintenance_document(),
        join_value="V-1",
        prediction_time=_PREDICTION_TIME,
        schema_allowlist=["main"],
        table_allowlist=["maintenance_event"],
    )
    assert count == 2


async def test_related_recency_days_uses_the_latest_event_before_prediction_time(
    sqlite_engine,  # type: ignore[no-untyped-def]
) -> None:
    async with sqlite_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO maintenance_event VALUES "
                "('V-1', 'scheduled', '2026-07-01 00:00:00', '2026-07-17 12:00:00'), "
                "('V-1', 'scheduled', '2026-08-01 00:00:00', '2026-08-01 12:00:00')"
            )
        )

    feature = DELIVERY_DELAY_RISK_CONTRACT.get_feature("days_since_last_maintenance")
    days = await compile_and_run_related_feature(
        sqlite_engine,
        feature,
        _maintenance_document(),
        join_value="V-1",
        prediction_time=_PREDICTION_TIME,
        schema_allowlist=["main"],
        table_allowlist=["maintenance_event"],
    )
    # Latest completed_at strictly before prediction_time (2026-07-27 12:00) is 2026-07-17
    # 12:00 -> exactly 10 days. The 2026-08-01 event is after prediction_time and must be
    # ignored, even though it would otherwise be "more recent."
    assert days == pytest.approx(10.0)


async def test_related_recency_returns_none_when_no_qualifying_rows(sqlite_engine) -> None:  # type: ignore[no-untyped-def]
    feature = DELIVERY_DELAY_RISK_CONTRACT.get_feature("days_since_last_maintenance")
    days = await compile_and_run_related_feature(
        sqlite_engine,
        feature,
        _maintenance_document(),
        join_value="V-NO-HISTORY",
        prediction_time=_PREDICTION_TIME,
        schema_allowlist=["main"],
        table_allowlist=["maintenance_event"],
    )
    assert days is None


async def test_related_ratio_computes_fraction_matching_filter(sqlite_engine) -> None:  # type: ignore[no-untyped-def]
    trip_document = _trip_document()
    async with sqlite_engine.begin() as conn:
        await conn.execute(text("ALTER TABLE trip ADD COLUMN driver_id TEXT"))
        await conn.execute(text("ALTER TABLE trip ADD COLUMN status TEXT"))
        await conn.execute(
            text(
                "INSERT INTO trip (trip_id, driver_id, status, planned_departure_at) VALUES "
                "('T2', 'D-1', 'completed', '2026-07-01 00:00:00'), "
                "('T3', 'D-1', 'cancelled', '2026-07-05 00:00:00'), "
                "('T4', 'D-1', 'completed', '2026-07-10 00:00:00')"
            )
        )
    trip_document = MappingDocument(
        entity="Trip",
        source=SourceTable(schema="main", table="trip"),
        identity={"trip_id": FieldMapping(sources=(ValueSource(column="trip_id"),))},
        fields={
            "driver_id": FieldMapping(sources=(ValueSource(column="driver_id"),)),
            "status": FieldMapping(sources=(ValueSource(column="status"),)),
            "planned_departure_at": FieldMapping(
                sources=(ValueSource(column="planned_departure_at"),)
            ),
        },
    )

    feature = DELIVERY_DELAY_RISK_CONTRACT.get_feature("driver_late_delivery_ratio")
    ratio = await compile_and_run_related_feature(
        sqlite_engine,
        feature,
        trip_document,
        join_value="D-1",
        prediction_time=_PREDICTION_TIME,
        schema_allowlist=["main"],
        table_allowlist=["trip"],
    )
    assert ratio == pytest.approx(1 / 3)


async def test_related_latest_value_orders_by_timestamp_and_respects_cutoff(
    sqlite_engine,  # type: ignore[no-untyped-def]
) -> None:
    async with sqlite_engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE odometer_reading (vehicle_id TEXT, "
                "recorded_at TIMESTAMP, reading_km REAL)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO odometer_reading VALUES "
                "('V-1', '2026-07-01 00:00:00', 1000.0), "
                "('V-1', '2026-07-20 00:00:00', 1500.0), "
                "('V-1', '2026-08-01 00:00:00', 2000.0)"  # after prediction_time
            )
        )
    document = MappingDocument(
        entity="OdometerReading",
        source=SourceTable(schema="main", table="odometer_reading"),
        identity={"vehicle_id": FieldMapping(sources=(ValueSource(column="vehicle_id"),))},
        fields={
            "recorded_at": FieldMapping(sources=(ValueSource(column="recorded_at"),)),
            "reading_km": FieldMapping(sources=(ValueSource(column="reading_km"),)),
        },
    )
    feature = DELIVERY_DELAY_RISK_CONTRACT.get_feature("odometer_km")
    value = await compile_and_run_related_feature(
        sqlite_engine,
        feature,
        document,
        join_value="V-1",
        prediction_time=_PREDICTION_TIME,
        schema_allowlist=["main"],
        table_allowlist=["odometer_reading"],
    )
    assert value == 1500.0  # not 2000.0 — that reading is after prediction_time


def test_resolve_column_rejects_static_and_derived_sources() -> None:
    from hermes_rpt.features.compiler import resolve_column

    document = MappingDocument(
        entity="Trip",
        source=SourceTable(schema="main", table="trip"),
        identity={"trip_id": FieldMapping(sources=(ValueSource(column="trip_id"),))},
        fields={"is_active": FieldMapping(sources=(ValueSource(static=True),))},
    )
    with pytest.raises(ColumnNotDirectlyMappedError):
        resolve_column(document, "is_active")
