"""Tests for `hermes_rpt.monitoring.outcomes.OutcomeService` — realized precision/recall/
calibration-drift computed from actually-recorded outcomes, and that outcome recording is
tenant-scoped like everything else. `mapping_version_id`/`model_version_id` below point at
UUIDs with no real row behind them — safe here since SQLite (this fixture's engine) does not
enforce foreign keys, and `OutcomeService` never dereferences those FKs itself.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from hermes_rpt.common.repository import TenantMismatchError
from hermes_rpt.inference.enums import PredictionStatus
from hermes_rpt.inference.models import (
    PredictionRequest,
    PredictionResult,
    PredictionTaskDefinition,
)
from hermes_rpt.monitoring.outcomes import OutcomeService
from hermes_rpt.tenants.context import TenantContext
from hermes_rpt.tenants.repository import TenantRepository, UserRepository
from tests.factories import make_tenant, make_user


async def _make_prediction_result(
    session: AsyncSession, *, tenant_context: TenantContext, probability: float
) -> PredictionResult:
    task = PredictionTaskDefinition(
        task_key=f"task-{uuid.uuid4().hex[:8]}", name="Test Task", feature_contract_version="1"
    )
    session.add(task)
    await session.flush()

    request = PredictionRequest(
        tenant_id=tenant_context.tenant_id,
        task_definition_id=task.id,
        business_reference="TRIP-1",
        prediction_time=datetime(2026, 1, 1, tzinfo=UTC),
        status=PredictionStatus.COMPLETED,
        requested_by_principal_id=tenant_context.principal_id,
    )
    session.add(request)
    await session.flush()

    result = PredictionResult(
        tenant_id=tenant_context.tenant_id,
        prediction_request_id=request.id,
        model_version_id=uuid.uuid4(),
        mapping_version_id=uuid.uuid4(),
        feature_version="1",
        output={"delay_probability": probability, "risk_level": "low"},
        explanations=[],
    )
    session.add(result)
    await session.flush()
    return result


async def _tenant_context(session: AsyncSession) -> TenantContext:
    tenant = await TenantRepository(session).add(make_tenant())
    user = await UserRepository(session).add(make_user())
    await session.commit()
    return TenantContext(tenant_id=tenant.id, principal_id=user.id)


async def test_recording_an_outcome_computes_realized_metrics(session: AsyncSession) -> None:
    ctx = await _tenant_context(session)
    result = await _make_prediction_result(session, tenant_context=ctx, probability=0.8)
    await session.commit()

    service = OutcomeService(session)
    outcome, realized = await service.record_outcome(
        result.id, actual_label=True, tenant_context=ctx
    )
    await session.commit()

    assert outcome.actual_label is True
    assert realized is not None
    assert realized.labeled_count == 1
    assert realized.precision == 1.0
    assert realized.recall == 1.0
    assert realized.calibration_drift == pytest.approx(0.2)  # |0.8 predicted - 1.0 realized|


async def test_recording_an_outcome_twice_updates_rather_than_duplicates(
    session: AsyncSession,
) -> None:
    ctx = await _tenant_context(session)
    result = await _make_prediction_result(session, tenant_context=ctx, probability=0.9)
    await session.commit()

    service = OutcomeService(session)
    await service.record_outcome(result.id, actual_label=True, tenant_context=ctx)
    await session.commit()
    outcome_2, realized_2 = await service.record_outcome(
        result.id, actual_label=False, tenant_context=ctx
    )
    await session.commit()

    assert outcome_2.actual_label is False
    assert realized_2 is not None
    assert realized_2.labeled_count == 1  # still one row, corrected in place


async def test_precision_recall_across_multiple_labeled_predictions(
    session: AsyncSession,
) -> None:
    ctx = await _tenant_context(session)
    service = OutcomeService(session)

    # true positive, false positive, false negative, true negative (threshold 0.5)
    cases = [(0.9, True), (0.7, False), (0.3, True), (0.1, False)]
    model_version_id = uuid.uuid4()
    realized = None
    for probability, actual in cases:
        result = await _make_prediction_result(session, tenant_context=ctx, probability=probability)
        result.model_version_id = model_version_id
        await session.commit()
        _outcome, realized = await service.record_outcome(
            result.id, actual_label=actual, tenant_context=ctx
        )
        await session.commit()

    assert realized is not None
    assert realized.labeled_count == 4
    assert realized.precision == pytest.approx(0.5)  # 1 tp / (1 tp + 1 fp)
    assert realized.recall == pytest.approx(0.5)  # 1 tp / (1 tp + 1 fn)


async def test_recording_an_outcome_for_another_tenants_prediction_is_rejected(
    session: AsyncSession,
) -> None:
    ctx_a = await _tenant_context(session)
    ctx_b = await _tenant_context(session)
    result = await _make_prediction_result(session, tenant_context=ctx_a, probability=0.5)
    await session.commit()

    service = OutcomeService(session)
    with pytest.raises(TenantMismatchError):
        await service.record_outcome(result.id, actual_label=True, tenant_context=ctx_b)
