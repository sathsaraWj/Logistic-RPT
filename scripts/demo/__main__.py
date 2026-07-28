"""Phase 17 end-to-end local demonstration CLI.

`uv run --group ml python -m scripts.demo <subcommand>` — see `docs/DEMO.md` for the full
`make demo-*` walkthrough and what each subcommand proves. Every subcommand is a separate
process invocation; state (tenants, connections, mappings, model versions) persists in the real
control-plane PostgreSQL between them — nothing here uses an in-memory or SQLite control plane,
unlike `scripts/build_synthetic_dataset.py` (Phase 9's dataset-building demo, which deliberately
stays fast/dependency-free instead of exercising the real HTTP/RLS stack).

Environment (same variable names `docker-compose.yml` reads, same defaults):
`CONTROL_PLANE_DB_PORT` (5432), `TENANT_ALPHA_DB_PORT` (5433), `TENANT_BETA_DB_PORT` (5434),
`MLFLOW_PORT` (5000).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

_CONTROL_PLANE_DB_PORT = os.environ.get("CONTROL_PLANE_DB_PORT", "5432")
_TENANT_ALPHA_DB_PORT = int(os.environ.get("TENANT_ALPHA_DB_PORT", "5433"))
_TENANT_BETA_DB_PORT = int(os.environ.get("TENANT_BETA_DB_PORT", "5434"))
_MLFLOW_PORT = os.environ.get("MLFLOW_PORT", "5000")

# Must happen before any `hermes_rpt.common.settings.get_settings()` call anywhere in the
# process (it's `@lru_cache`d) — every demo subcommand needs the app pointed at the demo's own
# control-plane/MLflow ports, which may differ from the settings default if those ports were
# already taken on this machine (see docs/DEMO.md "port conflicts").
os.environ.setdefault(
    "DATABASE_URL",
    f"postgresql+asyncpg://hermes:hermes@localhost:{_CONTROL_PLANE_DB_PORT}/hermes_control",
)
os.environ.setdefault("MLFLOW_TRACKING_URI", f"http://localhost:{_MLFLOW_PORT}")
os.environ.setdefault("ENVIRONMENT", "local")

from fastapi import Depends  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from tests.factories import make_tenant, make_user  # noqa: E402

from hermes_rpt.auth.dev_tokens import issue_dev_token  # noqa: E402
from hermes_rpt.auth.enums import PrincipalType, ScopeName  # noqa: E402
from hermes_rpt.common.db import get_session  # noqa: E402
from hermes_rpt.common.logging import get_logger  # noqa: E402
from hermes_rpt.common.settings import get_settings  # noqa: E402
from hermes_rpt.common.tenant_session import bind_tenant_for_row_level_security  # noqa: E402
from hermes_rpt.connectors.pool_registry import get_pool_registry  # noqa: E402
from hermes_rpt.connectors.postgres import PostgresConnector  # noqa: E402
from hermes_rpt.connectors.service import ConnectionLifecycleManager  # noqa: E402
from hermes_rpt.tenants.context import TenantContext  # noqa: E402
from hermes_rpt.tenants.models import Tenant, User  # noqa: E402
from hermes_rpt.tenants.repository import TenantRepository, UserRepository  # noqa: E402
from scripts.demo.fixtures import ALL_DEMO_ENTITIES  # noqa: E402
from scripts.demo.secrets import DualWriteSecretProvider, seed_real_provider_from_file  # noqa: E402
from scripts.demo.seed import seed_tenant_database  # noqa: E402

logger = get_logger(__name__)

_TENANTS: dict[str, dict[str, str | int]] = {
    "demo-alpha": {
        "name": "Demo Tenant Alpha (Fleet Logistics Co.)",
        "email": "admin@demo-alpha.example.com",
        "db_port": _TENANT_ALPHA_DB_PORT,
        "db_name": "tenant_alpha",
        "db_user": "alpha_app",
        "db_password": "alpha-dev-password",  # noqa: S105 # nosec B105 - docker-compose dev fixture
        "target_table": "fleet_vehicle",
    },
    "demo-beta": {
        "name": "Demo Tenant Beta (Asset Transport Ltd.)",
        "email": "admin@demo-beta.example.com",
        "db_port": _TENANT_BETA_DB_PORT,
        "db_name": "tenant_beta",
        "db_user": "beta_app",
        "db_password": "beta-dev-password",  # noqa: S105 # nosec B105 - docker-compose dev fixture
        "target_table": "assets",
    },
}
_ALL_SCOPES = tuple(s.value for s in ScopeName)
_RUN_DIR = REPO_ROOT / "data" / "demo_runs" / f"{datetime.now(UTC):%Y%m%dT%H%M%S}"


def _tenant_dsn(slug: str) -> str:
    cfg = _TENANTS[slug]
    return (
        f"postgresql+asyncpg://{cfg['db_user']}:{cfg['db_password']}"
        f"@localhost:{cfg['db_port']}/{cfg['db_name']}"
    )


_demo_control_plane_engine = None


def _session_factory() -> async_sessionmaker[AsyncSession]:
    """A control-plane session factory bound to this demo script's own engine — deliberately
    *not* `hermes_rpt.common.db.get_sessionmaker()`'s process-wide `@lru_cache`d engine, which
    the `TestClient(create_app())` instances these subcommands also build use internally on
    their *own* event loop (Starlette's `TestClient` runs the ASGI app on a separate loop in a
    background thread). asyncpg connections are bound to the loop that created them; sharing one
    cached engine's pool between this script's outer loop and TestClient's inner loop raises
    `RuntimeError: ... attached to a different loop`. A second, independent engine to the same
    database has no such problem — multiple engines against one Postgres instance are normal."""

    global _demo_control_plane_engine
    if _demo_control_plane_engine is None:
        _demo_control_plane_engine = create_async_engine(
            f"postgresql+asyncpg://hermes:hermes@localhost:{_CONTROL_PLANE_DB_PORT}/hermes_control",
            pool_pre_ping=True,
        )
    return async_sessionmaker(bind=_demo_control_plane_engine, expire_on_commit=False)


@asynccontextmanager
async def _rls_bound_session(session_factory, tenant_id: uuid.UUID):
    """Every demo step that talks to the control-plane DB directly (not through the HTTP API,
    which already binds RLS via `get_tenant_context`) must bind it itself — the same fix Phase
    16 applied to `hermes_rpt.schemas.jobs.run_discovery_job`. This demo runs against a real
    PostgreSQL control plane (not the ephemeral SQLite Phase 9's `build_synthetic_dataset.py`
    uses), so RLS's `FORCE ROW LEVEL SECURITY` is genuinely enforced here."""

    async with session_factory() as session:
        await bind_tenant_for_row_level_security(session, tenant_id)
        yield session


