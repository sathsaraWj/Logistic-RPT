"""Model artifact integrity (Phase 16 security review finding): a SHA-256 over the actual
bytes MLflow stored for an artifact, computed by downloading it back — not over an in-memory
object before MLflow (re-)serializes it, which is not guaranteed to match what's later loaded
(the module docstring on the old `hermes_rpt.models.training._compute_artifact_checksum`
admitted as much: "MLflow ... may re-serialize with different pickle protocol options").

Used at two points: `BaselineTrainingService.train()` computes the checksum from the artifact
*after* `mlflow.sklearn.log_model()` returns, so `ModelVersion.artifact_checksum` reflects what
was actually stored; `ModelLoader.load()` recomputes it from `artifact_uri` before trusting the
result and compares against the stored value, so a tampered `artifact_uri` (repointed at a
different model) or a substituted artifact under an unchanged URI is detected rather than
silently served. Closes a real gap: previously nothing ever re-read `artifact_checksum` after
registration at all.
"""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

import mlflow.artifacts


class ArtifactIntegrityError(Exception):
    def __init__(self, model_version_id: uuid.UUID, *, expected: str, actual: str) -> None:
        super().__init__(
            f"Artifact checksum mismatch for model version {model_version_id}: "
            f"expected {expected[:12]}..., got {actual[:12]}... — refusing to serve"
        )


def compute_artifact_checksum(artifact_uri: str) -> str:
    """Downloads the artifact (a no-op copy if it's already local-cached by this process) and
    hashes every file's bytes in a stable (sorted-path) order — deterministic across process
    restarts and across sklearn/skops serialization detail changes, since it hashes what's on
    disk, not an in-memory object's pickle representation.

    `mlflow.artifacts.download_artifacts` returns a path to a *directory* for a multi-file
    artifact (e.g. `mlflow.sklearn.log_model`'s MLmodel bundle) but a path to the *file itself*
    for a single-file artifact logged via a plain `mlflow.log_artifact()` (e.g. Hermes-RPT's
    `torch.save` checkpoint) — `Path.rglob` silently finds nothing when pointed at a file rather
    than a directory, so that case must be handled explicitly rather than falling through to an
    always-empty (and therefore always-"matching", security-defeating) hash."""

    local_path = Path(mlflow.artifacts.download_artifacts(artifact_uri=artifact_uri))
    if local_path.is_file():
        return hashlib.sha256(local_path.read_bytes()).hexdigest()

    hasher = hashlib.sha256()
    files = sorted(p for p in local_path.rglob("*") if p.is_file())
    for path in files:
        hasher.update(str(path.relative_to(local_path)).encode("utf-8"))
        hasher.update(path.read_bytes())
    return hasher.hexdigest()
