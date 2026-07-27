"""Pretraining orchestration for Hermes-RPT (Phase 12) — mirrors
`hermes_rpt.models.transformer.training.HermesRPTTrainingService`'s shape (reuse Phase 9's
`DatasetBuildService` for row selection, build `RelationalExample`s via the same
`RelationalContextBuilder`, log to MLflow, register a registry row) but trains the
self-supervised multi-task objective (`hermes_rpt.models.transformer.pretraining`) instead of
the supervised classification head, and produces a backbone-only checkpoint meant to be handed
to `HermesRPTTrainingService.train(..., pretrained_backbone_state_dict=...)` afterward.

"Track pretraining lineage": every run logs the same dataset/mapping/code-revision lineage tags
Phase 10/11 already use, plus which objectives (loss weights) were active — a pretraining run is
exactly as reproducible/traceable as a fine-tuning run, not a special case.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import mlflow
import torch
from sqlalchemy.ext.asyncio import AsyncSession

from hermes_rpt.datasets.builder import BuiltDataset, DatasetRow
from hermes_rpt.models.transformer.context import RelationalContextBuilder, RelationalExample
from hermes_rpt.models.transformer.encoding import encode_batch
from hermes_rpt.models.transformer.model import HermesRPTConfig
from hermes_rpt.models.transformer.pretraining import MaskingConfig, build_pretraining_batch
from hermes_rpt.models.transformer.pretraining_model import (
    HermesRPTForPretraining,
    LossWeights,
    compute_pretraining_loss,
)
from hermes_rpt.tenants.context import TenantContext


class PretrainingResult:
    def __init__(
        self,
        *,
        backbone_state_dict: dict[str, torch.Tensor],
        checkpoint_path: Path,
        checksum: str,
        mlflow_run_id: str,
        final_loss_components: dict[str, float],
    ) -> None:
        self.backbone_state_dict = backbone_state_dict
        self.checkpoint_path = checkpoint_path
        self.checksum = checksum
        self.mlflow_run_id = mlflow_run_id
        self.final_loss_components = final_loss_components


class HermesRPTPretrainingService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        context_builder: RelationalContextBuilder,
        mlflow_tracking_uri: str | None = None,
    ) -> None:
        self._session = session
        self._context_builder = context_builder
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

    async def pretrain(
        self,
        built: BuiltDataset,
        *,
        config: HermesRPTConfig,
        tenant_context: TenantContext,
        code_revision: str,
        checkpoint_dir: Path,
        masking_config: MaskingConfig | None = None,
        loss_weights: LossWeights | None = None,
        epochs: int = 60,
        learning_rate: float = 1e-3,
        random_seed: int = 42,
        experiment_name: str = "hermes-rpt-0.1-pretraining",
    ) -> PretrainingResult:
        """Tenant-isolated by default — pretrains on exactly one tenant's own data, the same
        `TenantContext` boundary every other training path in this platform uses. A shared
        (multi-tenant) pretraining run is a distinct, not-yet-built code path that would call
        `hermes_rpt.models.transformer.pretraining_consent.require_shared_pretraining_consent`
        for every contributing tenant before touching more than one tenant's rows — this method
        never combines tenants, so it never needs to.
        """

        torch.manual_seed(random_seed)
        masking_config = masking_config or MaskingConfig(random_seed=random_seed)
        loss_weights = loss_weights or LossWeights()

        train_examples = await self._build_examples(
            built.splits.train, tenant_context=tenant_context, config=config
        )
        train_batch = encode_batch(
            train_examples,
            max_records_per_relation=config.max_records_per_relation,
            categorical_vocab_size=config.categorical_vocab_size,
        )

        model = HermesRPTForPretraining(config)
        optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate)

        model.train()
        final_components: dict[str, float] = {}
        for epoch in range(epochs):
            masked_batch, targets = build_pretraining_batch(
                train_batch,
                config=MaskingConfig(
                    mask_probability=masking_config.mask_probability,
                    link_corruption_probability=masking_config.link_corruption_probability,
                    temporal_pair_probability=masking_config.temporal_pair_probability,
                    random_seed=masking_config.random_seed + epoch,  # a fresh mask draw per epoch
                ),
            )
            optimizer.zero_grad()
            encoded = model(masked_batch)
            loss = compute_pretraining_loss(model, encoded, targets, weights=loss_weights)
            loss.total.backward()  # type: ignore[no-untyped-call]
            optimizer.step()
            final_components = {
                "total": float(loss.total.item()),
                "numeric": loss.numeric,
                "categorical": loss.categorical,
                "datetime": loss.datetime,
                "link": loss.link,
                "temporal_order": loss.temporal_order,
            }

        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        checkpoint_path = checkpoint_dir / f"hermes_rpt_{config.name}_pretrained_backbone.pt"
        backbone_state_dict = model.backbone_state_dict()
        torch.save({"config": config, "backbone_state_dict": backbone_state_dict}, checkpoint_path)
        checksum = hashlib.sha256(checkpoint_path.read_bytes()).hexdigest()

        mlflow.set_experiment(experiment_name)
        with mlflow.start_run() as run:
            mlflow.log_params(
                {
                    "model_size": config.name,
                    "epochs": epochs,
                    "learning_rate": learning_rate,
                    "random_seed": random_seed,
                    "train_rows": len(train_examples),
                    "mask_probability": masking_config.mask_probability,
                    "link_corruption_probability": masking_config.link_corruption_probability,
                    "temporal_pair_probability": masking_config.temporal_pair_probability,
                    "loss_weight_numeric": loss_weights.numeric,
                    "loss_weight_categorical": loss_weights.categorical,
                    "loss_weight_datetime": loss_weights.datetime,
                    "loss_weight_link": loss_weights.link,
                    "loss_weight_temporal_order": loss_weights.temporal_order,
                }
            )
            mlflow.log_metrics(
                {f"final_{name}_loss": value for name, value in final_components.items()}
            )
            mlflow.set_tags(
                {
                    "tenant_id": str(tenant_context.tenant_id),
                    "dataset_checksum": built.manifest.checksum,
                    "dataset_id": str(built.manifest.dataset_id),
                    "code_revision": code_revision,
                    "model_family": "hermes-rpt-0.1-pretraining",
                    "ontology_version": built.manifest.lineage.ontology_version,
                }
            )
            mlflow.log_artifact(str(checkpoint_path))
            mlflow_run_id = run.info.run_id

        return PretrainingResult(
            backbone_state_dict=backbone_state_dict,
            checkpoint_path=checkpoint_path,
            checksum=checksum,
            mlflow_run_id=mlflow_run_id,
            final_loss_components=final_components,
        )
