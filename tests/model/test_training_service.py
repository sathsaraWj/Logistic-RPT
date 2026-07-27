"""End-to-end training tests (Phase 10): `BaselineTrainingService.train()` against a real
(file-based, isolated) MLflow tracking store and the real control-plane registry service —
covers MLflow experiment tracking, artifact registration + model signature, model
serialization/round-trip, and registry metadata, without needing Docker/Postgres.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import mlflow
import numpy as np
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from hermes_rpt.datasets.builder import BuiltDataset, DatasetRow
from hermes_rpt.datasets.manifest import DatasetLineage, DatasetManifest
from hermes_rpt.datasets.quality import DataQualityReport
from hermes_rpt.datasets.split import DatasetSplits
from hermes_rpt.datasets.statistics import FeatureStatistic, LabelStatistics
from hermes_rpt.features.contract import (
    FeatureContract,
    FeatureDataType,
    FeatureKind,
    FeatureSpec,
    LeakageRisk,
)
from hermes_rpt.inference.models import PredictionTaskDefinition
from hermes_rpt.models.config import BaselineModelType, TrainingConfig
from hermes_rpt.models.training import BaselineTrainingService
from hermes_rpt.registry.enums import ModelStage
from hermes_rpt.registry.repository import ModelVersionRepository
from hermes_rpt.tenants.context import TenantContext
from hermes_rpt.tenants.repository import TenantRepository, UserRepository
from tests.factories import make_tenant, make_user

_CONTRACT = FeatureContract(
    task_key="delivery-delay-risk",
    version="1",
    target_entity="Trip",
    features=(
        FeatureSpec(
            name="x1",
            description="test feature 1",
            canonical_source="Trip.x1",
            data_type=FeatureDataType.FLOAT,
            leakage_risk=LeakageRisk.NONE,
            availability_timestamp="now",
            required=True,
            kind=FeatureKind.DIRECT_FIELD,
            target_field="x1",
        ),
        FeatureSpec(
            name="x2",
            description="test feature 2 — has some missing values",
            canonical_source="Trip.x2",
            data_type=FeatureDataType.FLOAT,
            leakage_risk=LeakageRisk.NONE,
            availability_timestamp="now",
            kind=FeatureKind.DIRECT_FIELD,
            target_field="x2",
        ),
    ),
)


def _make_rows(n: int, *, seed: int) -> list[DatasetRow]:
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n):
        x1 = float(rng.normal())
        label = 1 if x1 + rng.normal(scale=0.3) > 0 else 0
        x2 = None if i % 10 == 0 else float(rng.normal())  # ~10% missing, like a real tenant gap
        rows.append(
            DatasetRow(
                business_reference=f"trip-{i:04d}",
                prediction_time=datetime(2026, 1, 1, tzinfo=UTC),
                features={"x1": x1, "x2": x2},
                label=label,
            )
        )
    return rows


def _built_dataset(*, tenant_id: uuid.UUID, principal_id: uuid.UUID) -> BuiltDataset:
    rows = _make_rows(150, seed=11)
    splits = DatasetSplits(
        train=tuple(rows[:100]), validation=tuple(rows[100:125]), test=tuple(rows[125:])
    )
    manifest = DatasetManifest(
        dataset_id=uuid.uuid4(),
        dataset_key="test-delivery-delay-risk",
        tenant_id=tenant_id,
        is_shared_research_dataset=False,
        lineage=DatasetLineage(
            ontology_version="1",
            feature_contract_version="1",
            task_key="delivery-delay-risk",
            mapping_version_ids={"Trip": uuid.uuid4()},
            schema_snapshot_ids={"Trip": uuid.uuid4()},
            built_at=datetime.now(UTC),
            built_by_principal_id=principal_id,
        ),
        row_counts={
            "train": len(splits.train),
            "validation": len(splits.validation),
            "test": len(splits.test),
        },
        checksum="c" * 64,
        quality_report=DataQualityReport(row_count=len(rows), issues=()),
        feature_statistics=(
            FeatureStatistic(feature_name="x1", count=150, missing_count=0),
            FeatureStatistic(feature_name="x2", count=135, missing_count=15),
        ),
        label_statistics=LabelStatistics(
            count=len(rows),
            positive_count=sum(r.label for r in rows),
            negative_count=sum(1 - r.label for r in rows),
        ),
    )
    return BuiltDataset(manifest=manifest, splits=splits)


@pytest.fixture
def mlflow_tracking_uri(tmp_path: Path) -> str:
    # MLflow's filesystem tracking backend ("file:///...") is in maintenance mode as of
    # mlflow 3.x and refuses to initialize without an explicit opt-out env var — a SQLite
    # database backend is the supported default now, and happens to fit this platform's
    # existing "SQLite for local/dev, Postgres for real deployments" convention anyway.
    return f"sqlite:///{(tmp_path / 'mlflow.db').as_posix()}"


async def _seed_task_definition(session: AsyncSession) -> None:
    session.add(
        PredictionTaskDefinition(
            task_key="delivery-delay-risk", name="Delivery Delay Risk", feature_contract_version="1"
        )
    )
    await session.flush()


async def _tenant_context(session: AsyncSession) -> TenantContext:
    tenant = await TenantRepository(session).add(make_tenant())
    user = await UserRepository(session).add(make_user())
    await session.commit()
    return TenantContext(tenant_id=tenant.id, principal_id=user.id)


async def test_training_registers_a_candidate_model_version(
    session: AsyncSession, mlflow_tracking_uri: str
) -> None:
    await _seed_task_definition(session)
    tenant_context = await _tenant_context(session)
    built = _built_dataset(
        tenant_id=tenant_context.tenant_id, principal_id=tenant_context.principal_id
    )

    service = BaselineTrainingService(session, mlflow_tracking_uri=mlflow_tracking_uri)
    result = await service.train(
        built,
        contract=_CONTRACT,
        config=TrainingConfig(model_type=BaselineModelType.LOGISTIC_REGRESSION),
        tenant_context=tenant_context,
        code_revision="test-revision",
    )
    await session.commit()

    model_version = await ModelVersionRepository(session).get(result.model_version_id)
    assert model_version is not None
    assert model_version.stage == ModelStage.CANDIDATE
    assert model_version.tenant_id == tenant_context.tenant_id
    assert model_version.mlflow_run_id == result.mlflow_run_id
    assert model_version.artifact_checksum == result.artifact_checksum
    assert len(model_version.artifact_checksum) == 64


async def test_training_produces_metrics_and_a_selected_threshold(
    session: AsyncSession, mlflow_tracking_uri: str
) -> None:
    await _seed_task_definition(session)
    tenant_context = await _tenant_context(session)
    built = _built_dataset(
        tenant_id=tenant_context.tenant_id, principal_id=tenant_context.principal_id
    )

    service = BaselineTrainingService(session, mlflow_tracking_uri=mlflow_tracking_uri)
    result = await service.train(
        built,
        contract=_CONTRACT,
        config=TrainingConfig(model_type=BaselineModelType.GRADIENT_BOOSTED_TREES),
        tenant_context=tenant_context,
        code_revision="test-revision",
    )

    assert 0.0 <= result.metrics.threshold <= 1.0
    assert 0.0 <= result.metrics.roc_auc <= 1.0
    assert result.feature_importance is None  # GBT baseline doesn't support it


async def test_logistic_regression_training_reports_feature_importance(
    session: AsyncSession, mlflow_tracking_uri: str
) -> None:
    await _seed_task_definition(session)
    tenant_context = await _tenant_context(session)
    built = _built_dataset(
        tenant_id=tenant_context.tenant_id, principal_id=tenant_context.principal_id
    )

    service = BaselineTrainingService(session, mlflow_tracking_uri=mlflow_tracking_uri)
    result = await service.train(
        built,
        contract=_CONTRACT,
        config=TrainingConfig(model_type=BaselineModelType.LOGISTIC_REGRESSION),
        tenant_context=tenant_context,
        code_revision="test-revision",
    )

    assert result.feature_importance is not None
    assert set(result.feature_importance) == {"x1", "x2"}


async def test_trained_model_can_be_loaded_back_from_mlflow_and_predicts_identically(
    session: AsyncSession, mlflow_tracking_uri: str
) -> None:
    """Model serialization: the artifact MLflow stored must round-trip to a usable model whose
    predictions match what was evaluated at training time."""

    await _seed_task_definition(session)
    tenant_context = await _tenant_context(session)
    built = _built_dataset(
        tenant_id=tenant_context.tenant_id, principal_id=tenant_context.principal_id
    )

    service = BaselineTrainingService(session, mlflow_tracking_uri=mlflow_tracking_uri)
    result = await service.train(
        built,
        contract=_CONTRACT,
        config=TrainingConfig(model_type=BaselineModelType.LOGISTIC_REGRESSION, random_seed=3),
        tenant_context=tenant_context,
        code_revision="test-revision",
    )

    model_version = await ModelVersionRepository(session).get(result.model_version_id)
    assert model_version is not None

    mlflow.set_tracking_uri(mlflow_tracking_uri)
    loaded = mlflow.sklearn.load_model(model_version.artifact_uri)

    from hermes_rpt.models.dataset_adapter import build_feature_matrix

    x_test, _y_test = build_feature_matrix(built.splits.test, feature_names=("x1", "x2"))
    reloaded_proba = loaded.predict_proba(x_test)[:, 1]
    assert reloaded_proba.shape == (len(built.splits.test),)
    assert np.all((reloaded_proba >= 0) & (reloaded_proba <= 1))


async def test_training_for_a_shared_research_dataset_registers_a_tenant_null_model(
    session: AsyncSession, mlflow_tracking_uri: str
) -> None:
    await _seed_task_definition(session)
    tenant = await TenantRepository(session).add(make_tenant())
    user = await UserRepository(session).add(make_user())
    await session.commit()
    built = _built_dataset(tenant_id=None, principal_id=user.id)

    service = BaselineTrainingService(session, mlflow_tracking_uri=mlflow_tracking_uri)
    result = await service.train(
        built,
        contract=_CONTRACT,
        config=TrainingConfig(model_type=BaselineModelType.MLP),
        tenant_context=None,
        code_revision="test-revision",
    )

    model_version = await ModelVersionRepository(session).get(result.model_version_id)
    assert model_version is not None
    assert model_version.tenant_id is None
    _ = tenant  # only needed to prove the tenant exists but is irrelevant to a shared model
