"""Tenant adapter training orchestration (Phase 14).

Mirrors `hermes_rpt.models.transformer.training.HermesRPTTrainingService` deliberately — same
row-selection/relational-context-building/full-batch-training shape — but trains only a
`HermesRPTAdapter` on top of a frozen, already-trained shared backbone, on exactly one tenant's
own data, and writes the adapter checkpoint under a tenant-specific path: "Checkpoints must be
stored in tenant-specific paths" (Phase 14).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import mlflow
import torch
from sqlalchemy.ext.asyncio import AsyncSession

from hermes_rpt.datasets.builder import BuiltDataset, DatasetRow
from hermes_rpt.models.metrics import EvaluationMetrics, compute_metrics, select_threshold
from hermes_rpt.models.transformer.adapter import (
    AdapterConfig,
    HermesRPTAdapter,
    HermesRPTWithAdapter,
)
from hermes_rpt.models.transformer.context import RelationalContextBuilder, RelationalExample
from hermes_rpt.models.transformer.encoding import EncodedBatch, encode_batch
from hermes_rpt.models.transformer.model import HermesRPTBackbone, HermesRPTConfig
from hermes_rpt.registry.models import ModelVersion, TenantModelAdapter
from hermes_rpt.registry.service import ModelRegistryService
from hermes_rpt.tenants.context import TenantContext


@dataclass(frozen=True, slots=True)
class AdapterTrainingResult:
    metrics: EvaluationMetrics
    mlflow_run_id: str
    tenant_model_adapter_id: object  # uuid.UUID; loosely typed to avoid an unused import churn
    artifact_checksum: str
    checkpoint_path: Path


class TenantAdapterTrainingService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        context_builder: RelationalContextBuilder,
        mlflow_tracking_uri: str | None = None,
    ) -> None:
        self._session = session
        self._context_builder = context_builder
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
        base_model: ModelVersion,
        backbone_state_dict: dict[str, torch.Tensor],
        model_config: HermesRPTConfig,
        adapter_config: AdapterConfig,
        tenant_context: TenantContext,
        checkpoint_dir: Path,
        epochs: int = 60,
        learning_rate: float = 1e-3,
        random_seed: int = 42,
        experiment_name: str = "hermes-rpt-0.1-adapters",
    ) -> AdapterTrainingResult:
        torch.manual_seed(random_seed)

        train_examples = await self._build_examples(
            built.splits.train, tenant_context=tenant_context, config=model_config
        )
        val_examples = await self._build_examples(
            built.splits.validation, tenant_context=tenant_context, config=model_config
        )
        test_examples = await self._build_examples(
            built.splits.test, tenant_context=tenant_context, config=model_config
        )

        def _encode(examples: list[RelationalExample]) -> EncodedBatch:
            return encode_batch(
                examples,
                max_records_per_relation=model_config.max_records_per_relation,
                categorical_vocab_size=model_config.categorical_vocab_size,
            )

        train_batch = _encode(train_examples)
        val_batch = _encode(val_examples)
        test_batch = _encode(test_examples)
        assert train_batch.labels is not None  # nosec B101 - every split row has a computed label
        train_labels: torch.Tensor = train_batch.labels

        backbone = HermesRPTBackbone(model_config)
        backbone.load_state_dict(backbone_state_dict)
        adapter = HermesRPTAdapter(model_config, adapter_config)
        model = HermesRPTWithAdapter(backbone, adapter)

        # Only the adapter's parameters are ever handed to the optimizer — the backbone stays
        # frozen (also enforced structurally inside HermesRPTWithAdapter.forward's no_grad
        # block), so no amount of training here can drift the shared base model's weights.
        optimizer = torch.optim.Adam(adapter.parameters(), lr=learning_rate)

        model.train()
        final_train_loss = float("nan")
        for _epoch in range(epochs):
            optimizer.zero_grad()
            logits = model(train_batch)
            loss: torch.Tensor = torch.nn.functional.binary_cross_entropy_with_logits(
                logits, train_labels
            )
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

        # Tenant-specific checkpoint path — the file itself contains only the adapter's own
        # state dict, never the backbone's.
        tenant_checkpoint_dir = checkpoint_dir / str(tenant_context.tenant_id)
        tenant_checkpoint_dir.mkdir(parents=True, exist_ok=True)
        checkpoint_path = tenant_checkpoint_dir / f"adapter_{model_config.name}.pt"
        torch.save(
            {
                "model_config": model_config,
                "adapter_config": adapter_config,
                "state_dict": adapter.state_dict(),
            },
            checkpoint_path,
        )
        artifact_checksum = hashlib.sha256(checkpoint_path.read_bytes()).hexdigest()

        mlflow.set_experiment(experiment_name)
        with mlflow.start_run() as run:
            mlflow.log_params(
                {
                    "model_size": model_config.name,
                    "bottleneck_dim": adapter_config.bottleneck_dim,
                    "epochs": epochs,
                    "learning_rate": learning_rate,
                    "random_seed": random_seed,
                    "base_model_version_id": str(base_model.id),
                    "train_rows": len(train_examples),
                }
            )
            mlflow.log_metrics(
                {
                    "final_train_loss": final_train_loss,
                    "roc_auc": metrics.roc_auc,
                    "pr_auc": metrics.pr_auc,
                    "f1": metrics.f1,
                }
            )
            mlflow.set_tags(
                {
                    "tenant_id": str(tenant_context.tenant_id),
                    "dataset_checksum": built.manifest.checksum,
                    "model_family": "hermes-rpt-0.1-adapter",
                }
            )
            mlflow.log_artifact(str(checkpoint_path))

            adapter_row = await self._registry.register_adapter_candidate(
                TenantModelAdapter(
                    tenant_id=tenant_context.tenant_id,
                    base_model_version_id=base_model.id,
                    name=f"{base_model.name}-adapter",
                    version_label=run.info.run_id[:12],
                    artifact_uri=str(checkpoint_path),
                    artifact_checksum=artifact_checksum,
                    ontology_version=base_model.ontology_version,
                    feature_contract_version=base_model.feature_contract_version,
                ),
                base_model=base_model,
                tenant_context=tenant_context,
            )

        return AdapterTrainingResult(
            metrics=metrics,
            mlflow_run_id=run.info.run_id,
            tenant_model_adapter_id=adapter_row.id,
            artifact_checksum=artifact_checksum,
            checkpoint_path=checkpoint_path,
        )