async def _get_or_create_tenant(session: AsyncSession, slug: str) -> tuple[Tenant, User]:
    cfg = _TENANTS[slug]
    tenants = TenantRepository(session)
    users = UserRepository(session)
    tenant = await tenants.get_by_slug(slug)
    if tenant is None:
        tenant = await tenants.add(make_tenant(name=cfg["name"], slug=slug))
    user = await users.get_by_email(cfg["email"])
    if user is None:
        user = await users.add(make_user(email=cfg["email"], display_name=f"{cfg['name']} Admin"))
    await session.commit()
    return tenant, user


def _token_for(tenant: Tenant, user: User) -> str:
    settings = get_settings()
    return issue_dev_token(
        settings=settings,
        tenant_id=tenant.id,
        principal_id=user.id,
        scopes=_ALL_SCOPES,
        principal_type=PrincipalType.HUMAN,
    )


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _demo_connection_lifecycle_manager(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ConnectionLifecycleManager:
    """Overrides the app's own `get_connection_lifecycle_manager` dependency to use the
    cross-process `DualWriteSecretProvider` instead of the (real, correct-for-a-live-server,
    but process-local) `LocalDevSecretProvider` singleton — see `scripts/demo/secrets.py` for
    why a single demo run spanning several `make demo-*` process invocations needs this.

    Deliberately a *module-level* function, not nested inside `_build_client()`: with
    `from __future__ import annotations` active, FastAPI resolves this function's parameter
    annotations (to find the `Depends(...)` marker) against its own `__globals__` — the
    defining *module's* globals, never an enclosing function's locals — so every name this
    signature references (`AsyncSession`, `Depends`, `get_session`) must already be a real
    module-level import, not one done lazily inside another function's body.
    """

    return ConnectionLifecycleManager(
        session,
        secret_provider=DualWriteSecretProvider(),
        pool_registry=get_pool_registry(),
        connector=PostgresConnector(),
    )


def _build_client() -> TestClient:
    from apps.api.deps import get_connection_lifecycle_manager
    from apps.api.main import create_app

    # Schema discovery's background job resolves secrets via the real, process-cached provider
    # directly (see scripts/demo/secrets.py) — pre-seed it from the file before any request in
    # this process might schedule that job, so a connection registered in an *earlier* demo
    # process (e.g. demo-security-test re-discovering what demo-discover created) still resolves.
    seed_real_provider_from_file()

    app = create_app()
    app.dependency_overrides[get_connection_lifecycle_manager] = _demo_connection_lifecycle_manager
    return TestClient(app)


def _print_header(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def _write_report(name: str, payload: dict[str, Any]) -> Path:
    _RUN_DIR.mkdir(parents=True, exist_ok=True)
    path = _RUN_DIR / f"{name}.json"
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"  report written: {path.relative_to(REPO_ROOT)}")
    return path


# --- demo-seed -------------------------------------------------------------------------------


async def cmd_seed() -> None:
    _print_header("SEED — provisioning demo tenants and synthetic operational history")
    session_factory = _session_factory()
    tenant_ids: dict[str, uuid.UUID] = {}
    async with session_factory() as session:
        for slug in _TENANTS:
            tenant, user = await _get_or_create_tenant(session, slug)
            tenant_ids[slug] = tenant.id
            print(f"  tenant {slug}: id={tenant.id} user={user.email}")

    for slug in _TENANTS:
        summary = await seed_tenant_database(
            tenant_slug=slug, dsn=_tenant_dsn(slug), seed=hash(slug) & 0xFFFF
        )
        print(
            f"  seeded {slug}: {summary.vehicle_count} vehicles, {summary.driver_count} drivers, "
            f"{summary.trip_count} trips, {summary.delivery_count} deliveries, "
            f"{summary.maintenance_event_count} maintenance events"
        )
    print("\nSeed complete. Next: make demo-discover")


# --- demo-discover -----------------------------------------------------------------------------


async def _register_connection(
    client: TestClient, *, slug: str, headers: dict[str, str], tables: list[str]
) -> uuid.UUID:
    cfg = _TENANTS[slug]
    existing = client.get("/v1/connections", headers=headers).json()
    for conn in existing:
        if conn["name"] == "primary":
            return uuid.UUID(conn["id"])

    response = client.post(
        "/v1/connections",
        headers=headers,
        json={
            "name": "primary",
            "engine": "postgresql",
            "host": "localhost",
            "port": cfg["db_port"],
            "database_name": cfg["db_name"],
            "username": cfg["db_user"],
            "secret_value": cfg["db_password"],
            "tls_mode": "disable",
            "schema_allowlist": ["public"],
            "table_allowlist": tables,
        },
    )
    response.raise_for_status()
    return uuid.UUID(response.json()["id"])


async def _run_discovery(
    client: TestClient, *, connection_id: uuid.UUID, headers: dict[str, str]
) -> dict[str, Any]:
    response = client.post(
        f"/v1/connections/{connection_id}/discovery",
        headers=headers,
        json={"profiling": {"enabled": False}},
    )
    response.raise_for_status()
    snapshot_id = response.json()["id"]

    for _ in range(30):
        detail = client.get(
            f"/v1/connections/{connection_id}/discovery/{snapshot_id}", headers=headers
        ).json()
        if detail["status"] in ("completed", "failed"):
            return detail
        time.sleep(0.3)
    raise TimeoutError(f"discovery snapshot {snapshot_id} did not complete in time")


async def cmd_discover() -> None:
    _print_header(
        "DISCOVER — real schema introspection against each tenant's own PostgreSQL database"
    )
    session_factory = _session_factory()
    client = _build_client()
    report: dict[str, Any] = {}
    with client:
        async with session_factory() as session:
            for slug in _TENANTS:
                tenant, user = await _get_or_create_tenant(session, slug)
                headers = _auth_headers(_token_for(tenant, user))
                tables = [e.table(slug) for e in ALL_DEMO_ENTITIES]
                connection_id = await _register_connection(
                    client, slug=slug, headers=headers, tables=tables
                )
                client.post(
                    f"/v1/connections/{connection_id}/validate", headers=headers
                ).raise_for_status()
                client.post(
                    f"/v1/connections/{connection_id}/enable", headers=headers
                ).raise_for_status()
                snapshot = await _run_discovery(
                    client, connection_id=connection_id, headers=headers
                )
                discovered_tables = sorted(
                    t["name"] for t in snapshot["metadata_document"]["tables"]
                )
                print(f"  {slug}: discovered tables = {discovered_tables}")
                print(f"    schema_fingerprint = {snapshot['schema_fingerprint']}")
                report[slug] = {
                    "connection_id": str(connection_id),
                    "snapshot_id": snapshot["id"],
                    "discovered_tables": discovered_tables,
                    "schema_fingerprint": snapshot["schema_fingerprint"],
                }
    _write_report("discover", report)
    print("\nDiscovery complete. Next: make demo-map")


# --- demo-map ----------------------------------------------------------------------------------


async def cmd_map() -> None:
    _print_header(
        "MAP — creating and activating tenant-specific schema mappings onto the shared ontology"
    )
    session_factory = _session_factory()
    client = _build_client()
    report: dict[str, Any] = {}
    with client:
        async with session_factory() as session:
            for slug in _TENANTS:
                tenant, user = await _get_or_create_tenant(session, slug)
                headers = _auth_headers(_token_for(tenant, user))
                connections = client.get("/v1/connections", headers=headers).json()
                connection_id = next(c["id"] for c in connections if c["name"] == "primary")
                snapshots = client.get(
                    f"/v1/connections/{connection_id}/discovery", headers=headers
                ).json()
                snapshot_id = snapshots[-1]["id"]

                existing_mappings = {
                    m["entity_name"]: m for m in client.get("/v1/mappings", headers=headers).json()
                }
                entity_report = []
                for entity in ALL_DEMO_ENTITIES:
                    if (
                        entity.entity in existing_mappings
                        and existing_mappings[entity.entity]["state"] == "active"
                    ):
                        entity_report.append(
                            {
                                "entity": entity.entity,
                                "table": entity.table(slug),
                                "state": "active",
                            }
                        )
                        continue
                    document = entity.mapping_document(slug).model_dump(mode="json", by_alias=True)
                    created = client.post(
                        "/v1/mappings",
                        headers=headers,
                        json={"schema_snapshot_id": snapshot_id, "document": document},
                    )
                    created.raise_for_status()
                    mapping_id = created.json()["id"]
                    client.post(
                        f"/v1/mappings/{mapping_id}/submit-for-validation", headers=headers
                    ).raise_for_status()
                    client.post(
                        f"/v1/mappings/{mapping_id}/approve", headers=headers
                    ).raise_for_status()
                    activated = client.post(f"/v1/mappings/{mapping_id}/activate", headers=headers)
                    activated.raise_for_status()
                    entity_report.append(
                        {
                            "entity": entity.entity,
                            "table": entity.table(slug),
                            "state": activated.json()["state"],
                        }
                    )
                    print(f"  {slug}: {entity.entity} -> {entity.table(slug)} (active)")
                report[slug] = entity_report
    _write_report("map", report)
    print("\nMapping complete. Next: make demo-train")


# --- demo-train --------------------------------------------------------------------------------


async def _build_dataset_and_train(slug: str, run_dir: Path) -> dict[str, Any]:
    from hermes_rpt.connectors.pool_registry import TenantConnectionPoolRegistry
    from hermes_rpt.connectors.postgres import PostgresConnector
    from hermes_rpt.connectors.service import ConnectionLifecycleManager
    from hermes_rpt.datasets.builder import DatasetBuildService
    from hermes_rpt.datasets.definition import DatasetDefinition, LabelDefinition
    from hermes_rpt.features.contract import DELIVERY_DELAY_RISK_CONTRACT
    from hermes_rpt.models.config import BaselineModelType, TrainingConfig
    from hermes_rpt.models.training import BaselineTrainingService
    from hermes_rpt.models.transformer.context import RelationalContextBuilder
    from hermes_rpt.models.transformer.model import TINY
    from hermes_rpt.models.transformer.training import HermesRPTTrainingService
    from hermes_rpt.registry.enums import ModelStage
    from hermes_rpt.registry.service import ModelRegistryService

    settings = get_settings()
    session_factory = _session_factory()

    async with session_factory() as session:
        tenant, user = await _get_or_create_tenant(session, slug)
    # A real scope set, not the empty default — `ModelRegistryService.transition_stage` requires
    # `MODEL_PROMOTE` (Phase 14's promotion-authorization check) to move a candidate to
    # STAGING/PRODUCTION.
    tenant_context = TenantContext(
        tenant_id=tenant.id, principal_id=user.id, scopes=frozenset(_ALL_SCOPES)
    )

    async with _rls_bound_session(session_factory, tenant.id) as session:
        manager = ConnectionLifecycleManager(
            session,
            secret_provider=DualWriteSecretProvider(),
            pool_registry=TenantConnectionPoolRegistry(PostgresConnector()),
            connector=PostgresConnector(),
        )
        definition = DatasetDefinition(
            dataset_key=f"{slug}-delivery-delay-risk",
            task_key=DELIVERY_DELAY_RISK_CONTRACT.task_key,
            tenant_id=tenant.id,
            time_range_start=datetime(2020, 1, 1, tzinfo=UTC),
            time_range_end=datetime.now(UTC),
            label=LabelDefinition(
                function_name="delivery_delay_label",
                source_fields=(
                    "status",
                    "planned_arrival_at",
                    "actual_arrival_at",
                    "planned_distance_km",
                ),
                time_field="planned_departure_at",
            ),
        )
        builder = DatasetBuildService(session, connection_manager=manager)
        built = await builder.build(
            definition, contract=DELIVERY_DELAY_RISK_CONTRACT, tenant_context=tenant_context
        )
        await session.commit()

        code_revision = "demo"
        mlflow_tracking_uri = settings.mlflow_tracking_uri
        results = []
        baseline_service = BaselineTrainingService(session, mlflow_tracking_uri=mlflow_tracking_uri)
        for model_type in BaselineModelType:
            result = await baseline_service.train(
                built,
                contract=DELIVERY_DELAY_RISK_CONTRACT,
                config=TrainingConfig(model_type=model_type),
                tenant_context=tenant_context,
                code_revision=code_revision,
            )
            await session.commit()
            results.append(result)

        context_builder = RelationalContextBuilder(session, connection_manager=manager)
        hermes_service = HermesRPTTrainingService(
            session, context_builder=context_builder, mlflow_tracking_uri=mlflow_tracking_uri
        )
        hermes_result = await hermes_service.train(
            built,
            config=TINY,
            tenant_context=tenant_context,
            code_revision=code_revision,
            task_key=DELIVERY_DELAY_RISK_CONTRACT.task_key,
            checkpoint_dir=run_dir / "checkpoints" / slug,
        )
        await session.commit()

        all_results = [*results, hermes_result]
        best = min(all_results, key=lambda r: r.metrics.pr_auc * -1)
        registry = ModelRegistryService(session)
        promoted = await registry.transition_stage(
            best.model_version_id, to_stage=ModelStage.STAGING, tenant_context=tenant_context
        )
        promoted = await registry.transition_stage(
            promoted.id, to_stage=ModelStage.PRODUCTION, tenant_context=tenant_context
        )
        await session.commit()

    return {
        "dataset_id": str(built.manifest.dataset_id),
        "row_counts": built.manifest.row_counts,
        "quality_passed": built.manifest.quality_report.passed,
        "models": [
            {"model_type": r.model_type, "pr_auc": r.metrics.pr_auc, "roc_auc": r.metrics.roc_auc}
            for r in all_results
        ],
        "production_model_version_id": str(promoted.id),
        "production_model_type": best.model_type,
    }


async def cmd_train() -> None:
    _print_header(
        "TRAIN — building tenant datasets, training baselines + Hermes-RPT-0.1 Tiny, "
        "promoting to production"
    )
    report: dict[str, Any] = {}
    for slug in _TENANTS:
        print(f"\n  --- {slug} ---")
        result = await _build_dataset_and_train(slug, _RUN_DIR)
        print(f"  dataset rows: {result['row_counts']}  quality_passed={result['quality_passed']}")
        for m in result["models"]:
            print(f"    {m['model_type']}: pr_auc={m['pr_auc']:.3f} roc_auc={m['roc_auc']:.3f}")
        model_type = result["production_model_type"]
        model_id = result["production_model_version_id"]
        print(f"  production model: {model_type} ({model_id})")
        report[slug] = result
    _write_report("train", report)
    print("\nTraining complete. Next: make demo-predict")


# --- demo-predict ------------------------------------------------------------------------------


async def _pick_trip_reference(slug: str) -> str:
    """A real, already-`completed` trip business reference from the tenant's own database —
    exactly what a real caller would supply, never a platform-internal UUID."""

    from sqlalchemy.ext.asyncio import create_async_engine

    column = "trip_id" if slug == "demo-alpha" else "job_ref"
    table = "transport_trip" if slug == "demo-alpha" else "jobs"
    status_column = "status" if slug == "demo-alpha" else "job_status"
    engine = create_async_engine(_tenant_dsn(slug))
    try:
        async with engine.connect() as conn:
            # column/table/status_column are internal constants keyed off `slug` (one of two
            # fixed values), never external input.
            select_sql = f"SELECT {column} FROM {table} WHERE {status_column} = 'completed' LIMIT 1"  # noqa: S608 # nosec B608
            row = (await conn.execute(text(select_sql))).first()
    finally:
        await engine.dispose()
    if row is None:
        raise RuntimeError(f"No completed trip found for {slug} — run make demo-seed first")
    return str(row[0])


async def _predict_and_show_lineage(client: TestClient, *, slug: str) -> dict[str, Any]:
    from hermes_rpt.inference.models import PredictionResult
    from hermes_rpt.mappings.models import MappingVersion
    from hermes_rpt.registry.models import ModelVersion

    session_factory = _session_factory()
    async with session_factory() as session:
        tenant, user = await _get_or_create_tenant(session, slug)
    headers = _auth_headers(_token_for(tenant, user))
    trip_reference = await _pick_trip_reference(slug)

    response = client.post(
        "/v1/predictions/delivery-delay",
        headers=headers,
        json={"trip_id": trip_reference, "prediction_time": datetime.now(UTC).isoformat()},
    )
    response.raise_for_status()
    body = response.json()
    print(
        f"  {slug}: trip {trip_reference} -> delay_probability={body['delay_probability']:.3f} "
        f"risk={body['risk_level']} model={body['model_version']}"
    )

    async with _rls_bound_session(session_factory, tenant.id) as session:
        result = await session.get(PredictionResult, uuid.UUID(body["prediction_id"]))
        model_version = await session.get(ModelVersion, result.model_version_id)
        mapping_version = await session.get(MappingVersion, result.mapping_version_id)
        lineage = {
            "prediction_id": body["prediction_id"],
            "trip_reference": trip_reference,
            "delay_probability": body["delay_probability"],
            "risk_level": body["risk_level"],
            "model_version_id": str(model_version.id),
            "model_name": model_version.name,
            "model_stage": model_version.stage.value,
            "model_artifact_checksum": model_version.artifact_checksum,
            "mapping_version_id": str(mapping_version.id),
            "feature_version": result.feature_version,
            "explanation_count": len(result.explanations),
        }
    print(
        f"    lineage: model={lineage['model_name']}@{lineage['model_stage']} "
        f"(checksum {lineage['model_artifact_checksum'][:12]}...) "
        f"mapping_version={lineage['mapping_version_id']} "
        f"feature_version={lineage['feature_version']}"
    )
    return lineage


async def cmd_predict() -> None:
    _print_header("PREDICT — executing a real prediction per tenant and showing its full lineage")
    client = _build_client()
    report: dict[str, Any] = {}
    with client:
        for slug in _TENANTS:
            report[slug] = await _predict_and_show_lineage(client, slug=slug)
    _write_report("predict", report)
    print("\nPrediction complete. Next: make demo-security-test")


# --- demo-security-test ------------------------------------------------------------------------


async def _trigger_alpha_schema_drift() -> None:
    """Simulates the customer altering their own database out of band — exactly the kind of
    change discovery/drift-detection exists to catch (docs/runbooks/SCHEMA_DRIFT_RUNBOOK.md)."""

    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(_tenant_dsn("demo-alpha"))
    try:
        async with engine.begin() as conn:
            # Idempotent: a re-run after an earlier partial failure shouldn't crash trying to
            # rename a column that's already been renamed.
            exists = await conn.execute(
                text(
                    "SELECT 1 FROM information_schema.columns WHERE table_name = 'fleet_vehicle' "
                    "AND column_name = 'registration_no'"
                )
            )
            if exists.first() is not None:
                await conn.execute(
                    text("ALTER TABLE fleet_vehicle RENAME COLUMN registration_no TO reg_no")
                )
    finally:
        await engine.dispose()


async def _rediscover_and_suspend(
    client: TestClient, *, slug: str, headers: dict[str, str]
) -> dict[str, Any]:
    from hermes_rpt.mappings.service import MappingService
    from hermes_rpt.schemas.models import SchemaSnapshot

    connections = client.get("/v1/connections", headers=headers).json()
    connection_id = uuid.UUID(next(c["id"] for c in connections if c["name"] == "primary"))
    snapshot = await _run_discovery(client, connection_id=connection_id, headers=headers)
    drift_events = snapshot["drift_summary"]["events"] if snapshot["drift_summary"] else []

    session_factory = _session_factory()
    async with session_factory() as session:
        tenant, user = await _get_or_create_tenant(session, slug)
    tenant_context = TenantContext(tenant_id=tenant.id, principal_id=user.id)
    async with _rls_bound_session(session_factory, tenant.id) as session:
        snapshot_row = await session.get(SchemaSnapshot, uuid.UUID(snapshot["id"]))
        suspended = await MappingService(session).suspend_affected_by_drift(
            snapshot_row, tenant_context=tenant_context
        )
        await session.commit()

    return {
        "drift_event_count": len(drift_events),
        "drift_events": drift_events,
        "suspended_mapping_entities": [m.entity_name for m in suspended],
    }


async def _attempt_cross_tenant_access(client: TestClient) -> dict[str, Any]:
    session_factory = _session_factory()
    async with session_factory() as session:
        alpha_tenant, alpha_user = await _get_or_create_tenant(session, "demo-alpha")
        beta_tenant, beta_user = await _get_or_create_tenant(session, "demo-beta")
    alpha_headers = _auth_headers(_token_for(alpha_tenant, alpha_user))
    beta_headers = _auth_headers(_token_for(beta_tenant, beta_user))

    beta_connections = client.get("/v1/connections", headers=beta_headers).json()
    beta_connection_id = beta_connections[0]["id"]
    beta_mappings = client.get("/v1/mappings", headers=beta_headers).json()
    beta_mapping_id = beta_mappings[0]["id"]

    connection_attempt = client.get(f"/v1/connections/{beta_connection_id}", headers=alpha_headers)
    mapping_attempt = client.get(f"/v1/mappings/{beta_mapping_id}", headers=alpha_headers)

    return {
        "beta_connection_id": beta_connection_id,
        "alpha_access_to_beta_connection_status": connection_attempt.status_code,
        "beta_mapping_id": beta_mapping_id,
        "alpha_access_to_beta_mapping_status": mapping_attempt.status_code,
        "both_denied_as_not_found": connection_attempt.status_code == 404
        and mapping_attempt.status_code == 404,
    }


async def _prove_no_credential_leak() -> dict[str, Any]:
    """Deliberately a *direct* service call, not `client.post(...)` through `TestClient` — the
    latter runs the actual request (and everything it logs) on `TestClient`'s own background
    thread with its own event loop, and `structlog.configure()`'s effect does not reliably cross
    that thread boundary, which previously made this check vacuously "pass" by never actually
    capturing any log output at all.

    Two parts: (1) a real `validate_connection` call, against this demo's actual connection —
    proving no *incidental* leak in the real code path — and (2) a deliberate attempt to log the
    credential value directly through the same `hermes_rpt.common.logging` pipeline this demo's
    own code uses, proving the mandatory redaction processor
    (`hermes_rpt.common.logging._redact_processor`, `docs/THREAT_MODEL.md` T-I3) is actually
    wired up end to end — not just unit-tested in isolation
    (`tests/unit/test_logging_redaction.py`). Part 1 alone isn't enough: nothing in
    `hermes_rpt.connectors.service`/`postgres`/`health` logs anything on a *successful*
    validation at all, so it would always "pass" without ever proving anything.
    """

    import io

    import structlog

    from hermes_rpt.common.logging import configure_logging, get_logger
    from hermes_rpt.common.settings import get_settings as _get_settings

    stream = io.StringIO()
    configure_logging(_get_settings())
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=stream))

    password = _TENANTS["demo-alpha"]["db_password"]

    session_factory = _session_factory()
    async with session_factory() as session:
        tenant, user = await _get_or_create_tenant(session, "demo-alpha")
        tenant_context = TenantContext(tenant_id=tenant.id, principal_id=user.id)
        manager = ConnectionLifecycleManager(
            session,
            secret_provider=DualWriteSecretProvider(),
            pool_registry=get_pool_registry(),
            connector=PostgresConnector(),
        )
        connections = await session.execute(
            text("SELECT id FROM customer_database_connections WHERE tenant_id = :t LIMIT 1"),
            {"t": tenant.id},
        )
        connection_id = connections.scalar_one()
        await manager.validate_connection(connection_id, tenant_context=tenant_context)
        await session.commit()

    get_logger(__name__).info(
        "demo_security_test_redaction_probe",
        password=password,
        note=f"connect failed: host=db.internal password={password} dbname=hermes",
    )

    log_output = stream.getvalue()
    leaked_password = password in log_output
    return {"log_bytes_captured": len(log_output), "credential_leaked": leaked_password}


