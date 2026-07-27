"""Dataset manifest and lineage (Phase 9). The manifest is the one artifact a dataset build
produces that's safe to keep around, share, or log — "manifests may contain metadata but not
sensitive raw values." It never embeds a feature value, a label, or a business identifier
(those live only in the built dataset's row files, themselves stored outside the repository —
see docs/DATASET_BUILDING.md).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from hermes_rpt.datasets.quality import DataQualityReport
from hermes_rpt.datasets.statistics import FeatureStatistic, LabelStatistics


class DatasetLineage(BaseModel):
    """What produced this dataset — "track ontology, mapping, feature and schema versions,"
    pinned precisely enough that a dataset can be explained or rebuilt later even after the
    tenant's mappings have since changed."""

    model_config = ConfigDict(frozen=True)

    ontology_version: str
    feature_contract_version: str
    task_key: str
    # entity name -> the exact MappingVersion id used for every row in this dataset. A tenant
    # activating a new mapping version mid-build never changes this after the fact — Phase 7's
    # immutable-after-activation guarantee is what makes pinning a version id meaningful.
    mapping_version_ids: dict[str, uuid.UUID]
    schema_snapshot_ids: dict[str, uuid.UUID]
    built_at: datetime
    built_by_principal_id: uuid.UUID


class DatasetManifest(BaseModel):
    model_config = ConfigDict(frozen=True)

    dataset_id: uuid.UUID
    dataset_key: str
    tenant_id: uuid.UUID | None
    is_shared_research_dataset: bool
    lineage: DatasetLineage
    row_counts: dict[str, int]  # {"train": ..., "validation": ..., "test": ...}
    checksum: str
    quality_report: DataQualityReport
    feature_statistics: tuple[FeatureStatistic, ...]
    label_statistics: LabelStatistics


def compute_dataset_checksum(
    rows: list[dict[str, float | int | bool | None]], *, labels: list[int]
) -> str:
    """A deterministic SHA-256 over the dataset's actual content (feature values + labels, in
    row order) — reproducibility/integrity, the same purpose Phase 5's schema fingerprint
    serves for discovered metadata. Two builds of the same definition against unchanged source
    data produce the same checksum; any row, value, or ordering change produces a different one.
    """

    hasher = hashlib.sha256()
    for row, label in zip(rows, labels, strict=True):
        canonical = json.dumps(row, sort_keys=True, default=str)
        hasher.update(canonical.encode("utf-8"))
        hasher.update(f"|label={label}\n".encode())
    return hasher.hexdigest()
