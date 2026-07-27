"""Baseline training orchestration (Phase 10): builds feature matrices from a `BuiltDataset`
(Phase 9), fits one baseline, selects a threshold on the validation split, evaluates on the held-
out test split, logs everything MLflow needs for experiment tracking (params, metrics, tags,
model artifact + signature), and registers a `CANDIDATE` `ModelVersion` in the control-plane
registry — never higher than `CANDIDATE`; see `hermes_rpt.registry.service`.
"""

from __future__ import annotations

import hashlib
import pickle  # nosec B403 - used only to compute a deterministic content checksum, never to load untrusted data
from typing import Any

import mlflow
import mlflow.sklearn
import numpy as np
from mlflow.models import infer_signature
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from hermes_rpt.datasets.builder import BuiltDataset
from hermes_rpt.features.contract import FeatureContract
from hermes_rpt.inference.repository import PredictionTaskDefinitionRepository
from hermes_rpt.models.baselines import build_baseline
from hermes_rpt.models.config import TrainingConfig
from hermes_rpt.models.dataset_adapter import build_feature_matrix
from hermes_rpt.models.interface import BaselineModel
from hermes_rpt.models.metrics import EvaluationMetrics, compute_metrics, select_threshold
from hermes_rpt.registry.models import ModelVersion
from hermes_rpt.registry.service import ModelRegistryService
from hermes_rpt.tenants.context import TenantContext


class TrainingResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    model_type: str
    metrics: EvaluationMetrics
    feature_importance: dict[str, float] | None
    mlflow_run_id: str
    model_version_id: Any  # uuid.UUID — Any to dodge a pydantic/uuid import cycle in this module
    artifact_checksum: str


def _estimator_of(model: BaselineModel) -> Any:
    """The real scikit-learn object behind a baseline, for MLflow's sklearn flavor
    (`mlflow.sklearn.log_model`) to serialize — every concrete baseline in
    `hermes_rpt.models.baselines` exposes exactly one of these two attribute names."""

    estimator = getattr(model, "_pipeline", None) or getattr(model, "_model", None)
    if estimator is None:
        raise TypeError(f"{type(model).__name__} exposes neither _pipeline nor _model")
    return estimator


def _compute_artifact_checksum(estimator: Any) -> str:
    """A deterministic SHA-256 over the fitted estimator's own serialized bytes — Phase 10's
    "model checksum" tracking requirement. Independent of MLflow's own artifact storage (which
    may re-serialize with different pickle protocol options); this is purely a content-identity
    hash, matching the same purpose Phase 5's schema fingerprint and Phase 9's dataset checksum
    serve elsewhere. `pickle` here only ever serializes an estimator we just fitted ourselves —
    never used to *load* anything, so there is no untrusted-deserialization risk (see comment on
    the `pickle` import above).
    """

    return hashlib.sha256(pickle.dumps(estimator)).hexdigest()  # nosec B301


class BaselineTrainingService:
    def __init__(self, session: AsyncSession, *, mlflow_tracking_uri: str | None = None) -> None:
        self._session = session
        self._task_definitions = PredictionTaskDefinitionRepository(session)
        self._registry = ModelRegistryService(session)
        if mlflow_tracking_uri:
            mlflow.set_tracking_uri(mlflow_tracking_uri)

    async def train(
        self,
        built: BuiltDataset,
        *,
        contract: FeatureContract,
        config: TrainingConfig,
        tenant_context: TenantContext | None,
        code_revision: str,
        experiment_name: str = "delivery-delay-risk-baselines",
    ) -> TrainingResult:
        feature_names = tuple(f.name for f in contract.features)
        x_train, y_train = build_feature_matrix(built.splits.train, feature_names=feature_names)
        x_val, y_val = build_feature_matrix(built.splits.validation, feature_names=feature_names)
        x_test, y_test = build_feature_matrix(built.splits.test, feature_names=feature_names)

        np.random.seed(config.random_seed)
        model = build_baseline(config)
        model.fit(x_train, y_train)

        val_proba = model.predict_proba(x_val)
        threshold = select_threshold(
            y_val,
            val_proba,
            strategy=config.threshold_strategy.value,
            fixed_value=config.threshold_value,
        )
        test_proba = model.predict_proba(x_test)
        metrics = compute_metrics(y_test, test_proba, threshold=threshold)
        importance = model.feature_importance(feature_names)

        task_definition = await self._task_definitions.get_by_task_key(contract.task_key)
        if task_definition is None:
            raise ValueError(f"No PredictionTaskDefinition registered for {contract.task_key!r}")

        estimator = _estimator_of(model)
        artifact_checksum = _compute_artifact_checksum(estimator)

        mlflow.set_experiment(experiment_name)
        with mlflow.start_run() as run:
            mlflow.log_params(
                {
                    "model_type": config.model_type.value,
                    "random_seed": config.random_seed,
                    "threshold_strategy": config.threshold_strategy.value,
                    "feature_count": len(feature_names),
                    "train_rows": len(built.splits.train),
                    "validation_rows": len(built.splits.validation),
                    "test_rows": len(built.splits.test),
                    **config.hyperparameters,
                }
            )
            mlflow.log_metrics(
                {
                    "roc_auc": metrics.roc_auc,
                    "pr_auc": metrics.pr_auc,
                    "precision": metrics.precision,
                    "recall": metrics.recall,
                    "f1": metrics.f1,
                    "brier_score": metrics.brier_score,
                    "threshold": metrics.threshold,
                }
            )
            mlflow.set_tags(
                {
                    "tenant_id": str(tenant_context.tenant_id) if tenant_context else "shared",
                    "dataset_checksum": built.manifest.checksum,
                    "dataset_id": str(built.manifest.dataset_id),
                    "mapping_version_ids": ",".join(
                        f"{k}={v}" for k, v in built.manifest.lineage.mapping_version_ids.items()
                    ),
                    "code_revision": code_revision,
                    "feature_contract_version": contract.version,
                    "ontology_version": built.manifest.lineage.ontology_version,
                }
            )
            signature = infer_signature(x_train, model.predict_proba(x_train))
            # MLflow's sklearn flavor uses skops for safe (non-pickle) serialization by default
            # and refuses to load any type it doesn't recognize as safe. The two types below are
            # the only ones any of this platform's three baselines ever actually produce
            # (plain numpy dtype metadata, and MLPClassifier's Adam optimizer state) — both pure
            # data containers with no executable behavior, safe to trust explicitly rather than
            # falling back to pickle-based serialization to work around the check.
            mlflow.sklearn.log_model(
                estimator,
                name="model",
                signature=signature,
                skops_trusted_types=[
                    "numpy.dtype",
                    "sklearn.neural_network._stochastic_optimizers.AdamOptimizer",
                ],
            )
            model_uri = f"runs:/{run.info.run_id}/model"

            model_version = await self._registry.register_candidate(
                ModelVersion(
                    tenant_id=tenant_context.tenant_id if tenant_context else None,
                    name=f"{contract.task_key}-{config.model_type.value}",
                    version_label=run.info.run_id[:12],
                    task_definition_id=task_definition.id,
                    artifact_uri=model_uri,
                    artifact_checksum=artifact_checksum,
                    mlflow_run_id=run.info.run_id,
                    ontology_version=built.manifest.lineage.ontology_version,
                    feature_contract_version=contract.version,
                )
            )

        return TrainingResult(
            model_type=config.model_type.value,
            metrics=metrics,
            feature_importance=importance,
            mlflow_run_id=run.info.run_id,
            model_version_id=model_version.id,
            artifact_checksum=artifact_checksum,
        )
