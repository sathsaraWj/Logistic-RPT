"""Feature extraction service (Phase 8): ties the mapping resolver, safe query compiler, cost
guard, normalizer, and lineage recorder together into one tenant-aware entry point.

`extract()` is the only method that touches a customer database; `plan()` (dry-run) never does
— see `hermes_rpt.features.planner`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from hermes_rpt.audit.enums import AuditOutcome
from hermes_rpt.audit.service import AuditService
from hermes_rpt.connectors.models import CustomerDatabaseConnection
from hermes_rpt.connectors.repository import CustomerDatabaseConnectionRepository
from hermes_rpt.connectors.service import ConnectionLifecycleManager
from hermes_rpt.features.compiler import compile_and_run_related_feature, fetch_target_row
from hermes_rpt.features.contract import FeatureContract, FeatureKind
from hermes_rpt.features.derive import apply_derive
from hermes_rpt.features.lineage import FeatureBatch, FeatureLineageRecord, MissingFeatureReason
from hermes_rpt.features.normalizer import normalize
from hermes_rpt.features.planner import QueryPlanReport, build_query_plan
from hermes_rpt.features.resolver import MappingResolver, ResolvedMapping
from hermes_rpt.schemas.models import SchemaSnapshot
from hermes_rpt.schemas.repository import SchemaSnapshotRepository
from hermes_rpt.tenants.context import TenantContext


class TargetRowNotFoundError(Exception):
    pass


class FeatureExtractionService:
    def __init__(
        self, session: AsyncSession, *, connection_manager: ConnectionLifecycleManager
    ) -> None:
        self._session = session
        self._connection_manager = connection_manager
        self._resolver = MappingResolver(session)
        self._connections = CustomerDatabaseConnectionRepository(session)
        self._snapshots = SchemaSnapshotRepository(session)
        self._audit = AuditService(session)

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

    async def _resolve_related_entities(
        self, contract: FeatureContract, *, tenant_context: TenantContext
    ) -> dict[str, ResolvedMapping | None]:
        related_entity_names = {f.related_entity for f in contract.features if f.related_entity}
        return {
            name: await self._resolver.resolve(name, tenant_context=tenant_context)
            for name in related_entity_names
        }

    async def plan(
        self, contract: FeatureContract, *, tenant_context: TenantContext
    ) -> QueryPlanReport:
        target = await self._resolver.resolve_target(
            contract.target_entity, tenant_context=tenant_context
        )
        related = await self._resolve_related_entities(contract, tenant_context=tenant_context)
        return build_query_plan(contract, target=target, related=related)

    async def extract(
        self,
        contract: FeatureContract,
        *,
        tenant_context: TenantContext,
        business_reference: str,
        prediction_time: datetime,
    ) -> FeatureBatch:
        target = await self._resolver.resolve_target(
            contract.target_entity, tenant_context=tenant_context
        )
        target_connection, target_snapshot = await self._connection_and_snapshot_for(
            target, tenant_context=tenant_context
        )
        _, target_engine = await self._connection_manager.get_or_create_engine(
            target_connection.id, tenant_context=tenant_context
        )

        needed_target_fields = {
            field
            for feature in contract.features
            for field in (
                feature.target_field,
                feature.target_field_secondary,
                feature.related_fk_field_on_target,
            )
            if field
        }
        target_row = await fetch_target_row(
            target_engine,
            target.document,
            business_reference=business_reference,
            needed_fields=needed_target_fields,
            schema_allowlist=target_connection.schema_allowlist,
            table_allowlist=target_connection.table_allowlist,
        )
        if target_row is None:
            raise TargetRowNotFoundError(
                f"No {contract.target_entity} row found for "
                f"business_reference={business_reference!r}"
            )

        related_cache = await self._resolve_related_entities(
            contract, tenant_context=tenant_context
        )
        related_mapping_version_ids: dict[str, uuid.UUID] = {}
        missing: list[MissingFeatureReason] = []
        features_out: dict[str, float | int | bool | None] = {}

        for feature in contract.features:
            if feature.kind in (FeatureKind.DIRECT_FIELD, FeatureKind.DERIVED_FROM_TARGET):
                assert feature.target_field is not None  # nosec B101 - validated by FeatureSpec
                raw = target_row.get(feature.target_field)
                if feature.kind == FeatureKind.DERIVED_FROM_TARGET:
                    assert feature.derive is not None  # nosec B101 - validated by FeatureSpec
                    secondary = (
                        target_row.get(feature.target_field_secondary)
                        if feature.target_field_secondary
                        else None
                    )
                    raw = apply_derive(
                        feature.derive, raw, secondary=secondary, prediction_time=prediction_time
                    )
                features_out[feature.name] = normalize(feature, raw)
                if raw is None and feature.required:
                    missing.append(
                        MissingFeatureReason(feature_name=feature.name, reason="No value")
                    )
                continue

            assert feature.related_entity is not None  # nosec B101 - validated by FeatureSpec
            assert feature.related_fk_field_on_target is not None  # nosec B101

            related = related_cache.get(feature.related_entity)
            if related is None or related.schema_mapping.active_version_id is None:
                features_out[feature.name] = normalize(feature, None)
                missing.append(
                    MissingFeatureReason(
                        feature_name=feature.name,
                        reason=f"No active mapping for {feature.related_entity}",
                    )
                )
                continue
            related_mapping_version_ids[feature.related_entity] = (
                related.schema_mapping.active_version_id
            )

            join_value = target_row.get(feature.related_fk_field_on_target)
            if join_value is None:
                features_out[feature.name] = normalize(feature, None)
                missing.append(
                    MissingFeatureReason(
                        feature_name=feature.name, reason="Join field has no value on target row"
                    )
                )
                continue

            related_connection, _ = await self._connection_and_snapshot_for(
                related, tenant_context=tenant_context
            )
            _, related_engine = await self._connection_manager.get_or_create_engine(
                related_connection.id, tenant_context=tenant_context
            )
            raw = await compile_and_run_related_feature(
                related_engine,
                feature,
                related.document,
                join_value=join_value,
                prediction_time=prediction_time,
                schema_allowlist=related_connection.schema_allowlist,
                table_allowlist=related_connection.table_allowlist,
            )
            if feature.derive is not None:
                # RELATED_LATEST_VALUE features may need a post-fetch transform (e.g.
                # vehicle_age_years: fetch Vehicle.acquired_at, then derive an age in years) —
                # the same fixed derive registry DERIVED_FROM_TARGET uses, just applied after
                # the related-entity query instead of the target row lookup.
                raw = apply_derive(feature.derive, raw, prediction_time=prediction_time)
            features_out[feature.name] = normalize(feature, raw)
            if raw is None and feature.required:
                missing.append(MissingFeatureReason(feature_name=feature.name, reason="No value"))

        # resolve_target() only ever returns a mapping with active_version_id set
        assert target.schema_mapping.active_version_id is not None  # nosec B101
        lineage = FeatureLineageRecord(
            tenant_id=tenant_context.tenant_id,
            task_key=contract.task_key,
            feature_contract_version=contract.version,
            target_mapping_version_id=target.schema_mapping.active_version_id,
            schema_snapshot_id=target_snapshot.id,
            related_mapping_version_ids=related_mapping_version_ids,
            extraction_timestamp=datetime.now(UTC),
            missing_features=tuple(missing),
        )
        await self._audit.record(
            action="feature_extraction.extract",
            outcome=AuditOutcome.SUCCESS,
            tenant_id=tenant_context.tenant_id,
            principal_id=tenant_context.principal_id,
            resource_type="PredictionTaskDefinition",
            correlation_id=tenant_context.correlation_id,
            details={
                "task_key": contract.task_key,
                "business_reference": business_reference,
                "missing_feature_count": len(missing),
            },
        )
        return FeatureBatch(
            business_reference=business_reference,
            prediction_time=prediction_time,
            features=features_out,
            lineage=lineage,
        )