async def cmd_security_test() -> None:
    _print_header("SECURITY-TEST — schema drift, mapping suspension, tenant isolation, log safety")
    client = _build_client()
    report: dict[str, Any] = {}
    with client:
        print("\n  [1] Triggering schema drift on Alpha's real database (customer ALTER TABLE)...")
        await _trigger_alpha_schema_drift()

        session_factory = _session_factory()
        async with session_factory() as session:
            alpha_tenant, alpha_user = await _get_or_create_tenant(session, "demo-alpha")
        alpha_headers = _auth_headers(_token_for(alpha_tenant, alpha_user))

        print("  [2] Re-running discovery and suspending any mapping affected by the drift...")
        drift_result = await _rediscover_and_suspend(
            client, slug="demo-alpha", headers=alpha_headers
        )
        print(
            f"      drift events: {drift_result['drift_event_count']}, "
            f"suspended: {drift_result['suspended_mapping_entities']}"
        )
        report["schema_drift"] = drift_result

        print("  [3] Proving Tenant Beta (unaffected) still operates normally...")
        beta_prediction = await _predict_and_show_lineage(client, slug="demo-beta")
        report["beta_still_operating"] = {
            "prediction_id": beta_prediction["prediction_id"],
            "succeeded": True,
        }

        print("  [4] Attempting cross-tenant access (Alpha token against Beta's resources)...")
        cross_tenant_result = await _attempt_cross_tenant_access(client)
        print(f"      denied as not-found: {cross_tenant_result['both_denied_as_not_found']}")
        report["cross_tenant_access_attempt"] = cross_tenant_result

        print("  [5] Proving logs do not expose the customer database credential...")
        log_result = await _prove_no_credential_leak()
        print(f"      credential leaked in logs: {log_result['credential_leaked']}")
        report["log_credential_safety"] = log_result

    all_passed = (
        drift_result["suspended_mapping_entities"] != []
        and report["beta_still_operating"]["succeeded"]
        and cross_tenant_result["both_denied_as_not_found"]
        and not log_result["credential_leaked"]
        # A zero-byte capture would make "credential not leaked" vacuously true rather than an
        # actual proof — require real log output to have been captured.
        and log_result["log_bytes_captured"] > 0
    )
    report["all_checks_passed"] = all_passed
    _write_report("security-test", report)
    print(f"\nSecurity test {'PASSED' if all_passed else 'FAILED'} — see report above.")
    if not all_passed:
        raise SystemExit(1)


# --- CLI entrypoint ------------------------------------------------------------------------------


_COMMANDS = {
    "seed": cmd_seed,
    "discover": cmd_discover,
    "map": cmd_map,
    "train": cmd_train,
    "predict": cmd_predict,
    "security-test": cmd_security_test,
}


def main() -> None:
    parser = argparse.ArgumentParser(prog="scripts.demo")
    parser.add_argument("command", choices=sorted(_COMMANDS))
    args = parser.parse_args()
    asyncio.run(_COMMANDS[args.command]())


if __name__ == "__main__":
    main()
