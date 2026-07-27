"""End-to-end dataset build tests (Phase 9) for Tenant Alpha's and Tenant Beta's differently
named synthetic schemas — "generate synthetic Alpha and Beta datasets... different source
schemas, similar canonical concepts." Runs the full pipeline (synthetic generation -> SQLite
load -> real connection/mapping registration -> DatasetBuildService) via `tests.fakes.
SQLiteConnector`, mirroring `tests/security/test_feature_extraction_service.py`'s pattern.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from hermes_rpt.connectors.enums import DatabaseEngine
from hermes_rpt.connectors.pool_registry import TenantConnectionPoolRegistry
from hermes_rpt.connectors.service import ConnectionLifecycleManager
from hermes_rpt.datasets.builder import DatasetBuildService
from hermes_rpt.datasets.definition import DatasetDefinition, LabelDefinition
from hermes_rpt.datasets.quality import QualitySeverity
from hermes_rpt.features.contract import DELIVERY_DELAY_RISK_CONTRACT
from hermes_rpt.mappings.service import MappingService
from hermes_rpt.schemas.enums import DiscoveryStatus
from hermes_rpt.schemas.introspection import SchemaIntrospectionResult
from hermes_rpt.schemas.models import SchemaSnapshot
from hermes_rpt.schemas.repository import SchemaSnapshotRepository
from hermes_rpt.secrets.provider import LocalDevSecretProvider
from hermes_rpt.synthetic.generator import generate_fleet_data
from hermes_rpt.synthetic.loader import create_tenant_schema, load_fleet_data
from hermes_rpt.synthetic.schemas import ALL_ENTITY_SCHEMAS
from hermes_rpt.tenants.context import TenantContext
from hermes_rpt.tenants.repository import TenantRepository, UserRepository
from tests.factories import make_tenant, make_user
from tests.fakes import SQLiteConnector

_START = datetime(2026, 1, 1, tzinfo=UTC)
_END = datetime(2026, 3, 1, tzinfo=UTC)
_LABEL = LabelDefinition(
    function_name="delivery_delay_label",
    source_fields=("status", "planned_arrival_at", "actual_arrival_at", "planned_distance_km"),
    time_field="planned_departure_at",
)


async def _make_sqlite_engine():  # type: ignore[no-untyped-def]
    return create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )


async def _provision_tenant(
    session: AsyncSession, *, tenant_slug: str, seed: int
) -> tuple[TenantContext, ConnectionLifecycleManager]:
    """Generates synthetic fleet data for `tenant_slug`, loads it into a fresh SQLite engine,
    registers a real tenant + connection + activated mapping for every one of the seven entities
    the delivery-delay-risk contract touches, and returns a ready-to-use TenantContext +
    ConnectionLifecycleManager."""

    tenant = await TenantRepository(session).add(make_tenant(name=tenant_slug, slug=tenant_slug))
    user = await UserRepository(session).add(make_user(email=f"{tenant_slug}@example.com"))
    await session.commit()
    tenant_context = TenantContext(tenant_id=tenant.id, principal_id=user.id)

    data_engine = await _make_sqlite_engine()
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
            host="db.internal",
            port=5432,
            database_name="db",
            username="app",
            secret_value="unit-test-secret",  # noqa: S106
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

    return tenant_context, manager


def _definition(*, dataset_key: str, tenant_id) -> DatasetDefinition:  # type: ignore[no-untyped-def]
    return DatasetDefinition(
        dataset_key=dataset_key,
        task_key="delivery-delay-risk",
        tenant_id=tenant_id,
        time_range_start=_START,
        time_range_end=_END,
        label=_LABEL,
    )


async def test_build_end_to_end_for_alpha_schema(session: AsyncSession) -> None:
    tenant_context, manager = await _provision_tenant(session, tenant_slug="alpha", seed=101)
    builder = DatasetBuildService(session, connection_manager=manager)
    built = await builder.build(
        _definition(dataset_key="alpha-delay-risk", tenant_id=tenant_context.tenant_id),
        contract=DELIVERY_DELAY_RISK_CONTRACT,
        tenant_context=tenant_context,
    )

    total_rows = sum(built.manifest.row_counts.values())
    assert total_rows > 0
    assert built.manifest.label_statistics.count == total_rows
    # Every required feature is present (not missing) on every assembled row.
    for split in (built.splits.train, built.splits.validation, built.splits.test):
        for row in split:
            assert row.features["planned_departure_hour"] is not None
            assert row.features["planned_trip_distance_km"] is not None


async def test_build_end_to_end_for_beta_schema(session: AsyncSession) -> None:
    """Same pipeline, completely different table/column names — proof the build is
    schema-agnostic, not tuned to Alpha's naming."""

    tenant_context, manager = await _provision_tenant(session, tenant_slug="beta", seed=202)
    builder = DatasetBuildService(session, connection_manager=manager)
    built = await builder.build(
        _definition(dataset_key="beta-delay-risk", tenant_id=tenant_context.tenant_id),
        contract=DELIVERY_DELAY_RISK_CONTRACT,
        tenant_context=tenant_context,
    )

    assert sum(built.manifest.row_counts.values()) > 0
    assert built.manifest.lineage.task_key == "delivery-delay-risk"
    assert set(built.manifest.lineage.mapping_version_ids) == {"Trip"}


