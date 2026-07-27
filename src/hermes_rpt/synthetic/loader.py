"""Loads generated synthetic rows into a real (SQLite) database engine (Phase 9) — creates the
tenant-shaped tables and inserts rows via parameterized `INSERT` statements, never string
formatting. Used only for local/CI synthetic fixtures; the resulting `.sqlite3` file is written
outside the repository (see `scripts/build_synthetic_dataset.py`) and is itself synthetic, so
there is nothing customer-identifying in it to protect beyond the platform's usual SQL-safety
posture.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from hermes_rpt.synthetic.generator import SyntheticFleetData
from hermes_rpt.synthetic.schemas import ALL_ENTITY_SCHEMAS, EntitySchema

_ENTITY_ROWS: dict[str, str] = {
    "Vehicle": "vehicles",
    "Trip": "trips",
    "MaintenanceEvent": "maintenance_events",
    "OdometerReading": "odometer_readings",
    "FuelEvent": "fuel_events",
    "RouteStop": "route_stops",
    "Delivery": "deliveries",
}


async def create_tenant_schema(engine: AsyncEngine, *, tenant_slug: str) -> None:
    async with engine.begin() as conn:
        for schema in ALL_ENTITY_SCHEMAS:
            await conn.execute(sa.text(schema.ddl(tenant_slug)))


async def load_fleet_data(
    engine: AsyncEngine, data: SyntheticFleetData, *, tenant_slug: str
) -> None:
    async with engine.begin() as conn:
        for schema in ALL_ENTITY_SCHEMAS:
            rows = getattr(data, _ENTITY_ROWS[schema.entity])
            if rows:
                await _insert_rows(conn, schema, rows, tenant_slug=tenant_slug)


async def _insert_rows(
    conn: AsyncConnection,
    schema: EntitySchema,
    rows: list[dict[str, object]],
    *,
    tenant_slug: str,
) -> None:
    columns = schema.columns(tenant_slug)
    table = sa.table(schema.table(tenant_slug), *(sa.column(c) for c in columns.values()))
    stmt = sa.insert(table)
    params = [{columns[field]: row.get(field) for field in columns if field in row} for row in rows]
    await conn.execute(stmt, params)
