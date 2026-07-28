""" "Precision/recall when labels arrive," and its downstream calibration-drift signal (Phase
15). `OutcomeService.record_outcome` is the only way a `PredictionOutcome` row is ever written —
always tenant-scoped, always audited, and every call recomputes + republishes this tenant's
realized precision/recall/calibration-drift Gauges for that model version so they never go
stale between outcome recordings.

Deliberately a fixed 0.5 probability threshold for the realized-precision/recall decision
boundary, independent of `hermes_rpt.inference.risk.risk_level_for`'s low/medium/high buckets
(those exist for explanation, not for a binary decision) — simple, and consistent regardless of
which model version produced the prediction.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from hermes_rpt.audit.enums import AuditOutcome
from hermes_rpt.audit.service import AuditService
from hermes_rpt.inference.models import PredictionOutcome
from hermes_rpt.inference.repository import PredictionOutcomeRepository, PredictionResultRepository
from hermes_rpt.monitoring import metrics
from hermes_rpt.tenants.context import TenantContext

_DECISION_THRESHOLD = 0.5


class RealizedModelMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    model_version_id: uuid.UUID
    labeled_count: int
    precision: float
    recall: float
    calibration_drift: float


class OutcomeService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._outcomes = PredictionOutcomeRepository(session)
        self._results = PredictionResultRepository(session)
        self._audit = AuditService(session)

    async def record_outcome(
        self,
        prediction_result_id: uuid.UUID,
        *,
        actual_label: bool,
        tenant_context: TenantContext,
    ) -> tuple[PredictionOutcome, RealizedModelMetrics | None]:
        # .require raises TenantMismatchError (-> 404 at the API boundary) for another
        # tenant's prediction — never confirms it exists.
        result = await self._results.require(prediction_result_id, tenant_context=tenant_context)

        existing = await self._outcomes.get_by_prediction_result_id(
            prediction_result_id, tenant_context=tenant_context
        )
        if existing is not None:
            existing.actual_label = actual_label
            outcome = existing
        else:
            outcome = await self._outcomes.add(
                PredictionOutcome(
                    prediction_result_id=prediction_result_id,
                    actual_label=actual_label,
                    recorded_by_principal_id=tenant_context.principal_id,
                ),
                tenant_context=tenant_context,
            )
        await self._session.flush()

        await self._audit.record(
            action="prediction.record_outcome",
            outcome=AuditOutcome.SUCCESS,
            tenant_id=tenant_context.tenant_id,
            principal_id=tenant_context.principal_id,
            resource_type="PredictionResult",
            resource_id=prediction_result_id,
            correlation_id=tenant_context.correlation_id,
            details={"actual_label": actual_label},
        )

        realized = await self.compute_realized_model_metrics(
            result.model_version_id, tenant_context=tenant_context
        )
        return outcome, realized

    async def compute_realized_model_metrics(
        self, model_version_id: uuid.UUID, *, tenant_context: TenantContext
    ) -> RealizedModelMetrics | None:
        """Recomputes and republishes `hermes_rpt.monitoring.metrics.model_precision` /
        `.model_recall` / `.model_calibration_drift` for this tenant + model version from every
        `(PredictionResult, PredictionOutcome)` pair currently on record. Returns `None` (and
        touches no metric) if this tenant has no labeled predictions yet for this model."""

        pairs = await self._outcomes.list_labeled_results_for_model(
            model_version_id, tenant_context=tenant_context
        )
        if not pairs:
            return None

        true_positive = false_positive = false_negative = 0
        probability_sum = 0.0
        actual_positive_count = 0
        for result, outcome in pairs:
            probability = float(result.output.get("delay_probability", 0.0))
            probability_sum += probability
            predicted_positive = probability >= _DECISION_THRESHOLD
            if outcome.actual_label:
                actual_positive_count += 1
            if predicted_positive and outcome.actual_label:
                true_positive += 1
            elif predicted_positive and not outcome.actual_label:
                false_positive += 1
            elif not predicted_positive and outcome.actual_label:
                false_negative += 1

        precision = (
            true_positive / (true_positive + false_positive)
            if (true_positive + false_positive)
            else 0.0
        )
        recall = (
            true_positive / (true_positive + false_negative)
            if (true_positive + false_negative)
            else 0.0
        )
        mean_predicted_probability = probability_sum / len(pairs)
        realized_positive_rate = actual_positive_count / len(pairs)
        calibration_drift = abs(mean_predicted_probability - realized_positive_rate)

        labels = {
            "tenant_id": str(tenant_context.tenant_id),
            "model_version_id": str(model_version_id),
        }
        metrics.model_precision.labels(**labels).set(precision)
        metrics.model_recall.labels(**labels).set(recall)
        metrics.model_calibration_drift.labels(**labels).set(calibration_drift)

        return RealizedModelMetrics(
            model_version_id=model_version_id,
            labeled_count=len(pairs),
            precision=precision,
            recall=recall,
            calibration_drift=calibration_drift,
        )
