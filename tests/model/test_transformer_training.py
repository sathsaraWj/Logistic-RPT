"""End-to-end training test for Hermes-RPT-0.1 (Tiny) — Phase 11: real synthetic Tenant-Alpha
data, real `DatasetBuildService` + `RelationalContextBuilder`, real MLflow tracking (SQLite-
backed, isolated per test) and registry, real (short) training loop.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from hermes_rpt.connectors.enums import DatabaseEngine
from hermes_rpt.connectors.pool_registry import TenantConnectionPoolRegistry
from hermes_rpt.connectors.service import ConnectionLifecycleManager
from hermes_rpt.datasets.builder import DatasetBuildService
from hermes_rpt.datasets.definition import DatasetDefinition, LabelDefinition
from hermes_rpt.datasets.split import DatasetSplits
from hermes_rpt.features.contract import DELIVERY_DELAY_RISK_CONTRACT
from hermes_rpt.inference.models import PredictionTaskDefinition
from hermes_rpt.mappings.service import MappingService
from hermes_rpt.models.transformer.context import RelationalContextBuilder
from hermes_rpt.models.transformer.model import TINY
from hermes_rpt.models.transformer.training import HermesRPTTrainingService
from hermes_rpt.registry.enums import ModelStage
from hermes_rpt.registry.repository import ModelVersionRepository
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


@pytest.fixture
def mlflow_tracking_uri(tmp_path: Path) -> str:
    return f"sqlite:///{(tmp_path / 'mlflow.db').as_posix()}"


async def test_hermes_rpt_tiny_trains_end_to_end_and_registers_a_candidate(
    session: AsyncSession, mlflow_tracking_uri: str, tmp_path: Path
) -> None:
    session.add(
        PredictionTaskDefinition(
            task_key=DELIVERY_DELAY_RISK_CONTRACT.task_key,
            name="Delivery Delay Risk",
            feature_contract_version=DELIVERY_DELAY_RISK_CONTRACT.version,
        )
    )
    await session.flush()

    tenant = await TenantRepository(session).add(make_tenant(name="alpha", slug="alpha"))
    user = await UserRepository(session).add(make_user(email="alpha@example.com"))
    await session.commit()
    tenant_context = TenantContext(tenant_id=tenant.id, principal_id=user.id)

    data_engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    await create_tenant_schema(data_engine, tenant_slug="alpha")
    data = generate_fleet_data(tenant_slug="alpha", seed=77, start=_START, end=_END)
    await load_fleet_data(data_engine, data, tenant_slug="alpha")

    connector = SQLiteConnector(data_engine)
    manager = ConnectionLifecycleManager(
        session,
        secret_provider=LocalDevSecretProvider(),
        pool_registry=TenantConnectionPoolRegistry(connector),
        connector=connector,
    )
    mapping_service = MappingService(session)
    for schema in ALL_ENTITY_SCHEMAS:
        document = schema.mapping_document("alpha")
        connection = await manager.register_connection(
            tenant_context=tenant_context,
            name=f"alpha-{schema.entity}",
            engine=DatabaseEngine.POSTGRESQL,
            host="db.internal",
            port=5432,
            database_name="db",
            username="app",
            secret_value="unit-test-secret",  # noqa: S106
            schema_allowlist=["main"],
            table_allowlist=[schema.table("alpha")],
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
        dataset_key="alpha-delivery-delay-risk",
        task_key=DELIVERY_DELAY_RISK_CONTRACT.task_key,
        tenant_id=tenant.id,
        time_range_start=_START,
        time_range_end=_END,
        label=_LABEL,
    )
    dataset_builder = DatasetBuildService(session, connection_manager=manager)
    built = await dataset_builder.build(
        definition, contract=DELIVERY_DELAY_RISK_CONTRACT, tenant_context=tenant_context
    )
    # Keep the test fast: a handful of rows per split is enough to exercise the full wiring.
    small_splits = DatasetSplits(
        train=built.splits.train[:12],
        validation=built.splits.validation[:4],
        test=built.splits.test[:4],
    )
    built = built.model_copy(update={"splits": small_splits})

    context_builder = RelationalContextBuilder(session, connection_manager=manager)
    training_service = HermesRPTTrainingService(
        session, context_builder=context_builder, mlflow_tracking_uri=mlflow_tracking_uri
    )
    result = await training_service.train(
        built,
        config=TINY,
        tenant_context=tenant_context,
        code_revision="test-revision",
        task_key=DELIVERY_DELAY_RISK_CONTRACT.task_key,
        checkpoint_dir=tmp_path / "checkpoints",
        epochs=5,
    )
    await session.commit()

    assert result.model_type == "hermes-rpt-0.1-tiny-scratch"
    assert 0.0 <= result.metrics.roc_auc <= 1.0
    assert len(result.artifact_checksum) == 64

    model_version = await ModelVersionRepository(session).get(result.model_version_id)
    assert model_version is not None
    assert model_version.stage == ModelStage.CANDIDATE
    assert model_version.tenant_id == tenant.id
    assert (tmp_path / "checkpoints" / "hermes_rpt_tiny.pt").exists()
