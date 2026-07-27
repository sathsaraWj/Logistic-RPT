"""Feature lineage and batch format (Phase 8).

`FeatureLineageRecord` is exactly the set of things Phase 8 requires be recorded for every
extraction: "tenant, mapping version, schema snapshot, feature version and extraction
timestamp" — plus which features actually came back missing and why, since "do not assume all
customers have every feature" only means something if callers can tell *which* features were
skipped for *this* tenant.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class MissingFeatureReason(BaseModel):
    model_config = ConfigDict(frozen=True)

    feature_name: str
    reason: str


class FeatureLineageRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    tenant_id: uuid.UUID
    task_key: str
    feature_contract_version: str
    target_mapping_version_id: uuid.UUID
    schema_snapshot_id: uuid.UUID
    related_mapping_version_ids: dict[str, uuid.UUID]
    extraction_timestamp: datetime
    missing_features: tuple[MissingFeatureReason, ...] = ()


class FeatureBatch(BaseModel):
    model_config = ConfigDict(frozen=True)

    business_reference: str
    prediction_time: datetime
    features: dict[str, float | int | bool | None]
    lineage: FeatureLineageRecord
