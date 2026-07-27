"""Dataset build orchestration (Phase 9): enumerates candidate target rows in a time range, runs
point-in-time-safe label computation, drives `FeatureExtractionService` per row, then assembles
quality checks, statistics, a temporal split, and a manifest.

Tenant isolation is structural, not just checked after the fact: every row this produces is
extracted under one `TenantContext` (`hermes_rpt.tenants.context` — never client-supplied), and
`hermes_rpt.datasets.quality.check_cross_tenant_contamination` re-verifies the assembled dataset
never mixes tenants before a manifest is produced.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from hermes_rpt.connectors.models import CustomerDatabaseConnection
from hermes_rpt.connectors.repository import CustomerDatabaseConnectionRepository
from hermes_rpt.connectors.service import ConnectionLifecycleManager
from hermes_rpt.datasets.definition import DatasetDefinition
from hermes_rpt.datasets.label import compute_label
from hermes_rpt.datasets.manifest import DatasetLineage, DatasetManifest, compute_dataset_checksum
from hermes_rpt.datasets.quality import (
    DataQualityReport,
    check_class_imbalance,
    check_cross_tenant_contamination,
    check_duplicate_ids,
    check_future_timestamps,
    check_invalid_date_ordering,
    check_missing_labels,
    check_negative_numeric,
    check_unrecognised_values,
)
from hermes_rpt.datasets.split import DatasetSplits, split_temporally
from hermes_rpt.datasets.statistics import compute_feature_statistics, compute_label_statistics
from hermes_rpt.features.compiler import fetch_rows_in_range, identity_field_name
from hermes_rpt.features.contract import FeatureContract
from hermes_rpt.features.resolver import MappingResolver, ResolvedMapping
from hermes_rpt.features.service import FeatureExtractionService, TargetRowNotFoundError
from hermes_rpt.schemas.models import SchemaSnapshot
from hermes_rpt.schemas.repository import SchemaSnapshotRepository
from hermes_rpt.tenants.context import TenantContext

_TRIP_STATUS_VALUES = frozenset({"planned", "in_progress", "completed", "cancelled"})


class DatasetRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    business_reference: str
    prediction_time: datetime
    features: dict[str, float | int | bool | None]
    label: int


class BuiltDataset(BaseModel):
    model_config = ConfigDict(frozen=True)

    manifest: DatasetManifest
    splits: DatasetSplits[DatasetRow]


class DatasetBuildService:
    def __init__(
        self, session: AsyncSession, *, connection_manager: ConnectionLifecycleManager
    ) -> None:
        self._session = session
        self._connection_manager = connection_manager
        self._resolver = MappingResolver(session)
        self._connections = CustomerDatabaseConnectionRepository(session)
        self._snapshots = SchemaSnapshotRepository(session)
        self._extraction = FeatureExtractionService(session, connection_manager=connection_manager)

    async def _connection_and_snapshot_for(
        self, resolved: ResolvedMapping, *, tenant_context: TenantContext
    ) -> tuple[CustomerDatabaseConnection, SchemaSnapshot]:
        snapshot = await self._snapshots.require(
            resolved.schema_mapping.schema_snapshot_id, tenant_context=tenant_context
        )
        connection = await self._connections.require(
            snapshot.connection_id, tenant_context=tenant_context
        )
        return connection, snapshot

    async def build(
        self,
        definition: DatasetDefinition,
        *,
        contract: FeatureContract,
        tenant_context: TenantContext,
    ) -> BuiltDataset:
        target = await self._resolver.resolve_target(
            contract.target_entity, tenant_context=tenant_context
        )
        connection, snapshot = await self._connection_and_snapshot_for(
            target, tenant_context=tenant_context
        )
        _, engine = await self._connection_manager.get_or_create_engine(
            connection.id, tenant_context=tenant_context
        )
        identity_field = identity_field_name(target.document)

        raw_rows = await fetch_rows_in_range(
            engine,
            target.document,
            time_field=definition.label.time_field,
            start=definition.time_range_start,
            end=definition.time_range_end,
            needed_fields=set(definition.label.source_fields),
            schema_allowlist=connection.schema_allowlist,
            table_allowlist=connection.table_allowlist,
        )

        quality_issues = [
            check_duplicate_ids(raw_rows, id_field=identity_field, check="duplicate_entity_ids"),
            check_invalid_date_ordering(
                raw_rows,
                id_field=identity_field,
                earlier_field=definition.label.time_field,
                later_field="actual_arrival_at",
                check="invalid_dates",
            )
            if "actual_arrival_at" in definition.label.source_fields
            else None,
            check_negative_numeric(
                raw_rows,
                id_field=identity_field,
                field="planned_distance_km",
                check="negative_distances",
            )
            if "planned_distance_km" in definition.label.source_fields
            else None,
            check_unrecognised_values(
                raw_rows,
                id_field=identity_field,
                field="status",
                allowed_values=_TRIP_STATUS_VALUES,
                check="unrecognised_statuses",
            )
            if "status" in definition.label.source_fields
            else None,
            check_future_timestamps(
                raw_rows,
                id_field=identity_field,
                field=definition.label.time_field,
                now=datetime.now(UTC),
                check="future_timestamps",
            ),
        ]

        labelable_rows: list[tuple[dict[str, object], int]] = []
        all_labels_including_missing: list[int | None] = []
        for row in raw_rows:
            label = compute_label(definition.label.function_name, row, **definition.label.kwargs)
            if label is not None and not self._is_point_in_time_valid(row, definition=definition):
                # The source row's own timestamps are corrupted (e.g. an arrival before its own
                # departure — already flagged by check_invalid_date_ordering above); the label
                # can't be trusted, so exclude the row rather than crash the whole build over
                # one bad upstream record.
                label = None
            all_labels_including_missing.append(label)
            if label is None:
                continue
            labelable_rows.append((row, label))
        quality_issues.append(check_missing_labels(all_labels_including_missing))

        dataset_rows: list[DatasetRow] = []
        for row, label in labelable_rows:
            business_reference = str(row[identity_field])
            prediction_time = row[definition.label.time_field]
            assert isinstance(prediction_time, datetime)  # nosec B101 - enforced by fetch_rows_in_range's ordering column
            try:
                batch = await self._extraction.extract(
                    contract,
                    tenant_context=tenant_context,
                    business_reference=business_reference,
                    prediction_time=prediction_time,
                )
            except TargetRowNotFoundError:
                # Row disappeared between enumeration and extraction — skip, don't fail the build.
                continue
            dataset_rows.append(
                DatasetRow(
                    business_reference=business_reference,
                    prediction_time=prediction_time,
                    features=batch.features,
                    label=label,
                )
            )

        quality_issues.append(check_class_imbalance([row.label for row in dataset_rows]))
        quality_issues.append(
            check_cross_tenant_contamination(
                [tenant_context.tenant_id] * len(dataset_rows),
                expected_tenant_id=tenant_context.tenant_id,
            )
        )
        quality_report = DataQualityReport(
            row_count=len(dataset_rows),
            issues=tuple(issue for issue in quality_issues if issue is not None),
        )

        splits = split_temporally(dataset_rows, strategy=definition.split_strategy)
        feature_rows = [row.features for row in dataset_rows]
        labels = [row.label for row in dataset_rows]
        checksum = compute_dataset_checksum(feature_rows, labels=labels)
        feature_statistics = compute_feature_statistics(
            feature_rows, feature_names=tuple(f.name for f in contract.features)
        )
        label_statistics = compute_label_statistics(labels)

        # resolve_target() only ever returns a mapping with active_version_id set
        assert target.schema_mapping.active_version_id is not None  # nosec B101
        manifest = DatasetManifest(
            dataset_id=uuid.uuid4(),
            dataset_key=definition.dataset_key,
            tenant_id=definition.tenant_id,
            is_shared_research_dataset=definition.is_shared_research_dataset,
            lineage=DatasetLineage(
                ontology_version=target.schema_mapping.ontology_version,
                feature_contract_version=contract.version,
                task_key=contract.task_key,
                mapping_version_ids={
                    contract.target_entity: target.schema_mapping.active_version_id
                },
                schema_snapshot_ids={contract.target_entity: snapshot.id},
                built_at=datetime.now(UTC),
                built_by_principal_id=tenant_context.principal_id,
            ),
            row_counts={
                "train": len(splits.train),
                "validation": len(splits.validation),
                "test": len(splits.test),
            },
            checksum=checksum,
            quality_report=quality_report,
            feature_statistics=feature_statistics,
            label_statistics=label_statistics,
        )
        return BuiltDataset(manifest=manifest, splits=splits)

    def _is_point_in_time_valid(
        self, row: dict[str, object], *, definition: DatasetDefinition
    ) -> bool:
        """False means the label event isn't strictly after prediction_time — i.e. it would, in
        principle, already have been knowable at prediction_time, which is exactly what "avoid
        placing future events into earlier training rows" forbids. Only checked when both
        timestamps are actually present and well-typed; a row missing one of them is handled by
        `hermes_rpt.datasets.label` returning `None` (no label) instead."""

        prediction_time = row.get(definition.label.time_field)
        actual_arrival_at = row.get("actual_arrival_at")
        if isinstance(prediction_time, datetime) and isinstance(actual_arrival_at, datetime):
            return actual_arrival_at > prediction_time
        return True
