"""Hermes-RPT training entry point.

`uv run --group ml python -m apps.trainer.main baselines` (Phase 10) provisions a small
synthetic Tenant-Alpha dataset, trains all three baselines (logistic regression, gradient-
boosted trees, MLP) against it, and prints a comparison report.

`uv run --group ml python -m apps.trainer.main hermes-rpt` (Phase 11) does the same, then also
trains Hermes-RPT-0.1 (Tiny) on the identical dataset/split and folds it into the same
comparison report — "compare the model against the strongest baseline."

`uv run --group ml python -m apps.trainer.main pretrain` (Phase 12) does the same, then also
self-supervised-pretrains a Hermes-RPT-0.1 Tiny backbone and fine-tunes *two* copies on the
identical dataset/split — one starting from the pretrained backbone, one from scratch — folding
every result (baselines, scratch, pretrained) into one comparison report: "compare pretrained
and non-pretrained Tiny models."

`uv run --group ml python -m apps.trainer.main adapt` (Phase 14) trains one shared Hermes-RPT-0.1
Tiny backbone, then a private `HermesRPTAdapter` per synthetic tenant (Alpha, Beta) on top of it,
and compares "shared base alone" vs. "shared base + Alpha's adapter" vs. "shared base + Beta's
adapter" — while also proving, against the real registry/governance layer, that Alpha's
`TenantContext` can neither read nor alias Beta's adapter.
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess  # nosec B404 - only used to read `git rev-parse HEAD` for run lineage, no shell
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from hermes_rpt.common.logging import configure_logging, get_logger
from hermes_rpt.common.settings import get_settings

if TYPE_CHECKING:
    import uuid

    from hermes_rpt.connectors.service import ConnectionLifecycleManager
    from hermes_rpt.datasets.builder import BuiltDataset
    from hermes_rpt.models.metrics import EvaluationMetrics
    from hermes_rpt.models.training import TrainingResult
    from hermes_rpt.tenants.context import TenantContext

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
# See scripts/__init__.py — scripts/ (not part of the packaged wheel) is where this platform
# allows bridging test-only fakes into a runnable demo/training path; apps/ itself never imports
# from tests/ directly.
sys.path.insert(0, str(REPO_ROOT))


def _code_revision() -> str:
    git_command = ["git", "rev-parse", "--short", "HEAD"]  # noqa: S607 - resolved via PATH, fine here
    try:
        result = subprocess.run(  # noqa: S603  # nosec B603 - fixed args, no shell
            git_command, cwd=REPO_ROOT, capture_output=True, text=True, check=True, timeout=5
        )
        return result.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return "unknown"


async def _provision_alpha_dataset(
    session_factory: async_sessionmaker[AsyncSession], *, run_dir: Path
) -> tuple[TenantContext, ConnectionLifecycleManager, BuiltDataset]:
    from scripts.build_synthetic_dataset import provision_and_build_dataset

    from hermes_rpt.features.contract import DELIVERY_DELAY_RISK_CONTRACT
    from hermes_rpt.inference.models import PredictionTaskDefinition

    async with session_factory() as seed_session:
        seed_session.add(
            PredictionTaskDefinition(
                task_key=DELIVERY_DELAY_RISK_CONTRACT.task_key,
                name="Delivery Delay Risk",
                feature_contract_version=DELIVERY_DELAY_RISK_CONTRACT.version,
            )
        )
        await seed_session.commit()

    return await provision_and_build_dataset(
        session_factory, tenant_slug="alpha", seed=9001, data_dir=run_dir
    )


async def _train_baselines(
    session_factory: async_sessionmaker[AsyncSession],
    built: BuiltDataset,
    *,
    tenant_context: TenantContext,
    code_revision: str,
    mlflow_tracking_uri: str,
) -> list[TrainingResult]:
    from hermes_rpt.features.contract import DELIVERY_DELAY_RISK_CONTRACT
    from hermes_rpt.models.config import BaselineModelType, TrainingConfig
    from hermes_rpt.models.training import BaselineTrainingService

    logger = get_logger(__name__)
    results = []
    async with session_factory() as train_session:
        service = BaselineTrainingService(train_session, mlflow_tracking_uri=mlflow_tracking_uri)
        for model_type in BaselineModelType:
            result = await service.train(
                built,
                contract=DELIVERY_DELAY_RISK_CONTRACT,
                config=TrainingConfig(model_type=model_type),
                tenant_context=tenant_context,
                code_revision=code_revision,
            )
            await train_session.commit()
            results.append(result)
            logger.info(
                "baseline_trained",
                model_type=model_type.value,
                roc_auc=result.metrics.roc_auc,
                pr_auc=result.metrics.pr_auc,
                f1=result.metrics.f1,
            )
    return results


async def _run_baselines() -> None:
    from hermes_rpt.common import model_registry  # noqa: F401 - populates Base.metadata
    from hermes_rpt.common.db import Base
    from hermes_rpt.models.comparison import build_comparison_report

    logger = get_logger(__name__)
    code_revision = _code_revision()
    run_dir = REPO_ROOT / "data" / "baseline_training_runs" / code_revision
    run_dir.mkdir(parents=True, exist_ok=True)
    mlflow_tracking_uri = f"sqlite:///{(run_dir / 'mlflow.db').as_posix()}"

    control_plane_engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with control_plane_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(bind=control_plane_engine, expire_on_commit=False)

    tenant_context, _manager, built = await _provision_alpha_dataset(
        session_factory, run_dir=run_dir
    )
    logger.info(
        "baseline_dataset_built",
        row_counts=built.manifest.row_counts,
        quality_passed=built.manifest.quality_report.passed,
    )

    results = await _train_baselines(
        session_factory,
        built,
        tenant_context=tenant_context,
        code_revision=code_revision,
        mlflow_tracking_uri=mlflow_tracking_uri,
    )

    report = build_comparison_report(results)
    print(report.as_markdown())
    print(f"\nSelected baseline: {report.selected_model_type}")
    print(f"MLflow tracking store: {mlflow_tracking_uri}")

    await control_plane_engine.dispose()


async def _run_hermes_rpt(*, promote_if_winner: bool = False) -> None:
    from hermes_rpt.common import model_registry  # noqa: F401 - populates Base.metadata
    from hermes_rpt.common.db import Base
    from hermes_rpt.features.contract import DELIVERY_DELAY_RISK_CONTRACT
    from hermes_rpt.models.comparison import build_comparison_report
    from hermes_rpt.models.transformer.context import RelationalContextBuilder
    from hermes_rpt.models.transformer.model import TINY
    from hermes_rpt.models.transformer.training import HermesRPTTrainingService
    from hermes_rpt.registry.enums import ModelStage
    from hermes_rpt.registry.service import ModelRegistryService

    logger = get_logger(__name__)
    code_revision = _code_revision()
    run_dir = REPO_ROOT / "data" / "hermes_rpt_training_runs" / code_revision
    run_dir.mkdir(parents=True, exist_ok=True)
    mlflow_tracking_uri = f"sqlite:///{(run_dir / 'mlflow.db').as_posix()}"

    control_plane_engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with control_plane_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(bind=control_plane_engine, expire_on_commit=False)

    tenant_context, manager, built = await _provision_alpha_dataset(
        session_factory, run_dir=run_dir
    )
    logger.info(
        "hermes_rpt_dataset_built",
        row_counts=built.manifest.row_counts,
        quality_passed=built.manifest.quality_report.passed,
    )

    results = await _train_baselines(
        session_factory,
        built,
        tenant_context=tenant_context,
        code_revision=code_revision,
        mlflow_tracking_uri=mlflow_tracking_uri,
    )

    # Reuses the `manager` `_provision_alpha_dataset` returned (its pool registry already has
    # this tenant's data engine cached) rather than building a new one — Phase 11's relational
    # transformer needs to keep querying the tenant's customer database for raw per-record
    # context, unlike Phase 10's baselines, which only ever needed Phase 9's already-extracted
    # scalar features.
    async with session_factory() as train_session:
        context_builder = RelationalContextBuilder(train_session, connection_manager=manager)
        service = HermesRPTTrainingService(
            train_session, context_builder=context_builder, mlflow_tracking_uri=mlflow_tracking_uri
        )
        hermes_rpt_result = await service.train(
            built,
            config=TINY,
            tenant_context=tenant_context,
            code_revision=code_revision,
            task_key=DELIVERY_DELAY_RISK_CONTRACT.task_key,
            checkpoint_dir=run_dir / "checkpoints",
        )
        await train_session.commit()
        logger.info(
            "hermes_rpt_trained",
            roc_auc=hermes_rpt_result.metrics.roc_auc,
            pr_auc=hermes_rpt_result.metrics.pr_auc,
            f1=hermes_rpt_result.metrics.f1,
        )

    report = build_comparison_report([*results, hermes_rpt_result])
    print(report.as_markdown())
    print(f"\nSelected model: {report.selected_model_type}")
    print(f"MLflow tracking store: {mlflow_tracking_uri}")
    print(
        "\nNote: do not read a Hermes-RPT-0.1 win here as a claim of production-readiness — "
        "see docs/HERMES_RPT_0_1.md's Limitations section."
    )

    if promote_if_winner:
        if report.selected_model_type == hermes_rpt_result.model_type:
            async with session_factory() as promote_session:
                registry = ModelRegistryService(promote_session)
                await registry.transition_stage(
                    hermes_rpt_result.model_version_id, to_stage=ModelStage.STAGING
                )
                await registry.transition_stage(
                    hermes_rpt_result.model_version_id, to_stage=ModelStage.PRODUCTION
                )
                await promote_session.commit()
            print(
                f"\nPromoted {hermes_rpt_result.model_type!r} "
                f"({hermes_rpt_result.model_version_id}) to PRODUCTION — it was the selected "
                "model in the comparison above."
            )
        else:
            print(
                f"\n--promote-if-winner set, but {report.selected_model_type!r} was selected, "
                f"not {hermes_rpt_result.model_type!r} — leaving Hermes-RPT-0.1 at CANDIDATE."
            )

    await control_plane_engine.dispose()


async def _run_pretrain() -> None:
    from hermes_rpt.common import model_registry  # noqa: F401 - populates Base.metadata
    from hermes_rpt.common.db import Base
    from hermes_rpt.features.contract import DELIVERY_DELAY_RISK_CONTRACT
    from hermes_rpt.models.comparison import build_comparison_report
    from hermes_rpt.models.transformer.context import RelationalContextBuilder
    from hermes_rpt.models.transformer.model import TINY
    from hermes_rpt.models.transformer.pretraining_training import HermesRPTPretrainingService
    from hermes_rpt.models.transformer.training import HermesRPTTrainingService

    logger = get_logger(__name__)
    code_revision = _code_revision()
    run_dir = REPO_ROOT / "data" / "hermes_rpt_pretraining_runs" / code_revision
    run_dir.mkdir(parents=True, exist_ok=True)
    mlflow_tracking_uri = f"sqlite:///{(run_dir / 'mlflow.db').as_posix()}"

    control_plane_engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with control_plane_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(bind=control_plane_engine, expire_on_commit=False)

    tenant_context, manager, built = await _provision_alpha_dataset(
        session_factory, run_dir=run_dir
    )
    logger.info(
        "pretrain_dataset_built",
        row_counts=built.manifest.row_counts,
        quality_passed=built.manifest.quality_report.passed,
    )

    results = await _train_baselines(
        session_factory,
        built,
        tenant_context=tenant_context,
        code_revision=code_revision,
        mlflow_tracking_uri=mlflow_tracking_uri,
    )

    async with session_factory() as pretrain_session:
        context_builder = RelationalContextBuilder(pretrain_session, connection_manager=manager)
        pretraining_service = HermesRPTPretrainingService(
            pretrain_session,
            context_builder=context_builder,
            mlflow_tracking_uri=mlflow_tracking_uri,
        )
        pretrain_result = await pretraining_service.pretrain(
            built,
            config=TINY,
            tenant_context=tenant_context,
            code_revision=code_revision,
            checkpoint_dir=run_dir / "pretrain-checkpoints",
        )
        await pretrain_session.commit()
        logger.info("hermes_rpt_pretrained", **pretrain_result.final_loss_components)

    async with session_factory() as train_session:
        context_builder = RelationalContextBuilder(train_session, connection_manager=manager)
        training_service = HermesRPTTrainingService(
            train_session, context_builder=context_builder, mlflow_tracking_uri=mlflow_tracking_uri
        )
        scratch_result = await training_service.train(
            built,
            config=TINY,
            tenant_context=tenant_context,
            code_revision=code_revision,
            task_key=DELIVERY_DELAY_RISK_CONTRACT.task_key,
            checkpoint_dir=run_dir / "finetune-scratch",
        )
        await train_session.commit()
        logger.info("hermes_rpt_scratch_trained", roc_auc=scratch_result.metrics.roc_auc)

    async with session_factory() as train_session:
        context_builder = RelationalContextBuilder(train_session, connection_manager=manager)
        training_service = HermesRPTTrainingService(
            train_session, context_builder=context_builder, mlflow_tracking_uri=mlflow_tracking_uri
        )
        pretrained_result = await training_service.train(
            built,
            config=TINY,
            tenant_context=tenant_context,
            code_revision=code_revision,
            task_key=DELIVERY_DELAY_RISK_CONTRACT.task_key,
            checkpoint_dir=run_dir / "finetune-pretrained",
            pretrained_backbone_state_dict=pretrain_result.backbone_state_dict,
        )
        await train_session.commit()
        logger.info("hermes_rpt_pretrained_trained", roc_auc=pretrained_result.metrics.roc_auc)

    report = build_comparison_report([*results, scratch_result, pretrained_result])
    print(report.as_markdown())
    print(f"\nSelected model: {report.selected_model_type}")
    print(f"MLflow tracking store: {mlflow_tracking_uri}")
    print(
        "\nNote: with this platform's tiny synthetic dataset, a pretrained-vs-scratch "
        "comparison is a wiring/plumbing demonstration, not evidence pretraining generalizes — "
        "see docs/HERMES_RPT_PRETRAINING.md's Experimental Report section."
    )

    await control_plane_engine.dispose()


async def _run_adapt() -> None:
    """Phase 14's tenant-adaptation experiment: "Shared Hermes-RPT base + tenant-specific private
    adapter." Trains one shared backbone, then a private `HermesRPTAdapter` per tenant (Alpha,
    Beta) on that tenant's own data with the backbone frozen, and compares three servable
    configurations: shared base alone (evaluated on each tenant's own data), shared base +
    Alpha's adapter, shared base + Beta's adapter. Also proves the governance requirement
    "Tenant A cannot load Tenant B's adapter" end to end, not just via a repository-level unit
    test.
    """

    import torch
    from scripts.build_synthetic_dataset import provision_and_build_dataset

    from hermes_rpt.auth.enums import ScopeName
    from hermes_rpt.common import model_registry  # noqa: F401 - populates Base.metadata
    from hermes_rpt.common.db import Base
    from hermes_rpt.common.repository import TenantMismatchError
    from hermes_rpt.features.contract import DELIVERY_DELAY_RISK_CONTRACT
    from hermes_rpt.inference.models import PredictionTaskDefinition
    from hermes_rpt.models.comparison import build_comparison_report
    from hermes_rpt.models.metrics import compute_metrics, select_threshold
    from hermes_rpt.models.training import TrainingResult
    from hermes_rpt.models.transformer.adapter import AdapterConfig
    from hermes_rpt.models.transformer.adapter_training import TenantAdapterTrainingService
    from hermes_rpt.models.transformer.context import RelationalContextBuilder
    from hermes_rpt.models.transformer.encoding import encode_batch
    from hermes_rpt.models.transformer.model import TINY, HermesRPT01
    from hermes_rpt.models.transformer.training import HermesRPTTrainingService
    from hermes_rpt.registry.enums import ModelStage
    from hermes_rpt.registry.repository import ModelVersionRepository, TenantModelAdapterRepository
    from hermes_rpt.registry.service import ModelRegistryService
    from hermes_rpt.tenants.context import TenantContext

    logger = get_logger(__name__)
    code_revision = _code_revision()
    run_dir = REPO_ROOT / "data" / "hermes_rpt_adaptation_runs" / code_revision
    run_dir.mkdir(parents=True, exist_ok=True)
    mlflow_tracking_uri = f"sqlite:///{(run_dir / 'mlflow.db').as_posix()}"

    control_plane_engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with control_plane_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(bind=control_plane_engine, expire_on_commit=False)

    async with session_factory() as seed_session:
        seed_session.add(
            PredictionTaskDefinition(
                task_key=DELIVERY_DELAY_RISK_CONTRACT.task_key,
                name="Delivery Delay Risk",
                feature_contract_version=DELIVERY_DELAY_RISK_CONTRACT.version,
            )
        )
        await seed_session.commit()

    alpha_ctx, alpha_manager, alpha_built = await provision_and_build_dataset(
        session_factory, tenant_slug="alpha", seed=9001, data_dir=run_dir
    )
    beta_ctx, beta_manager, beta_built = await provision_and_build_dataset(
        session_factory, tenant_slug="beta", seed=9002, data_dir=run_dir
    )
    # Adapter training/promotion needs `model:promote` too (Phase 14's approval gate applies
    # uniformly, not only to base-model promotion) — a fresh TenantContext with the scope added,
    # same tenant/principal identity `provision_and_build_dataset` already established.
    alpha_ctx = TenantContext(
        tenant_id=alpha_ctx.tenant_id,
        principal_id=alpha_ctx.principal_id,
        scopes=frozenset({ScopeName.MODEL_PROMOTE.value}),
    )
    beta_ctx = TenantContext(
        tenant_id=beta_ctx.tenant_id,
        principal_id=beta_ctx.principal_id,
        scopes=frozenset({ScopeName.MODEL_PROMOTE.value}),
    )
    logger.info(
        "adapt_datasets_built",
        alpha_rows=alpha_built.manifest.row_counts,
        beta_rows=beta_built.manifest.row_counts,
    )

    # Shared base: trained once. `HermesRPTTrainingService.train()` requires a concrete
    # `TenantContext` (unlike the baseline trainer, which accepts `None` for a shared model), so
    # this trains under Alpha's context — standing in for a shared/consented training pool
    # (Phase 12 already implements the real cross-tenant consent gate for shared pretraining;
    # this experiment isn't re-demonstrating that, just needs *a* shared backbone to adapt from)
    # — then flips `tenant_id` to `None` before promoting, marking it shared for real.
    async with session_factory() as base_session:
        context_builder = RelationalContextBuilder(base_session, connection_manager=alpha_manager)
        base_training_service = HermesRPTTrainingService(
            base_session, context_builder=context_builder, mlflow_tracking_uri=mlflow_tracking_uri
        )
        base_result = await base_training_service.train(
            alpha_built,
            config=TINY,
            tenant_context=alpha_ctx,
            code_revision=code_revision,
            task_key=DELIVERY_DELAY_RISK_CONTRACT.task_key,
            checkpoint_dir=run_dir / "shared-base",
        )
        await base_session.commit()
        logger.info("adapt_shared_base_trained", roc_auc=base_result.metrics.roc_auc)

        registry = ModelRegistryService(base_session)
        base_model = await ModelVersionRepository(base_session).get(base_result.model_version_id)
        assert base_model is not None  # nosec B101 - just registered it above
        base_model.tenant_id = None  # promote from "trained using Alpha's data" to shared
        await registry.transition_stage(base_model.id, to_stage=ModelStage.STAGING)
        await registry.transition_stage(base_model.id, to_stage=ModelStage.PRODUCTION)
        await registry.set_alias(
            task_definition_id=base_model.task_definition_id, model_version_id=base_model.id
        )
        await base_session.commit()
        base_model_id = base_model.id
        base_model_task_definition_id = base_model.task_definition_id

    backbone_checkpoint = run_dir / "shared-base" / f"hermes_rpt_{TINY.name}.pt"
    # weights_only=False: this checkpoint stores a plain HermesRPTConfig dataclass alongside the
    # state dict (same trust-boundary trade-off as Phase 11/12's checkpoints — see
    # docs/HERMES_RPT_0_1.md); safe here specifically because this process wrote the file itself,
    # moments earlier, in this same run.
    checkpoint_payload = torch.load(backbone_checkpoint, weights_only=False)  # nosec B614
    reference_model = HermesRPT01(TINY)
    reference_model.load_state_dict(checkpoint_payload["state_dict"])
    reference_model.eval()
    backbone_state_dict = reference_model.backbone.state_dict()

    async def _train_and_promote_adapter(
        tenant_context: TenantContext, built: BuiltDataset, manager: ConnectionLifecycleManager
    ) -> tuple[uuid.UUID, EvaluationMetrics]:
        async with session_factory() as adapter_session:
            context_builder = RelationalContextBuilder(adapter_session, connection_manager=manager)
            adapter_service = TenantAdapterTrainingService(
                adapter_session,
                context_builder=context_builder,
                mlflow_tracking_uri=mlflow_tracking_uri,
            )
            base_model_for_session = await ModelVersionRepository(adapter_session).get(
                base_model_id
            )
            assert base_model_for_session is not None  # nosec B101
            result = await adapter_service.train(
                built,
                base_model=base_model_for_session,
                backbone_state_dict=backbone_state_dict,
                model_config=TINY,
                adapter_config=AdapterConfig(),
                tenant_context=tenant_context,
                checkpoint_dir=run_dir / "adapters",
            )
            await adapter_session.commit()
            logger.info(
                "adapt_tenant_adapter_trained",
                tenant_id=str(tenant_context.tenant_id),
                roc_auc=result.metrics.roc_auc,
            )

            adapter_id: uuid.UUID = result.tenant_model_adapter_id  # type: ignore[assignment]
            adapter_registry = ModelRegistryService(adapter_session)
            await adapter_registry.transition_adapter_stage(
                adapter_id, to_stage=ModelStage.STAGING, tenant_context=tenant_context
            )
            await adapter_registry.transition_adapter_stage(
                adapter_id, to_stage=ModelStage.PRODUCTION, tenant_context=tenant_context
            )
            await adapter_registry.set_alias(
                task_definition_id=base_model_task_definition_id,
                model_version_id=base_model_id,
                tenant_model_adapter_id=adapter_id,
                tenant_context=tenant_context,
            )
            await adapter_session.commit()
            return adapter_id, result.metrics

    alpha_adapter_id, alpha_adapter_metrics = await _train_and_promote_adapter(
        alpha_ctx, alpha_built, alpha_manager
    )
    beta_adapter_id, beta_adapter_metrics = await _train_and_promote_adapter(
        beta_ctx, beta_built, beta_manager
    )

    # "Prove that adapter lookup is tenant-isolated" — not just at the repository layer (already
    # covered by tests/model/test_registry.py), but through this experiment's own governance
    # calls: Alpha's TenantContext can neither fetch Beta's adapter row nor alias against it.
    async with session_factory() as isolation_session:
        adapters = TenantModelAdapterRepository(isolation_session)
        cross_tenant_fetch = await adapters.get(beta_adapter_id, tenant_context=alpha_ctx)
        assert (  # nosec B101 - a real correctness check for this experiment run, not a test
            cross_tenant_fetch is None
        ), "Tenant isolation violated: Alpha read Beta's adapter"

        isolation_registry = ModelRegistryService(isolation_session)
        tenant_isolation_held = False
        try:
            await isolation_registry.set_alias(
                task_definition_id=base_model_task_definition_id,
                model_version_id=base_model_id,
                tenant_model_adapter_id=beta_adapter_id,
                tenant_context=alpha_ctx,
            )
        except TenantMismatchError:
            tenant_isolation_held = True
        assert (  # nosec B101 - a real correctness check for this experiment run, not a test
            tenant_isolation_held
        ), "Tenant isolation violated: Alpha aliased Beta's adapter"
    logger.info("adapt_tenant_isolation_proven")

    # Evaluate "shared base alone" (no adapter) on each tenant's own test split, for a fair
    # three-way comparison against each tenant's adapted result.
    async def _evaluate_shared_base_alone(
        tenant_context: TenantContext, built: BuiltDataset, manager: ConnectionLifecycleManager
    ) -> EvaluationMetrics:
        async with session_factory() as eval_session:
            context_builder = RelationalContextBuilder(eval_session, connection_manager=manager)
            val_examples = [
                await context_builder.build_example(
                    business_reference=row.business_reference,
                    prediction_time=row.prediction_time,
                    label=row.label,
                    tenant_context=tenant_context,
                    max_records_per_relation=TINY.max_records_per_relation,
                )
                for row in built.splits.validation
            ]
            test_examples = [
                await context_builder.build_example(
                    business_reference=row.business_reference,
                    prediction_time=row.prediction_time,
                    label=row.label,
                    tenant_context=tenant_context,
                    max_records_per_relation=TINY.max_records_per_relation,
                )
                for row in built.splits.test
            ]
        val_batch = encode_batch(
            val_examples,
            max_records_per_relation=TINY.max_records_per_relation,
            categorical_vocab_size=TINY.categorical_vocab_size,
        )
        test_batch = encode_batch(
            test_examples,
            max_records_per_relation=TINY.max_records_per_relation,
            categorical_vocab_size=TINY.categorical_vocab_size,
        )
        assert val_batch.labels is not None and test_batch.labels is not None  # nosec B101
        val_proba = reference_model.predict_proba(val_batch).numpy()
        test_proba = reference_model.predict_proba(test_batch).numpy()
        threshold = select_threshold(
            val_batch.labels.numpy(), val_proba, strategy="max_f1", fixed_value=0.5
        )
        return compute_metrics(test_batch.labels.numpy(), test_proba, threshold=threshold)

    shared_on_alpha = await _evaluate_shared_base_alone(alpha_ctx, alpha_built, alpha_manager)
    shared_on_beta = await _evaluate_shared_base_alone(beta_ctx, beta_built, beta_manager)

    def _result(
        model_type: str, metrics: EvaluationMetrics, adapter_id: uuid.UUID | None
    ) -> TrainingResult:
        return TrainingResult(
            model_type=model_type,
            metrics=metrics,
            feature_importance=None,
            mlflow_run_id=base_result.mlflow_run_id,
            model_version_id=adapter_id if adapter_id is not None else base_model_id,
            artifact_checksum=base_result.artifact_checksum,
        )

    report = build_comparison_report(
        [
            _result("shared-base-only-on-alpha", shared_on_alpha, None),
            _result("shared-base-only-on-beta", shared_on_beta, None),
            _result("shared-base+alpha-adapter", alpha_adapter_metrics, alpha_adapter_id),
            _result("shared-base+beta-adapter", beta_adapter_metrics, beta_adapter_id),
        ]
    )
    print(report.as_markdown())
    print(f"\nMLflow tracking store: {mlflow_tracking_uri}")
    print(
        "\nTenant isolation proven: Alpha's TenantContext could neither read nor alias Beta's "
        "adapter (see adapt_tenant_isolation_proven in the logs above)."
    )
    print(
        "\nNote: as with every other experiment in this project's synthetic-data phases, do not "
        "read these numbers as evidence of real-world adapter benefit — see docs/"
        "MODEL_ADAPTATION.md's Known Gaps section."
    )

    await control_plane_engine.dispose()


def main() -> None:
    settings = get_settings()
    configure_logging(settings)
    logger = get_logger(__name__)

    parser = argparse.ArgumentParser(prog="apps.trainer.main")
    parser.add_argument(
        "task", choices=["baselines", "hermes-rpt", "pretrain", "adapt"], nargs="?", default=None
    )
    parser.add_argument(
        "--promote-if-winner",
        action="store_true",
        help="`hermes-rpt` only: promote hermes-rpt-0.1-tiny-scratch to PRODUCTION if it's the "
        "comparison's selected model, per ADR-0008's falsifiable bar. Never promotes a loser.",
    )
    args = parser.parse_args()

    if args.task == "baselines":
        asyncio.run(_run_baselines())
        return
    if args.task == "hermes-rpt":
        asyncio.run(_run_hermes_rpt(promote_if_winner=args.promote_if_winner))
        return
    if args.task == "pretrain":
        asyncio.run(_run_pretrain())
        return
    if args.task == "adapt":
        asyncio.run(_run_adapt())
        return

    logger.info(
        "trainer_placeholder_start",
        environment=settings.environment,
        note="pass a task, e.g. `python -m apps.trainer.main baselines` (Phase 10), "
        "`hermes-rpt` (Phase 11), `pretrain` (Phase 12), or `adapt` (Phase 14)",
    )


if __name__ == "__main__":
    main()
