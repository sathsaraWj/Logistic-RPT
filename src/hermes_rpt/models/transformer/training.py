"""Hermes-RPT-0.1 training orchestration (Phase 11).

Reuses Phase 9's `DatasetBuildService` for exactly what it already does well — enumerating
labelable rows, computing labels, and splitting temporally (`BuiltDataset.splits`) — but instead
of Phase 8's scalar feature extraction, each split row's `(business_reference, prediction_time,
label)` triple drives `RelationalContextBuilder.build_example()` to fetch the *raw* relational
context this model actually consumes. Two different downstream representations (tabular vs.
relational) built from the same upstream row-selection/labeling/splitting logic, not two
parallel pipelines.

Full-batch training (not mini-batched) — appropriate at this phase's tiny synthetic dataset
sizes (a few hundred rows), not a claim about how a larger production run would train.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import mlflow
import torch
from sqlalchemy.ext.asyncio import AsyncSession

from hermes_rpt.datasets.builder import BuiltDataset, DatasetRow
from hermes_rpt.inference.repository import PredictionTaskDefinitionRepository
from hermes_rpt.models.metrics import compute_metrics, select_threshold
from hermes_rpt.models.training import TrainingResult
from hermes_rpt.models.transformer.context import RelationalContextBuilder, RelationalExample
from hermes_rpt.models.transformer.encoding import encode_batch
from hermes_rpt.models.transformer.model import HermesRPT01, HermesRPTConfig
from hermes_rpt.registry.models import ModelVersion
from hermes_rpt.registry.service import ModelRegistryService
from hermes_rpt.tenants.context import TenantContext


class HermesRPTTrainingService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        context_builder: RelationalContextBuilder,
        mlflow_tracking_uri: str | None = None,
    ) -> None:
        self._session = session
        self._context_builder = context_builder
        self._task_definitions = PredictionTaskDefinitionRepository(session)
        self._registry = ModelRegistryService(session)
        if mlflow_tracking_uri:
            mlflow.set_tracking_uri(mlflow_tracking_uri)

    async def _build_examples(
        self,
        rows: tuple[DatasetRow, ...],
        *,
        tenant_context: TenantContext,
        config: HermesRPTConfig,
    ) -> list[RelationalExample]:
        examples = []
        for row in rows:
            example = await self._context_builder.build_example(
                business_reference=row.business_reference,
                prediction_time=row.prediction_time,
                label=row.label,
                tenant_context=tenant_context,
                max_records_per_relation=config.max_records_per_relation,
            )
            examples.append(example)
        return examples

    async def train(
        self,
        built: BuiltDataset,
        *,
        config: HermesRPTConfig,
        tenant_context: TenantContext,
        code_revision: str,
        task_key: str,
        checkpoint_dir: Path,
        epochs: int = 60,
        learning_rate: float = 1e-3,
        random_seed: int = 42,
        experiment_name: str = "hermes-rpt-0.1",
        pretrained_backbone_state_dict: dict[str, torch.Tensor] | None = None,
    ) -> TrainingResult:
        torch.manual_seed(random_seed)

        train_examples = await self._build_examples(
            built.splits.train, tenant_context=tenant_context, config=config
        )
        val_examples = await self._build_examples(
            built.splits.validation, tenant_context=tenant_context, config=config
        )
        test_examples = await self._build_examples(
            built.splits.test, tenant_context=tenant_context, config=config
        )

        def _encode(examples: list[RelationalExample]) -> Any:
            return encode_batch(
                examples,
                max_records_per_relation=config.max_records_per_relation,
                categorical_vocab_size=config.categorical_vocab_size,
            )

        train_batch = _encode(train_examples)
        val_batch = _encode(val_examples)
        test_batch = _encode(test_examples)
        assert train_batch.labels is not None  # nosec B101 - every split row has a computed label
        train_labels: torch.Tensor = train_batch.labels

        model = HermesRPT01(config)
        if pretrained_backbone_state_dict is not None:
            model.load_pretrained_backbone(pretrained_backbone_state_dict)
        optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate)

        model.train()
        final_train_loss = float("nan")
        for _epoch in range(epochs):
            optimizer.zero_grad()
            logits = model(train_batch)
            loss: torch.Tensor = torch.nn.functional.binary_cross_entropy_with_logits(
                logits, train_labels
            )
            # torch stubs leave this overload of Tensor.backward untyped
            loss.backward()  # type: ignore[no-untyped-call]
            optimizer.step()
            final_train_loss = loss.item()

        model.eval()
        with torch.no_grad():
            val_proba = model.predict_proba(val_batch).numpy()
            test_proba = model.predict_proba(test_batch).numpy()

        assert val_batch.labels is not None and test_batch.labels is not None  # nosec B101
        threshold = select_threshold(
            val_batch.labels.numpy(), val_proba, strategy="max_f1", fixed_value=0.5
        )
        metrics = compute_metrics(test_batch.labels.numpy(), test_proba, threshold=threshold)

        task_definition = await self._task_definitions.get_by_task_key(task_key)
        if task_definition is None:
            raise ValueError(f"No PredictionTaskDefinition registered for {task_key!r}")

        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        checkpoint_path = checkpoint_dir / f"hermes_rpt_{config.name}.pt"
        torch.save({"config": config, "state_dict": model.state_dict()}, checkpoint_path)
        artifact_checksum = hashlib.sha256(checkpoint_path.read_bytes()).hexdigest()

        mlflow.set_experiment(experiment_name)
        with mlflow.start_run() as run:
            mlflow.log_params(
                {
                    "model_size": config.name,
                    "d_model": config.d_model,
                    "n_heads": config.n_heads,
                    "n_layers": config.n_layers,
                    "max_records_per_relation": config.max_records_per_relation,
                    "epochs": epochs,
                    "learning_rate": learning_rate,
                    "random_seed": random_seed,
                    "train_rows": len(train_examples),
                    "validation_rows": len(val_examples),
                    "test_rows": len(test_examples),
                    "pretrained": pretrained_backbone_state_dict is not None,
                }
            )
            mlflow.log_metrics(
                {
                    "final_train_loss": final_train_loss,
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
                    "tenant_id": str(tenant_context.tenant_id),
                    "dataset_checksum": built.manifest.checksum,
                    "dataset_id": str(built.manifest.dataset_id),
                    "code_revision": code_revision,
                    "model_family": "hermes-rpt-0.1",
                }
            )
            mlflow.log_artifact(str(checkpoint_path))
            model_uri = f"runs:/{run.info.run_id}/{checkpoint_path.name}"
            variant = "pretrained" if pretrained_backbone_state_dict is not None else "scratch"
            model_type = f"hermes-rpt-0.1-{config.name}-{variant}"

            model_version = await self._registry.register_candidate(
                ModelVersion(
                    tenant_id=tenant_context.tenant_id,
                    name=f"{task_key}-{model_type}",
                    version_label=run.info.run_id[:12],
                    task_definition_id=task_definition.id,
                    artifact_uri=model_uri,
                    artifact_checksum=artifact_checksum,
                    mlflow_run_id=run.info.run_id,
                    ontology_version=built.manifest.lineage.ontology_version,
                    feature_contract_version=built.manifest.lineage.feature_contract_version,
                )
            )

        return TrainingResult(
            model_type=model_type,
            metrics=metrics,
            feature_importance=None,  # not implemented for the transformer in this phase
            mlflow_run_id=run.info.run_id,
            model_version_id=model_version.id,
            artifact_checksum=artifact_checksum,
        )