async def test_build_detects_injected_data_quality_defects(session: AsyncSession) -> None:
    """The synthetic generator deliberately injects a handful of bad rows
    (`hermes_rpt.synthetic.generator._inject_defects`) — this proves the quality checks actually
    catch them in a real end-to-end build, not just in isolated unit tests."""

    tenant_context, manager = await _provision_tenant(session, tenant_slug="alpha", seed=303)
    builder = DatasetBuildService(session, connection_manager=manager)
    built = await builder.build(
        _definition(dataset_key="alpha-quality-check", tenant_id=tenant_context.tenant_id),
        contract=DELIVERY_DELAY_RISK_CONTRACT,
        tenant_context=tenant_context,
    )

    checks_seen = {issue.check for issue in built.manifest.quality_report.issues}
    assert "invalid_dates" in checks_seen
    assert "negative_distances" in checks_seen
    assert "unrecognised_statuses" in checks_seen
    assert any(
        issue.severity == QualitySeverity.ERROR for issue in built.manifest.quality_report.issues
    )
    assert built.manifest.quality_report.passed is False


async def test_dataset_never_mixes_tenants(session: AsyncSession) -> None:
    """Structural proof of "a dataset cannot contain multiple tenants by accident": Alpha's and
    Beta's builds run against completely independent SQLite engines/mappings under separate
    TenantContexts, and each build's own cross-tenant-contamination check must pass."""

    alpha_context, alpha_manager = await _provision_tenant(session, tenant_slug="alpha", seed=404)
    beta_context, beta_manager = await _provision_tenant(session, tenant_slug="beta", seed=505)

    alpha_built = await DatasetBuildService(session, connection_manager=alpha_manager).build(
        _definition(dataset_key="alpha-isolation", tenant_id=alpha_context.tenant_id),
        contract=DELIVERY_DELAY_RISK_CONTRACT,
        tenant_context=alpha_context,
    )
    beta_built = await DatasetBuildService(session, connection_manager=beta_manager).build(
        _definition(dataset_key="beta-isolation", tenant_id=beta_context.tenant_id),
        contract=DELIVERY_DELAY_RISK_CONTRACT,
        tenant_context=beta_context,
    )

    contamination_checks = [
        issue
        for issue in (
            *alpha_built.manifest.quality_report.issues,
            *beta_built.manifest.quality_report.issues,
        )
        if issue.check == "cross_tenant_contamination"
    ]
    assert contamination_checks == []  # no issue means the check passed (found nothing) for both
    assert alpha_built.manifest.tenant_id == alpha_context.tenant_id
    assert beta_built.manifest.tenant_id == beta_context.tenant_id
    assert alpha_built.manifest.tenant_id != beta_built.manifest.tenant_id


async def test_checksum_is_reproducible_across_two_builds_of_the_same_data(
    session: AsyncSession,
) -> None:
    tenant_context, manager = await _provision_tenant(session, tenant_slug="alpha", seed=606)
    builder = DatasetBuildService(session, connection_manager=manager)
    definition = _definition(dataset_key="alpha-repro", tenant_id=tenant_context.tenant_id)

    first = await builder.build(
        definition, contract=DELIVERY_DELAY_RISK_CONTRACT, tenant_context=tenant_context
    )
    second = await builder.build(
        definition, contract=DELIVERY_DELAY_RISK_CONTRACT, tenant_context=tenant_context
    )
    assert first.manifest.checksum == second.manifest.checksum
