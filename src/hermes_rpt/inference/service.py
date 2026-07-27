"""Production-style inference orchestration for the delivery-delay-risk task (Phase 13).

Implements the 14-step flow from prompts.txt Prompt 13 exactly, each step delegated to a module
that already exists from an earlier phase — this service is coordination, not new business
logic:

1. Authenticate / 2. Resolve tenant context — `apps.api.deps` (FastAPI dependency chain, before
   this class is ever constructed).
3. Authorise `prediction:execute` — the router's `require_scopes` dependency.
4-6. Resolve active connection / mapping / feature definition — `FeatureExtractionService`
   (Phase 8), reused unchanged.
7. Extract point-in-time features — `FeatureExtractionService.extract` (Phase 8's own
   leakage-prevention machinery, not reimplemented here).
8. Validate model input — the fixed feature-name ordering below plus scikit-learn's own
   shape check inside `predict_proba` (Phase 10's `test_predict_proba_rejects_a_mismatched_
   feature_count` already covers this at the estimator level).
9. Resolve authorised model version — `ModelVersionRepository.get_production_model` (§6 there —
   "shared OR mine, never someone else's", now also gated on `stage == PRODUCTION`).
10. Run prediction — `ModelLoader` + `LoadedModel.predict_proba`.
11. Generate safe explanation — `hermes_rpt.inference.explanation`.
12. Persist prediction lineage — `PredictionRequest` + `PredictionResult` rows.
13. Emit audit event — `hermes_rpt.audit`.
14. Return response — the caller (router) builds the HTTP response from this method's result.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from hermes_rpt.audit.enums import AuditOutcome
from hermes_rpt.audit.service import AuditService
from hermes_rpt.connectors.service import ConnectionLifecycleManager
from hermes_rpt.features.contract import DELIVERY_DELAY_RISK_CONTRACT
from hermes_rpt.features.service import FeatureExtractionService
from hermes_rpt.inference.enums import PredictionStatus
from hermes_rpt.inference.explanation import PredictionExplanation, generate_explanations
from hermes_rpt.inference.model_loading import ModelLoader
from hermes_rpt.inference.models import PredictionRequest, PredictionResult
from hermes_rpt.inference.repository import (
    PredictionRequestRepository,
    PredictionResultRepository,
    PredictionTaskDefinitionRepository,
)
from hermes_rpt.inference.risk import risk_level_for
from hermes_rpt.registry.repository import ModelVersionRepository
from hermes_rpt.tenants.context import TenantContext

_FEATURE_NAMES = tuple(f.name for f in DELIVERY_DELAY_RISK_CONTRACT.features)


class NoProductionModelError(Exception):
    def __init__(self, task_key: str) -> None:
        super().__init__(f"No PRODUCTION model version is available for task {task_key!r}")


class PredictionTaskNotConfiguredError(Exception):
    """Raised when no `PredictionTaskDefinition` row exists for the delivery-delay-risk task —
    a deployment/setup gap (a missing migration/seed step), not something a caller did wrong.
    Mapped to a 503 by the router, same family as `NoProductionModelError`: "this endpoint
    isn't ready yet," never a raw 500 with an internal message."""

    def __init__(self, task_key: str) -> None:
        super().__init__(f"No PredictionTaskDefinition registered for {task_key!r}")


@dataclass(frozen=True, slots=True)
class PredictionResponse:
    prediction_id: uuid.UUID
    business_reference: str
    delay_probability: float
    risk_level: str
    model_version: str
    feature_version: str
    prediction_time: datetime
    explanations: tuple[PredictionExplanation, ...]


