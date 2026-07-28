"""Adversarial test for "model artifact substitution" (prompts.txt Prompt 16): if a
`ModelVersion.artifact_uri` is repointed at different bytes after registration — a compromised
MLflow artifact store, an operator mistake, or a deliberate swap — `ModelLoader.load()` must
refuse to serve it rather than silently loading whatever now lives at that URI.

Uses `monkeypatch` on `compute_artifact_checksum` (not a real MLflow artifact store) so this
runs fast and without the `ml` dependency group — the checksum computation itself is covered
separately by `tests/model/test_training_service.py` (real MLflow I/O) and
`src/hermes_rpt/registry/artifact_integrity.py`'s own logic is simple enough that a fake here is
a faithful stand-in for "the stored checksum no longer matches what's actually at the URI."
"""

from __future__ import annotations

import uuid

import mlflow.sklearn
import pytest

from hermes_rpt.inference import model_loading
from hermes_rpt.inference.model_loading import ModelLoader
from hermes_rpt.registry.artifact_integrity import ArtifactIntegrityError
from hermes_rpt.registry.enums import ModelStage
from hermes_rpt.registry.models import ModelVersion


def _model_version(*, artifact_checksum: str) -> ModelVersion:
    return ModelVersion(
        id=uuid.uuid4(),
        tenant_id=None,
        name="delivery-delay-risk-logreg",
        version_label="v1",
        task_definition_id=uuid.uuid4(),
        stage=ModelStage.PRODUCTION,
        artifact_uri="runs:/deadbeef/model",
        artifact_checksum=artifact_checksum,
        ontology_version="v1",
        feature_contract_version="v1",
        is_active=True,
    )


async def test_a_substituted_artifact_is_rejected_before_it_would_ever_be_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The artifact at `artifact_uri` now hashes to something other than what was registered —
    simulating a substitution — so `load()` must raise `ArtifactIntegrityError` and must never
    reach `mlflow.sklearn.load_model` at all."""

    model_version = _model_version(artifact_checksum="a" * 64)

    monkeypatch.setattr(
        model_loading,
        "compute_artifact_checksum",
        lambda _uri: "b" * 64,  # a different hash
    )

    def _fail_if_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("mlflow.sklearn.load_model must not run after a checksum mismatch")

    monkeypatch.setattr(mlflow.sklearn, "load_model", _fail_if_called)

    loader = ModelLoader()

    with pytest.raises(ArtifactIntegrityError):
        await loader.load(model_version)

    # A rejected artifact must not be cached either — a later retry (e.g. after the operator
    # fixes the artifact store) should re-verify, not serve a cached failure or a cached load.
    assert model_version.id not in loader._cache  # noqa: SLF001


async def test_a_genuine_artifact_still_loads_when_the_checksum_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_version = _model_version(artifact_checksum="c" * 64)

    monkeypatch.setattr(model_loading, "compute_artifact_checksum", lambda _uri: "c" * 64)
    monkeypatch.setattr(mlflow.sklearn, "load_model", lambda _uri: "fake-fitted-estimator")

    loader = ModelLoader()
    loaded = await loader.load(model_version)

    assert loaded.estimator == "fake-fitted-estimator"
    assert model_version.id in loader._cache  # noqa: SLF001
