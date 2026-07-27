"""Feature extraction endpoints (Phase 8): a dry-run query plan and a real extraction, both
scoped to a single `FeatureContract` selected by `task_key`. This is the extraction *layer*
only — the actual prediction-serving endpoint (`POST /v1/predictions/...`) is Phase 13's job;
these endpoints exist so the safe extraction pipeline is reachable and auditable on its own,
consistent with the API routers Phases 5-7 already added for their respective layers.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from apps.api.deps import FeatureExtractionServiceDep
from hermes_rpt.auth.dependencies import require_scopes
from hermes_rpt.auth.enums import ScopeName
from hermes_rpt.features.contract import DELIVERY_DELAY_RISK_CONTRACT, FeatureContract
from hermes_rpt.features.lineage import FeatureBatch
from hermes_rpt.features.planner import QueryPlanReport
from hermes_rpt.features.resolver import TargetMappingUnavailableError
from hermes_rpt.features.service import TargetRowNotFoundError
from hermes_rpt.tenants.context import TenantContext

router = APIRouter(prefix="/v1/features", tags=["features"])

_ExecuteScope = Annotated[TenantContext, Depends(require_scopes(ScopeName.PREDICTION_EXECUTE))]

# The platform's registered feature contracts, keyed by task_key — Phase 8 defines exactly one
# (docs/IMPLEMENTATION_PLAN.md §4); later prediction tasks register here the same way.
_CONTRACTS: dict[str, FeatureContract] = {
    DELIVERY_DELAY_RISK_CONTRACT.task_key: DELIVERY_DELAY_RISK_CONTRACT,
}


def _get_contract(task_key: str) -> FeatureContract:
    contract = _CONTRACTS.get(task_key)
    if contract is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Unknown feature task")
    return contract


class ExtractRequest(BaseModel):
    business_reference: str
    prediction_time: datetime


@router.get("/{task_key}/plan", response_model=QueryPlanReport)
async def plan(
    task_key: str, service: FeatureExtractionServiceDep, tenant_context: _ExecuteScope
) -> QueryPlanReport:
    """Dry-run: reports what would be queried for this tenant's current mappings without ever
    opening a customer database connection or resolving a secret."""

    return await service.plan(_get_contract(task_key), tenant_context=tenant_context)


@router.post("/{task_key}/extract", response_model=FeatureBatch)
async def extract(
    task_key: str,
    body: ExtractRequest,
    service: FeatureExtractionServiceDep,
    tenant_context: _ExecuteScope,
) -> FeatureBatch:
    try:
        return await service.extract(
            _get_contract(task_key),
            tenant_context=tenant_context,
            business_reference=body.business_reference,
            prediction_time=body.prediction_time,
        )
    except TargetMappingUnavailableError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except TargetRowNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