class PredictionService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        connection_manager: ConnectionLifecycleManager,
        model_loader: ModelLoader,
    ) -> None:
        self._session = session
        self._extraction = FeatureExtractionService(session, connection_manager=connection_manager)
        self._task_definitions = PredictionTaskDefinitionRepository(session)
        self._requests = PredictionRequestRepository(session)
        self._results = PredictionResultRepository(session)
        self._model_versions = ModelVersionRepository(session)
        self._audit = AuditService(session)
        self._model_loader = model_loader

    async def predict(
        self,
        *,
        tenant_context: TenantContext,
        business_reference: str,
        prediction_time: datetime,
        idempotency_key: str | None,
    ) -> PredictionResponse:
        if idempotency_key:
            cached = await self._cached_response(
                tenant_context=tenant_context, idempotency_key=idempotency_key
            )
            if cached is not None:
                return cached

        task_definition = await self._task_definitions.get_by_task_key(
            DELIVERY_DELAY_RISK_CONTRACT.task_key
        )
        if task_definition is None:
            raise PredictionTaskNotConfiguredError(DELIVERY_DELAY_RISK_CONTRACT.task_key)

        request = await self._requests.add(
            PredictionRequest(
                task_definition_id=task_definition.id,
                business_reference=business_reference,
                prediction_time=prediction_time,
                status=PredictionStatus.PENDING,
                idempotency_key=idempotency_key,
                requested_by_principal_id=tenant_context.principal_id,
                correlation_id=tenant_context.correlation_id,
            ),
            tenant_context=tenant_context,
        )
        await self._session.flush()

        try:
            model_version = await self._model_versions.get_production_model(
                task_definition.id, tenant_context=tenant_context
            )
            if model_version is None:
                raise NoProductionModelError(DELIVERY_DELAY_RISK_CONTRACT.task_key)
            loaded_model = await self._model_loader.load(model_version)

            batch = await self._extraction.extract(
                DELIVERY_DELAY_RISK_CONTRACT,
                tenant_context=tenant_context,
                business_reference=business_reference,
                prediction_time=prediction_time,
            )
            feature_row = [
                float(value) if (value := batch.features.get(name)) is not None else math.nan
                for name in _FEATURE_NAMES
            ]
            probability = loaded_model.predict_proba(feature_row)
            explanations = generate_explanations(
                feature_importance=loaded_model.feature_importance(_FEATURE_NAMES),
                features=batch.features,
            )

            result = await self._results.add(
                PredictionResult(
                    prediction_request_id=request.id,
                    model_version_id=model_version.id,
                    mapping_version_id=batch.lineage.target_mapping_version_id,
                    feature_version=DELIVERY_DELAY_RISK_CONTRACT.version,
                    output={
                        "delay_probability": probability,
                        "risk_level": risk_level_for(probability),
                    },
                    explanations=[e.model_dump() for e in explanations],
                ),
                tenant_context=tenant_context,
            )
            request.status = PredictionStatus.COMPLETED
            await self._session.flush()

            await self._audit.record(
                action="prediction.execute",
                outcome=AuditOutcome.SUCCESS,
                tenant_id=tenant_context.tenant_id,
                principal_id=tenant_context.principal_id,
                principal_type=tenant_context.principal_type,
                resource_type="PredictionRequest",
                resource_id=request.id,
                correlation_id=tenant_context.correlation_id,
                details={
                    "task_key": DELIVERY_DELAY_RISK_CONTRACT.task_key,
                    "model_version_id": str(model_version.id),
                },
            )
        except Exception:
            request.status = PredictionStatus.FAILED
            await self._session.flush()
            await self._audit.record(
                action="prediction.execute",
                outcome=AuditOutcome.ERROR,
                tenant_id=tenant_context.tenant_id,
                principal_id=tenant_context.principal_id,
                principal_type=tenant_context.principal_type,
                resource_type="PredictionRequest",
                resource_id=request.id,
                correlation_id=tenant_context.correlation_id,
            )
            raise

        return PredictionResponse(
            prediction_id=result.id,
            business_reference=business_reference,
            delay_probability=probability,
            risk_level=risk_level_for(probability),
            model_version=model_version.name,
            feature_version=DELIVERY_DELAY_RISK_CONTRACT.version,
            prediction_time=prediction_time,
            explanations=explanations,
        )

    async def _cached_response(
        self, *, tenant_context: TenantContext, idempotency_key: str
    ) -> PredictionResponse | None:
        existing_request = await self._requests.get_by_idempotency_key(
            idempotency_key, tenant_context=tenant_context
        )
        if existing_request is None or existing_request.status != PredictionStatus.COMPLETED:
            return None
        existing_result = await self._results.get_by_request_id(
            existing_request.id, tenant_context=tenant_context
        )
        if existing_result is None:
            return None
        model_version = await self._model_versions.get(existing_result.model_version_id)
        model_version_name = model_version.name if model_version is not None else "unknown"
        return PredictionResponse(
            prediction_id=existing_result.id,
            business_reference=existing_request.business_reference,
            delay_probability=float(existing_result.output["delay_probability"]),
            risk_level=str(existing_result.output["risk_level"]),
            model_version=model_version_name,
            feature_version=existing_result.feature_version,
            prediction_time=existing_request.prediction_time,
            explanations=tuple(PredictionExplanation(**e) for e in existing_result.explanations),
        )
