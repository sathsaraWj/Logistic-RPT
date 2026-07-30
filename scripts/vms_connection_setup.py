"""One-off setup script: registers Hermes VMS's database as a Hermes-RPT connection, runs
schema discovery, and creates + activates the Trip mapping.

Run from the repo root with:
    ADMIN_TOKEN=<token> VMS_DB_PASSWORD=<password> uv run python scripts/vms_connection_setup.py

Prerequisites:
  - ADMIN_TOKEN env var set to a valid admin-scoped bearer token for the VMS tenant.
  - VMS_DB_PASSWORD env var set to the hermes_rpt_reader Postgres password.
  - The connection's own credential storage requires Hermes-RPT's Google Secret Manager
    provider to actually be deployed (SECRET_PROVIDER_BACKEND=google_secret_manager) — without
    it, this connection's password only lives in one Cloud Run container's memory and will
    silently stop working whenever that instance recycles.

This only talks to the real deployed API over HTTPS — it never touches the database directly.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any

import httpx

API_BASE = "https://hermes-rpt-api-341508818275.us-central1.run.app"

VMS_DB_HOST = "136.115.127.149"
VMS_DB_PORT = 5432
VMS_DB_NAME = "hermes"
VMS_DB_USER = "hermes_rpt_reader"

_STATUS_MAP = {
    "Planned": "planned",
    "Ongoing": "in_progress",
    "Completed": "completed",
    "Cancelled": "cancelled",
}


def _headers() -> dict[str, str]:
    token = os.environ.get("ADMIN_TOKEN")
    if not token:
        print("ADMIN_TOKEN env var is required", file=sys.stderr)
        sys.exit(1)
    return {"Authorization": f"Bearer {token}"}


def _vms_db_password() -> str:
    password = os.environ.get("VMS_DB_PASSWORD")
    if not password:
        print("VMS_DB_PASSWORD env var is required", file=sys.stderr)
        sys.exit(1)
    return password


def register_connection(headers: dict[str, str]) -> str:
    existing = httpx.get(f"{API_BASE}/v1/connections", headers=headers, timeout=30).json()
    for conn in existing:
        if conn["name"] == "vms-primary":
            print(f"connection already registered: {conn['id']}")
            return conn["id"]

    resp = httpx.post(
        f"{API_BASE}/v1/connections",
        headers=headers,
        json={
            "name": "vms-primary",
            "engine": "postgresql",
            "host": VMS_DB_HOST,
            "port": VMS_DB_PORT,
            "database_name": VMS_DB_NAME,
            "username": VMS_DB_USER,
            "secret_value": _vms_db_password(),
            "tls_mode": "require",
            "schema_allowlist": ["public"],
            "table_allowlist": ["trips", "vehicles", "drivers"],
        },
        timeout=30,
    )
    resp.raise_for_status()
    connection_id = resp.json()["id"]
    print(f"registered connection: {connection_id}")
    return connection_id


def validate_and_enable(headers: dict[str, str], connection_id: str) -> None:
    validated = httpx.post(
        f"{API_BASE}/v1/connections/{connection_id}/validate", headers=headers, timeout=60
    )
    validated.raise_for_status()
    print("validated:", validated.json())
    httpx.post(
        f"{API_BASE}/v1/connections/{connection_id}/enable", headers=headers, timeout=30
    ).raise_for_status()
    print("enabled")


def run_discovery(headers: dict[str, str], connection_id: str) -> str:
    resp = httpx.post(
        f"{API_BASE}/v1/connections/{connection_id}/discovery",
        headers=headers,
        json={"profiling": {"enabled": False}},
        timeout=30,
    )
    resp.raise_for_status()
    snapshot_id = resp.json()["id"]

    for _ in range(30):
        detail = httpx.get(
            f"{API_BASE}/v1/connections/{connection_id}/discovery/{snapshot_id}",
            headers=headers,
            timeout=30,
        ).json()
        if detail["status"] in ("completed", "failed"):
            if detail["status"] == "failed":
                raise RuntimeError(f"discovery failed: {detail}")
            tables = sorted(t["name"] for t in detail["metadata_document"]["tables"])
            print("discovered tables:", tables)
            return snapshot_id
        time.sleep(1)
    raise TimeoutError("discovery did not complete in time")


def _field(column: str) -> dict[str, Any]:
    return {"sources": [{"column": column}]}


def create_and_activate_mapping(headers: dict[str, str], snapshot_id: str) -> str:
    existing = httpx.get(f"{API_BASE}/v1/mappings", headers=headers, timeout=30).json()
    for mapping in existing:
        if mapping["entity_name"] == "Trip" and mapping["state"] == "active":
            print(f"mapping already active: {mapping['id']}")
            return mapping["id"]

    document = {
        "entity": "Trip",
        "source": {"schema": "public", "table": "trips"},
        "identity": {"trip_id": _field("id")},
        "fields": {
            "vehicle_id": _field("vehicle_id"),
            "driver_id": _field("driver_id"),
            "planned_departure_at": _field("planned_departure_at"),
            "planned_arrival_at": _field("planned_arrival_at"),
            "actual_departure_at": _field("start_time"),
            "actual_arrival_at": _field("end_time"),
            "planned_distance_km": _field("planned_distance_km"),
            "status": {"sources": [{"column": "trip_status", "value_map": _STATUS_MAP}]},
        },
    }
    created = httpx.post(
        f"{API_BASE}/v1/mappings",
        headers=headers,
        json={"schema_snapshot_id": snapshot_id, "document": document},
        timeout=30,
    )
    created.raise_for_status()
    mapping_id = created.json()["id"]
    print(f"mapping created: {mapping_id}")

    httpx.post(
        f"{API_BASE}/v1/mappings/{mapping_id}/submit-for-validation",
        headers=headers,
        timeout=30,
    ).raise_for_status()
    httpx.post(
        f"{API_BASE}/v1/mappings/{mapping_id}/approve", headers=headers, timeout=30
    ).raise_for_status()
    activated = httpx.post(
        f"{API_BASE}/v1/mappings/{mapping_id}/activate", headers=headers, timeout=30
    )
    activated.raise_for_status()
    print("mapping activated:", activated.json()["state"])
    return mapping_id


def main() -> None:
    headers = _headers()
    connection_id = register_connection(headers)
    validate_and_enable(headers, connection_id)
    snapshot_id = run_discovery(headers, connection_id)
    create_and_activate_mapping(headers, snapshot_id)
    print("\nDone. Next: training (see scripts/vms_train.py once written).")


if __name__ == "__main__":
    main()
