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
    from hermes_rpt.connectors.service import ConnectionLifecycleManager
    from hermes_rpt.datasets.builder import BuiltDataset
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


async def _run_hermes_rpt() -> None:
    from hermes_rpt.common import model_registry  # noqa: F401 - populates Base.metadata
    from hermes_rpt.common.db import Base
    from hermes_rpt.features.contract import DELIVERY_DELAY_RISK_CONTRACT
    from hermes_rpt.models.comparison import build_comparison_report
    from hermes_rpt.models.transformer.context import RelationalContextBuilder
    from hermes_rpt.models.transformer.model import TINY
    from hermes_rpt.models.transformer.training import HermesRPTTrainingService

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


def main() -> None:
    settings = get_settings()
    configure_logging(settings)
    logger = get_logger(__name__)

    parser = argparse.ArgumentParser(prog="apps.trainer.main")
    parser.add_argument(
        "task", choices=["baselines", "hermes-rpt", "pretrain"], nargs="?", default=None
    )
    args = parser.parse_args()

    if args.task == "baselines":
        asyncio.run(_run_baselines())
        return
    if args.task == "hermes-rpt":
        asyncio.run(_run_hermes_rpt())
        return
    if args.task == "pretrain":
        asyncio.run(_run_pretrain())
        return

    logger.info(
        "trainer_placeholder_start",
        environment=settings.environment,
        note="pass a task, e.g. `python -m apps.trainer.main baselines` (Phase 10), "
        "`hermes-rpt` (Phase 11), or `pretrain` (Phase 12)",
    )


if __name__ == "__main__":
    main()
