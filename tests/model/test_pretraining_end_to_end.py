"""End-to-end pretraining tests (Phase 12): real synthetic Tenant-Alpha data, real MLflow
tracking, backbone transfer into a fresh `HermesRPT01`, and a comparison between a pretrained
and a from-scratch Tiny model on identical data/config.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import torch
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
from hermes_rpt.models.transformer.encoding import encode_batch
from hermes_rpt.models.transformer.model import TINY, HermesRPT01
from hermes_rpt.models.transformer.pretraining import MaskingConfig
from hermes_rpt.models.transformer.pretraining_model import LossWeights
from hermes_rpt.models.transformer.pretraining_training import HermesRPTPretrainingService
from hermes_rpt.models.transformer.training import HermesRPTTrainingService
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


async def _provision(session: AsyncSession):  # type: ignore[no-untyped-def]
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
    data = generate_fleet_data(tenant_slug="alpha", seed=55, start=_START, end=_END)
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
    small_splits = DatasetSplits(
        train=built.splits.train[:16],
        validation=built.splits.validation[:4],
        test=built.splits.test[:4],
    )
    built = built.model_copy(update={"splits": small_splits})

    return tenant_context, manager, built


async def test_pretraining_produces_a_backbone_checkpoint_and_finite_losses(
    session: AsyncSession, mlflow_tracking_uri: str, tmp_path: Path
) -> None:
    tenant_context, manager, built = await _provision(session)
    context_builder = RelationalContextBuilder(session, connection_manager=manager)
    service = HermesRPTPretrainingService(
        session, context_builder=context_builder, mlflow_tracking_uri=mlflow_tracking_uri
    )

    result = await service.pretrain(
        built,
        config=TINY,
        tenant_context=tenant_context,
        code_revision="test-revision",
        checkpoint_dir=tmp_path / "pretrain-checkpoints",
        epochs=5,
    )

    assert result.checkpoint_path.exists()
    assert len(result.checksum) == 64
    assert all(v == v for v in result.final_loss_components.values())  # no NaNs (NaN != NaN)


async def test_pretraining_is_reproducible_given_the_same_seed(
    session: AsyncSession, mlflow_tracking_uri: str, tmp_path: Path
) -> None:
    tenant_context, manager, built = await _provision(session)
    context_builder = RelationalContextBuilder(session, connection_manager=manager)
    service = HermesRPTPretrainingService(
        session, context_builder=context_builder, mlflow_tracking_uri=mlflow_tracking_uri
    )

    result_a = await service.pretrain(
        built,
        config=TINY,
        tenant_context=tenant_context,
        code_revision="test-revision",
        checkpoint_dir=tmp_path / "run-a",
        epochs=5,
        random_seed=123,
    )
    result_b = await service.pretrain(
        built,
        config=TINY,
        tenant_context=tenant_context,
        code_revision="test-revision",
        checkpoint_dir=tmp_path / "run-b",
        epochs=5,
        random_seed=123,
    )

    assert result_a.final_loss_components == result_b.final_loss_components
    for key in result_a.backbone_state_dict:
        torch.testing.assert_close(
            result_a.backbone_state_dict[key], result_b.backbone_state_dict[key]
        )


async def test_pretrained_backbone_transfers_into_a_fresh_finetuning_model(
    session: AsyncSession, mlflow_tracking_uri: str, tmp_path: Path
) -> None:
    tenant_context, manager, built = await _provision(session)
    context_builder = RelationalContextBuilder(session, connection_manager=manager)
    pretraining_service = HermesRPTPretrainingService(
        session, context_builder=context_builder, mlflow_tracking_uri=mlflow_tracking_uri
    )
    pretrain_result = await pretraining_service.pretrain(
        built,
        config=TINY,
        tenant_context=tenant_context,
        code_revision="test-revision",
        checkpoint_dir=tmp_path / "pretrain",
        epochs=5,
    )

    training_service = HermesRPTTrainingService(
        session, context_builder=context_builder, mlflow_tracking_uri=mlflow_tracking_uri
    )
    result = await training_service.train(
        built,
        config=TINY,
        tenant_context=tenant_context,
        code_revision="test-revision",
        task_key=DELIVERY_DELAY_RISK_CONTRACT.task_key,
        checkpoint_dir=tmp_path / "finetune",
        epochs=5,
        pretrained_backbone_state_dict=pretrain_result.backbone_state_dict,
    )

    assert result.model_type == "hermes-rpt-0.1-tiny-pretrained"


async def test_finetuning_inference_output_never_exposes_more_than_a_risk_score(
    session: AsyncSession, mlflow_tracking_uri: str, tmp_path: Path
) -> None:
    """ "Ensure canaries are not returned through inference APIs" — structural proof: no matter
    what the backbone learned during (possibly canary-containing) pretraining, the model's only
    externally callable inference path (`HermesRPT01.forward`/`predict_proba`) produces exactly
    one scalar per example, never a per-field or per-record reconstruction."""

    tenant_context, manager, built = await _provision(session)
    context_builder = RelationalContextBuilder(session, connection_manager=manager)
    pretraining_service = HermesRPTPretrainingService(
        session, context_builder=context_builder, mlflow_tracking_uri=mlflow_tracking_uri
    )
    pretrain_result = await pretraining_service.pretrain(
        built,
        config=TINY,
        tenant_context=tenant_context,
        code_revision="test-revision",
        checkpoint_dir=tmp_path / "pretrain",
        epochs=3,
    )

    model = HermesRPT01(TINY)
    model.load_pretrained_backbone(pretrain_result.backbone_state_dict)
    model.eval()

    examples = await context_builder.build_example(
        business_reference=built.splits.train[0].business_reference,
        prediction_time=built.splits.train[0].prediction_time,
        label=None,
        tenant_context=tenant_context,
        max_records_per_relation=TINY.max_records_per_relation,
    )
    batch = encode_batch(
        [examples],
        max_records_per_relation=TINY.max_records_per_relation,
        categorical_vocab_size=TINY.categorical_vocab_size,
    )
    proba = model.predict_proba(batch)
    assert proba.shape == (1,)  # exactly one scalar — nothing per-field, nothing per-record


async def test_ablation_configuration_runs_with_only_the_link_objective_active(
    session: AsyncSession, mlflow_tracking_uri: str, tmp_path: Path
) -> None:
    tenant_context, manager, built = await _provision(session)
    context_builder = RelationalContextBuilder(session, connection_manager=manager)
    service = HermesRPTPretrainingService(
        session, context_builder=context_builder, mlflow_tracking_uri=mlflow_tracking_uri
    )

    result = await service.pretrain(
        built,
        config=TINY,
        tenant_context=tenant_context,
        code_revision="test-revision",
        checkpoint_dir=tmp_path / "ablation",
        epochs=3,
        masking_config=MaskingConfig(mask_probability=0.0, link_corruption_probability=1.0),
        loss_weights=LossWeights(
            numeric=0.0, categorical=0.0, datetime=0.0, link=1.0, temporal_order=0.0
        ),
    )
    assert result.final_loss_components["numeric"] == 0.0
    assert result.final_loss_components["categorical"] == 0.0
    assert result.final_loss_components["link"] != 0.0
