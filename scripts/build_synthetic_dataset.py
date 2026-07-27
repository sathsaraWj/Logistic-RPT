#!/usr/bin/env python3
"""`make build-synthetic-dataset` entry point (Phase 9).

Generates synthetic Tenant-Alpha and Tenant-Beta fleet data, loads each into its own SQLite
file, registers real tenants/connections/mappings against an ephemeral control-plane database,
builds a delivery-delay-risk dataset for each tenant through the real
`hermes_rpt.datasets.builder.DatasetBuildService`, and writes a manifest (metadata only — no
feature/label values) plus a safe, aggregate data-quality report per tenant.

Everything this script writes lives under `data/synthetic_datasets/<run_id>/` — outside the
repository's tracked tree (`data/` is gitignored) — "generated data must be stored outside the
source repository." Nothing printed to stdout or written to disk here is a raw customer value:
this is synthetic data end to end, and even so, only aggregate statistics/manifests are
persisted, consistent with the platform's general no-raw-values-in-metadata posture.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

REPO_ROOT = Path(__file__).resolve().parent.parent
# `hermes_rpt` is already importable via the editable install `uv sync` creates; only `tests`
# (this script's source of `SQLiteConnector` and the tenant/user factories — plain, pytest-free
# dev helpers, safe to reuse here) needs the repo root added explicitly.
sys.path.insert(0, str(REPO_ROOT))

from tests.factories import make_tenant, make_user  # noqa: E402
from tests.fakes import SQLiteConnector  # noqa: E402

from hermes_rpt.common import model_registry  # noqa: E402, F401 - populates Base.metadata
from hermes_rpt.common.db import Base  # noqa: E402
from hermes_rpt.connectors.enums import DatabaseEngine  # noqa: E402
from hermes_rpt.connectors.pool_registry import TenantConnectionPoolRegistry  # noqa: E402
from hermes_rpt.connectors.service import ConnectionLifecycleManager  # noqa: E402
from hermes_rpt.datasets.builder import BuiltDataset, DatasetBuildService  # noqa: E402
from hermes_rpt.datasets.definition import DatasetDefinition, LabelDefinition  # noqa: E402
from hermes_rpt.features.contract import DELIVERY_DELAY_RISK_CONTRACT  # noqa: E402
from hermes_rpt.mappings.service import MappingService  # noqa: E402
from hermes_rpt.schemas.enums import DiscoveryStatus  # noqa: E402
from hermes_rpt.schemas.introspection import SchemaIntrospectionResult  # noqa: E402
from hermes_rpt.schemas.models import SchemaSnapshot  # noqa: E402
from hermes_rpt.schemas.repository import SchemaSnapshotRepository  # noqa: E402
from hermes_rpt.secrets.provider import LocalDevSecretProvider  # noqa: E402
from hermes_rpt.synthetic.generator import generate_fleet_data  # noqa: E402
from hermes_rpt.synthetic.loader import create_tenant_schema, load_fleet_data  # noqa: E402
from hermes_rpt.synthetic.schemas import ALL_ENTITY_SCHEMAS  # noqa: E402
from hermes_rpt.tenants.context import TenantContext  # noqa: E402
from hermes_rpt.tenants.repository import TenantRepository, UserRepository  # noqa: E402

_START = datetime(2026, 1, 1, tzinfo=UTC)
# Deliberately extends slightly past "now" so the generator's injected future-timestamp defect
# (see hermes_rpt.synthetic.generator._inject_defects) actually falls inside this run's query
# range and gets caught by check_future_timestamps, rather than silently never being fetched.
_END = datetime.now(UTC) + timedelta(days=1)
_LABEL = LabelDefinition(
    function_name="delivery_delay_label",
    source_fields=("status", "planned_arrival_at", "actual_arrival_at", "planned_distance_km"),
    time_field="planned_departure_at",
)


async def provision_and_build_dataset(
    control_plane_session_factory, *, tenant_slug: str, seed: int, data_dir: Path
) -> tuple[TenantContext, ConnectionLifecycleManager, BuiltDataset]:
    """Generates fleet data for `tenant_slug`, loads it into `data_dir/<tenant_slug>.sqlite3`,
    registers a real tenant with activated mappings for all seven synthetic entities, and builds
    a `delivery-delay-risk` dataset. Reused by `apps.trainer.main`'s `baselines` and `hermes-rpt`
    commands (Phases 10-11) as well as this script's own `main()`, so this orchestration lives in
    exactly one place.

    Returns the `ConnectionLifecycleManager` too (not just the tenant/dataset) — Phase 11's
    relational transformer needs to keep querying the tenant's customer database *after* this
    call returns (raw per-record context, not just Phase 9's pre-extracted scalar features), so
    the underlying session/engine are deliberately left open rather than closed here. This is a
    one-shot CLI tool, not a long-running service: the caller's own control-plane engine
    disposal at the end of `main()` is what ultimately cleans these up, not an explicit
    close/dispose in this function.
    """

    session = control_plane_session_factory()
    tenant = await TenantRepository(session).add(make_tenant(name=tenant_slug, slug=tenant_slug))
    user = await UserRepository(session).add(make_user(email=f"{tenant_slug}@example.com"))
    await session.commit()
    tenant_context = TenantContext(tenant_id=tenant.id, principal_id=user.id)

    data_path = data_dir / f"{tenant_slug}.sqlite3"
    data_engine = create_async_engine(
        f"sqlite+aiosqlite:///{data_path}",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    await create_tenant_schema(data_engine, tenant_slug=tenant_slug)
    data = generate_fleet_data(tenant_slug=tenant_slug, seed=seed, start=_START, end=_END)
    await load_fleet_data(data_engine, data, tenant_slug=tenant_slug)

    connector = SQLiteConnector(data_engine)
    manager = ConnectionLifecycleManager(
        session,
        secret_provider=LocalDevSecretProvider(),
        pool_registry=TenantConnectionPoolRegistry(connector),
        connector=connector,
    )
    mapping_service = MappingService(session)
    for schema in ALL_ENTITY_SCHEMAS:
        document = schema.mapping_document(tenant_slug)
        connection = await manager.register_connection(
            tenant_context=tenant_context,
            name=f"{tenant_slug}-{schema.entity}",
            engine=DatabaseEngine.POSTGRESQL,
            host="local-synthetic",
            port=5432,
            database_name=data_path.name,
            username="synthetic",
            secret_value="synthetic-not-a-real-secret",  # noqa: S106  # nosec B106
            schema_allowlist=["main"],
            table_allowlist=[schema.table(tenant_slug)],
        )
        await session.commit()
        snapshot = await SchemaSnapshotRepository(session).add(
            SchemaSnapshot(
                connection_id=connection.id,
                sequence_number=1,
                status=DiscoveryStatus.COMPLETED,
                metadata_document=SchemaIntrospectionResult(schemas=["main"], tables=[]).model_dump(
                    mode="json"
                ),
            ),
            tenant_context=tenant_context,
        )
        await session.commit()
        mapping, _version = await mapping_service.create_draft(
            tenant_context=tenant_context, schema_snapshot_id=snapshot.id, document=document
        )
        await session.commit()
        await mapping_service.submit_for_validation(mapping.id, tenant_context=tenant_context)
        await mapping_service.approve(mapping.id, tenant_context=tenant_context)
        await mapping_service.activate(mapping.id, tenant_context=tenant_context)
        await session.commit()

    definition = DatasetDefinition(
        dataset_key=f"{tenant_slug}-delivery-delay-risk",
        task_key=DELIVERY_DELAY_RISK_CONTRACT.task_key,
        tenant_id=tenant.id,
        time_range_start=_START,
        time_range_end=_END,
        label=_LABEL,
    )
    builder = DatasetBuildService(session, connection_manager=manager)
    built = await builder.build(
        definition, contract=DELIVERY_DELAY_RISK_CONTRACT, tenant_context=tenant_context
    )
    return tenant_context, manager, built


def _print_report(tenant_slug: str, built: BuiltDataset) -> None:
    manifest = built.manifest
    print(f"\n=== {tenant_slug.upper()} — {manifest.dataset_key} ===")
    print(f"dataset_id: {manifest.dataset_id}")
    print(f"row_counts: {manifest.row_counts}")
    print(
        f"label balance: {manifest.label_statistics.positive_count}/"
        f"{manifest.label_statistics.count} positive "
        f"({manifest.label_statistics.positive_fraction:.1%})"
    )
    print(f"checksum: {manifest.checksum}")
    print(f"quality report passed: {manifest.quality_report.passed}")
    for issue in manifest.quality_report.issues:
        print(f"  [{issue.severity.value}] {issue.check}: {issue.message}")
    print("feature statistics (name: mean / missing_count):")
    for stat in manifest.feature_statistics:
        mean_display = f"{stat.mean:.3f}" if stat.mean is not None else "n/a"
        print(f"  {stat.feature_name}: {mean_display} (missing={stat.missing_count})")


async def main() -> None:
    run_id = f"{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"
    run_dir = REPO_ROOT / "data" / "synthetic_datasets" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    control_plane_engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with control_plane_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(bind=control_plane_engine, expire_on_commit=False)

    results: dict[str, BuiltDataset] = {}
    for tenant_slug, seed in (("alpha", 1001), ("beta", 2002)):
        _tenant_context, _manager, built = await provision_and_build_dataset(
            session_factory, tenant_slug=tenant_slug, seed=seed, data_dir=run_dir
        )
        results[tenant_slug] = built

    for tenant_slug, built in results.items():
        _print_report(tenant_slug, built)
        manifest_path = run_dir / f"{tenant_slug}-manifest.json"
        manifest_path.write_text(built.manifest.model_dump_json(indent=2), encoding="utf-8")

    await control_plane_engine.dispose()
    print(f"\nRun artifacts written to: {run_dir}")


if __name__ == "__main__":
    asyncio.run(main())
