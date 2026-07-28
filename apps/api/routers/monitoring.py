"""Monitoring endpoints (Phase 15).

Two genuinely different surfaces, deliberately kept separate rather than merged into one
"metrics" concept — see `hermes_rpt.monitoring.metrics` and `hermes_rpt.monitoring.service` for
the full rationale:

* `GET /metrics` — the raw, process-wide Prometheus scrape endpoint. Cross-tenant *by
  necessity* (a Prometheus server needs every tenant's series to alert correctly — "alerts
  should identify the tenant internally"), so it is operator-only, gated on
  `ScopeName.MONITORING_READ`.

  **Known gap, not silently glossed over**: this platform's auth model has no genuine
  "platform-wide, not tied to any one tenant" credential yet — every issued token (`TokenClaims.
  tenant_id`) belongs to exactly one tenant, service tokens included. `MONITORING_READ` is
  therefore only as safe as *who this scope is issued to* — it must be restricted to a small
  number of trusted internal scraper/operator credentials by issuance discipline, not by a
  structural "this credential has no tenant" check this codebase doesn't have. Revisit in
  Phase 16 (docs/SECURITY_REVIEW.md) if a real platform-operator credential type gets built.
* `GET /v1/monitoring/summary` and `POST /v1/monitoring/predictions/{id}/outcome` — ordinary
  tenant-scoped endpoints, isolated the same way every other tenant-facing read/write in this
  platform is (tenant-scoped repositories), safe for a regular tenant token.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel

from apps.api.deps import DbSessionDep, MonitoringServiceDep, OutcomeServiceDep
from hermes_rpt.auth.dependencies import require_scopes
from hermes_rpt.auth.enums import ScopeName
from hermes_rpt.common.repository import TenantMismatchError
from hermes_rpt.monitoring import metrics
from hermes_rpt.monitoring.outcomes import RealizedModelMetrics
from hermes_rpt.monitoring.service import TenantMonitoringSummary
from hermes_rpt.tenants.context import TenantContext

router = APIRouter(tags=["monitoring"])

_MonitoringReadScope = Annotated[TenantContext, Depends(require_scopes(ScopeName.MONITORING_READ))]
_PredictionExecuteScope = Annotated[
    TenantContext, Depends(require_scopes(ScopeName.PREDICTION_EXECUTE))
]


@router.get("/metrics")
async def scrape_metrics(_tenant_context: _MonitoringReadScope) -> Response:
    return Response(content=generate_latest(metrics.REGISTRY), media_type=CONTENT_TYPE_LATEST)


@router.get("/v1/monitoring/summary", response_model=TenantMonitoringSummary)
async def get_tenant_summary(
    tenant_context: _MonitoringReadScope, service: MonitoringServiceDep
) -> TenantMonitoringSummary:
    return await service.tenant_summary(tenant_context=tenant_context)


class RecordOutcomeRequest(BaseModel):
    actual_label: bool


@router.post(
    "/v1/monitoring/predictions/{prediction_id}/outcome",
    response_model=RealizedModelMetrics | None,
)
async def record_prediction_outcome(
    prediction_id: uuid.UUID,
    body: RecordOutcomeRequest,
    session: DbSessionDep,
    tenant_context: _PredictionExecuteScope,
    service: OutcomeServiceDep,
) -> RealizedModelMetrics | None:
    try:
        _outcome, realized = await service.record_outcome(
            prediction_id, actual_label=body.actual_label, tenant_context=tenant_context
        )
    except TenantMismatchError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Prediction not found") from exc

    await session.commit()
    return realized
