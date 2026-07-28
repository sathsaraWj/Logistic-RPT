"""Loads a served model from its registry artifact (Phase 13) — the "resolve authorised model
version" + "run prediction" steps of the inference flow.

Two model families are servable: Phase 10 baselines (`LoadedModel`, an MLflow sklearn-flavor
artifact) and Hermes-RPT-0.1 (`LoadedHermesRPTModel`, a raw `torch.save({"config", "state_dict"})`
checkpoint logged as a plain MLflow artifact — see `hermes_rpt.models.transformer.training`).
`ModelLoader.load()` branches on `ModelVersion.name` to pick the right loading path; any other
name is rejected with `UnsupportedModelFamilyError` rather than silently mis-loaded.

Loaded models are cached in-process, keyed by `ModelVersion.id` — MLflow artifact loading is the
one genuinely "external dependency" in the inference flow, so it is the one thing wrapped in a
timeout + circuit breaker + retry here (Phase 13: "add timeout, retry and circuit-breaker
interfaces"), shared across both families (one `ModelLoader` instance, one circuit breaker —
they compete for the same "is the artifact store healthy" signal).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

# Must be set before any mlflow call that might trigger its telemetry client (e.g. the first
# `mlflow.sklearn.load_model`) — otherwise loading a model artifact pays for an unrelated,
# unnecessary outbound network round-trip inside this timeout-guarded path.
os.environ.setdefault("MLFLOW_DISABLE_TELEMETRY", "true")

import mlflow
import torch

from hermes_rpt.common.logging import get_logger
from hermes_rpt.inference.resilience import (
    CircuitBreaker,
    RetryConfig,
    retry_with_backoff,
    with_timeout,
)
from hermes_rpt.models.transformer.encoding import EncodedBatch
from hermes_rpt.models.transformer.model import HermesRPT01, HermesRPTConfig
from hermes_rpt.registry.artifact_integrity import ArtifactIntegrityError, compute_artifact_checksum
from hermes_rpt.registry.models import ModelVersion

_HERMES_RPT_NAME_MARKER = "hermes-rpt"

logger = get_logger(__name__)


class NoCheckpointArtifactError(Exception):
    def __init__(self, model_version_id: uuid.UUID, local_dir: str) -> None:
        super().__init__(
            f"Model version {model_version_id}'s artifact directory {local_dir!r} contains no "
            "'*.pt' checkpoint file — a hermes-rpt-named ModelVersion with no matching torch "
            "checkpoint artifact is a data-integrity problem, not a transient load failure"
        )


class UnsupportedModelFamilyError(Exception):
    def __init__(self, model_version_name: str) -> None:
        super().__init__(
            f"Model {model_version_name!r} is not a supported serving family for this "
            "endpoint — only Phase 10 baseline models are servable here"
        )


@dataclass(frozen=True, slots=True)
class LoadedModel:
    model_version_id: uuid.UUID
    estimator: object  # a fitted scikit-learn estimator/pipeline (mlflow.sklearn's flavor)

    def predict_proba(self, feature_row: list[float]) -> float:
        proba = self.estimator.predict_proba([feature_row])  # type: ignore[attr-defined]
        return float(proba[0][1])

    def feature_importance(self, feature_names: tuple[str, ...]) -> dict[str, float] | None:
        """Mirrors `hermes_rpt.models.baselines.LogisticRegressionBaseline.feature_importance`
        — this operates on the *raw* pipeline MLflow handed back (not our wrapper class, which
        only exists at training time), so the same coefficient-extraction logic is reimplemented
        here against whatever `Pipeline.named_steps["classifier"]` turns out to be. `None` for
        any estimator kind without a linear `coef_` (gradient-boosted trees, MLP) — "generate
        safe explanation" never fabricates an explanation a model kind can't honestly support.
        """

        classifier = getattr(self.estimator, "named_steps", {}).get("classifier")
        coefficients = getattr(classifier, "coef_", None)
        if coefficients is None:
            return None
        return dict(zip(feature_names, (float(c) for c in coefficients[0]), strict=True))


@dataclass(frozen=True, slots=True)
class LoadedHermesRPTModel:
    model_version_id: uuid.UUID
    model: HermesRPT01
    config: HermesRPTConfig

    def predict_proba(self, batch: EncodedBatch) -> float:
        return float(self.model.predict_proba(batch).item())

    def feature_importance(self, feature_names: tuple[str, ...]) -> dict[str, float] | None:
        """Hermes-RPT-0.1 has no linear coefficient to report a contribution from — always
        `None`, the same "never fabricate an explanation a model can't honestly support" rule
        `LoadedModel.feature_importance` already applies to gradient-boosted trees/MLP."""

        return None


AnyLoadedModel = LoadedModel | LoadedHermesRPTModel


class ModelLoader:
    """One instance per process is expected (see `apps.api.deps`) — the cache and circuit
    breaker are meaningless if a new instance is built per request."""

    def __init__(
        self,
        *,
        mlflow_tracking_uri: str | None = None,
        # A cold `mlflow.sklearn.load_model` for the *first* model version loaded in a process
        # pays a highly variable, sometimes tens-of-seconds cost (scikit-learn/skops type-registry
        # construction, artifact download) on top of the actual deserialization — 10s measured as
        # too tight even after warming the module import at startup (see apps.api.main.lifespan).
        # Per-model-version results are cached, so this cost is paid at most once per model
        # version per process, not on every prediction.
        load_timeout_seconds: float = 60.0,
        retry_config: RetryConfig | None = None,
        circuit_breaker: CircuitBreaker | None = None,
    ) -> None:
        self._mlflow_tracking_uri = mlflow_tracking_uri
        self._load_timeout_seconds = load_timeout_seconds
        self._retry_config = retry_config or RetryConfig()
        self._circuit_breaker = circuit_breaker or CircuitBreaker()
        self._cache: dict[uuid.UUID, AnyLoadedModel] = {}
        self._lock = asyncio.Lock()

    async def _verify_checksum(self, model_version: ModelVersion) -> None:
        # Re-verify the artifact's integrity before trusting it — a Phase 16 security review
        # found nothing ever re-checked `artifact_checksum` after registration, so a tampered
        # `artifact_uri` (repointed at a different model) or a substituted artifact under an
        # unchanged URI would have been served undetected. Flavor-agnostic — used by both
        # families.
        actual_checksum = await asyncio.to_thread(
            compute_artifact_checksum, model_version.artifact_uri
        )
        if actual_checksum != model_version.artifact_checksum:
            logger.error(
                "model_artifact_integrity_check_failed",
                model_version_id=str(model_version.id),
                expected_checksum=model_version.artifact_checksum,
                actual_checksum=actual_checksum,
            )
            raise ArtifactIntegrityError(
                model_version.id,
                expected=model_version.artifact_checksum,
                actual=actual_checksum,
            )

    async def _load_sklearn(self, model_version: ModelVersion) -> LoadedModel:
        if self._mlflow_tracking_uri:
            mlflow.set_tracking_uri(self._mlflow_tracking_uri)
        await self._verify_checksum(model_version)
        estimator = await asyncio.to_thread(mlflow.sklearn.load_model, model_version.artifact_uri)
        return LoadedModel(model_version_id=model_version.id, estimator=estimator)

    async def _load_hermes_rpt(self, model_version: ModelVersion) -> LoadedHermesRPTModel:
        if self._mlflow_tracking_uri:
            mlflow.set_tracking_uri(self._mlflow_tracking_uri)
        await self._verify_checksum(model_version)
        # `download_artifacts` returns a path to the downloaded *file itself* for a single-file
        # artifact (Hermes-RPT's checkpoint, logged via a plain `mlflow.log_artifact()`), not a
        # containing directory — unlike a multi-file artifact (e.g. an sklearn model bundle). The
        # same distinction `compute_artifact_checksum` has to handle explicitly.
        downloaded_path = Path(
            await asyncio.to_thread(
                mlflow.artifacts.download_artifacts, artifact_uri=model_version.artifact_uri
            )
        )
        if downloaded_path.is_file():
            checkpoint_file = downloaded_path if downloaded_path.suffix == ".pt" else None
        else:
            checkpoint_file = next(downloaded_path.glob("*.pt"), None)
        if checkpoint_file is None:
            raise NoCheckpointArtifactError(model_version.id, str(downloaded_path))
        # `weights_only=False`: the checkpoint stores a plain `HermesRPTConfig` dataclass
        # alongside the state dict (`HermesRPTTrainingService.train`, `torch.save({"config":
        # ..., "state_dict": ...})`), which `weights_only=True` can't deserialize — same
        # precedent as `apps/trainer/main.py` and `tests/model/test_transformer_checkpoint.py`.
        # Safe under the same trust boundary: the checksum above already re-verified this exact
        # artifact against what was registered, so this isn't an arbitrary untrusted pickle.
        checkpoint = await asyncio.to_thread(torch.load, checkpoint_file, weights_only=False)
        model = HermesRPT01(checkpoint["config"])
        model.load_state_dict(checkpoint["state_dict"])
        model.eval()
        return LoadedHermesRPTModel(
            model_version_id=model_version.id, model=model, config=checkpoint["config"]
        )

    async def load(self, model_version: ModelVersion) -> AnyLoadedModel:
        is_hermes_rpt = _HERMES_RPT_NAME_MARKER in model_version.name

        async with self._lock:
            cached = self._cache.get(model_version.id)
            if cached is not None:
                return cached

            async def _load() -> AnyLoadedModel:
                if is_hermes_rpt:
                    return await self._load_hermes_rpt(model_version)
                return await self._load_sklearn(model_version)

            async def _load_with_timeout() -> AnyLoadedModel:
                return await with_timeout(_load(), seconds=self._load_timeout_seconds)

            async def _load_with_circuit_breaker() -> AnyLoadedModel:
                return await self._circuit_breaker.call(_load_with_timeout)

            loaded = await retry_with_backoff(_load_with_circuit_breaker, config=self._retry_config)
            self._cache[model_version.id] = loaded
            return loaded
