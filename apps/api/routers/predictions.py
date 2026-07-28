"""Production-style inference endpoint for the delivery-delay-risk task (Phase 13).

`POST /v1/predictions/delivery-delay` — the request carries only a business identifier and a
prediction timestamp, never a tenant-selected connection string or SQL (prompts.txt Prompt 13);
the trusted tenant comes exclusively from the authenticated `TenantContext`
(`require_scopes(ScopeName.PREDICTION_EXECUTE)`), never from the request body.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict

from apps.api.deps import DbSessionDep, PredictionServiceDep
from hermes_rpt.auth.dependencies import require_scopes
from hermes_rpt.auth.enums import ScopeName
from hermes_rpt.features.resolver import TargetMappingUnavailableError
from hermes_rpt.features.service import TargetRowNotFoundError
from hermes_rpt.inference.model_loading import UnsupportedModelFamilyError
from hermes_rpt.inference.resilience import CircuitOpenError, TimeoutExceededError
from hermes_rpt.inference.service import (
    NoProductionModelError,
    PredictionResponse,
    PredictionTaskNotConfiguredError,
)
from hermes_rpt.registry.artifact_integrity import ArtifactIntegrityError
from hermes_rpt.tenants.context import TenantContext

router = APIRouter(prefix="/v1/predictions", tags=["predictions"])

_ExecuteScope = Annotated[TenantContext, Depends(require_scopes(ScopeName.PREDICTION_EXECUTE))]


class DeliveryDelayPredictionRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    trip_id: str
    prediction_time: datetime


class PredictionExplanationResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    factor: str
    direction: str


class DeliveryDelayPredictionResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    prediction_id: uuid.UUID
    trip_id: str
    delay_probability: float
    risk_level: str
    model_version: str
    feature_version: str
    prediction_time: datetime
    explanations: tuple[PredictionExplanationResponse, ...]

    @classmethod
    def from_service_response(cls, response: PredictionResponse) -> DeliveryDelayPredictionResponse:
        return cls(
            prediction_id=response.prediction_id,
            trip_id=response.business_reference,
            delay_probability=response.delay_probability,
            risk_level=response.risk_level,
            model_version=response.model_version,
            feature_version=response.feature_version,
            prediction_time=response.prediction_time,
            explanations=tuple(
                PredictionExplanationResponse(factor=e.factor, direction=e.direction)
                for e in response.explanations
            ),
        )


@router.post(
    "/delivery-delay",
    response_model=DeliveryDelayPredictionResponse,
    status_code=status.HTTP_200_OK,
)
async def predict_delivery_delay(
    body: DeliveryDelayPredictionRequest,
    session: DbSessionDep,
    service: PredictionServiceDep,
    tenant_context: _ExecuteScope,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> DeliveryDelayPredictionResponse:
    try:
        result = await service.predict(
            tenant_context=tenant_context,
            business_reference=body.trip_id,
            prediction_time=body.prediction_time,
            idempotency_key=idempotency_key,
        )
    except TargetRowNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Trip not found") from exc
    except TargetMappingUnavailableError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except NoProductionModelError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except PredictionTaskNotConfiguredError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except UnsupportedModelFamilyError as exc:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Configured model is not servable"
        ) from exc
    except ArtifactIntegrityError as exc:
        # Fixed message, not str(exc) — the exception carries checksum prefixes that, while not
        # sensitive, are internal detail this endpoint never leaks (same rule as everywhere
        # else in this router). The mismatch itself is the interesting signal; it's logged with
        # full detail server-side by the raise site, not dropped.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail="Configured model failed integrity checks"
        ) from exc
    except CircuitOpenError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail="Model loading temporarily unavailable"
        ) from exc
    except TimeoutExceededError as exc:
        raise HTTPException(
            status.HTTP_504_GATEWAY_TIMEOUT, detail="Model loading timed out"
        ) from exc

    await session.commit()
    return DeliveryDelayPredictionResponse.from_service_response(result)
