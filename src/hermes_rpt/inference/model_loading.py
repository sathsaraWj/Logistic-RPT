"""Loads a served model from its registry artifact (Phase 13) — the "resolve authorised model
version" + "run prediction" steps of the inference flow.

Scoped to Phase 10's baseline models only. Hermes-RPT-0.1 (Phase 11/12) needs a fundamentally
different input — raw relational context via `RelationalContextBuilder`, not the scalar
`dict[str, float | int | bool | None]` a baseline consumes — and is documented throughout
Phases 11/12 as experimental, not production-ready. Attempting to serve a Hermes-RPT
`ModelVersion` through this path fails closed with `UnsupportedModelFamilyError` rather than
silently mis-loading it; wiring a parallel relational-context serving flow for it is explicit
follow-up (see docs/INFERENCE_API.md).

Loaded models are cached in-process, keyed by `ModelVersion.id` — MLflow artifact loading is the
one genuinely "external dependency" in the inference flow, so it is the one thing wrapped in a
timeout + circuit breaker + retry here (Phase 13: "add timeout, retry and circuit-breaker
interfaces").
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

import mlflow

from hermes_rpt.inference.resilience import (
    CircuitBreaker,
    RetryConfig,
    retry_with_backoff,
    with_timeout,
)
from hermes_rpt.registry.models import ModelVersion

_HERMES_RPT_NAME_MARKER = "hermes-rpt"


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


class ModelLoader:
    """One instance per process is expected (see `apps.api.deps`) — the cache and circuit
    breaker are meaningless if a new instance is built per request."""

    def __init__(
        self,
        *,
        mlflow_tracking_uri: str | None = None,
        load_timeout_seconds: float = 10.0,
        retry_config: RetryConfig | None = None,
        circuit_breaker: CircuitBreaker | None = None,
    ) -> None:
        self._mlflow_tracking_uri = mlflow_tracking_uri
        self._load_timeout_seconds = load_timeout_seconds
        self._retry_config = retry_config or RetryConfig()
        self._circuit_breaker = circuit_breaker or CircuitBreaker()
        self._cache: dict[uuid.UUID, LoadedModel] = {}
        self._lock = asyncio.Lock()

    async def load(self, model_version: ModelVersion) -> LoadedModel:
        if _HERMES_RPT_NAME_MARKER in model_version.name:
            raise UnsupportedModelFamilyError(model_version.name)

        async with self._lock:
            cached = self._cache.get(model_version.id)
            if cached is not None:
                return cached

            async def _load() -> LoadedModel:
                if self._mlflow_tracking_uri:
                    mlflow.set_tracking_uri(self._mlflow_tracking_uri)
                estimator = await asyncio.to_thread(
                    mlflow.sklearn.load_model, model_version.artifact_uri
                )
                return LoadedModel(model_version_id=model_version.id, estimator=estimator)

            async def _load_with_timeout() -> LoadedModel:
                return await with_timeout(_load(), seconds=self._load_timeout_seconds)

            async def _load_with_circuit_breaker() -> LoadedModel:
                return await self._circuit_breaker.call(_load_with_timeout)

            loaded = await retry_with_backoff(_load_with_circuit_breaker, config=self._retry_config)
            self._cache[model_version.id] = loaded
            return loaded
