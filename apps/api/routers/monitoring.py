"""Monitoring endpoints (Phase 15).

Two genuinely different surfaces, deliberately kept separate rather than merged into one
"metrics" concept — see `hermes_rpt.monitoring.metrics` and `hermes_rpt.monitoring.service` for
the full rationale:

* `GET /metrics` — the raw, process-wide Prometheus scrape endpoint. Cross-tenant *by
  necessity* (a Prometheus server needs every tenant's series to alert correctly — "alerts
  should identify the tenant internally"), so it is operator-only, gated on `ScopeName.
  MONITORING_READ` **and** `require_service` (a Phase 16 hardening: a Phase 15 review flagged
  that scope alone would let any ordinary human tenant-admin token reach every tenant's series
  if it were ever mistakenly granted `monitoring:read`; requiring a service-type principal too
  means at minimum a deliberately-issued, non-human credential is needed, not an accidental
  scope grant on a human's dashboard login).

  **Residual gap, not silently glossed over**: this platform's auth model still has no genuine
  "platform-wide, not tied to any one tenant" credential — every issued token (`TokenClaims.
  tenant_id`), service tokens included, belongs to exactly one tenant. So `/metrics` is safe
  only because of *who* is issued a service token with this scope, not because of a structural
  "this credential has no tenant" check this codebase doesn't have. See docs/SECURITY_REVIEW.md
  if a real platform-operator credential type gets built later.
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
from hermes_rpt.auth.dependencies import require_scopes, require_service
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


def _require_metrics_scrape_access(tenant_context: _MonitoringReadScope) -> TenantContext:
    return require_service(tenant_context)


_MetricsScrapeAccess = Annotated[TenantContext, Depends(_require_metrics_scrape_access)]


@router.get("/metrics")
async def scrape_metrics(_tenant_context: _MetricsScrapeAccess) -> Response:
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
