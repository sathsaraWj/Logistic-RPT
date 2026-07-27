"""Tests for the shared-pretraining consent gate (Phase 12)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from hermes_rpt.models.transformer.pretraining_consent import (
    SHARED_PRETRAINING_CONSENT_TYPE,
    SharedPretrainingConsentError,
    require_shared_pretraining_consent,
)
from hermes_rpt.tenants.context import TenantContext
from hermes_rpt.tenants.enums import ConsentStatus
from hermes_rpt.tenants.models import DataUsageConsent
from hermes_rpt.tenants.repository import (
    DataUsageConsentRepository,
    TenantRepository,
    UserRepository,
)
from tests.factories import make_tenant, make_user


async def _tenant_context(session: AsyncSession) -> TenantContext:
    tenant = await TenantRepository(session).add(make_tenant())
    user = await UserRepository(session).add(make_user())
    await session.commit()
    return TenantContext(tenant_id=tenant.id, principal_id=user.id)


async def test_raises_when_no_consent_record_exists(session: AsyncSession) -> None:
    ctx = await _tenant_context(session)
    with pytest.raises(SharedPretrainingConsentError):
        await require_shared_pretraining_consent(session, tenant_ids=[ctx.tenant_id])


async def test_passes_when_a_granted_consent_record_exists(session: AsyncSession) -> None:
    ctx = await _tenant_context(session)
    await DataUsageConsentRepository(session).add(
        DataUsageConsent(
            consent_type=SHARED_PRETRAINING_CONSENT_TYPE,
            status=ConsentStatus.GRANTED,
            granted_by_user_id=ctx.principal_id,
        ),
        tenant_context=ctx,
    )
    await session.commit()

    await require_shared_pretraining_consent(session, tenant_ids=[ctx.tenant_id])  # must not raise


async def test_raises_when_consent_is_revoked(session: AsyncSession) -> None:
    ctx = await _tenant_context(session)
    await DataUsageConsentRepository(session).add(
        DataUsageConsent(
            consent_type=SHARED_PRETRAINING_CONSENT_TYPE,
            status=ConsentStatus.REVOKED,
            granted_by_user_id=ctx.principal_id,
        ),
        tenant_context=ctx,
    )
    await session.commit()

    with pytest.raises(SharedPretrainingConsentError):
        await require_shared_pretraining_consent(session, tenant_ids=[ctx.tenant_id])


async def test_raises_when_consent_is_for_a_different_purpose(session: AsyncSession) -> None:
    ctx = await _tenant_context(session)
    await DataUsageConsentRepository(session).add(
        DataUsageConsent(
            consent_type="marketing_analytics",  # not shared_pretraining
            status=ConsentStatus.GRANTED,
            granted_by_user_id=ctx.principal_id,
        ),
        tenant_context=ctx,
    )
    await session.commit()

    with pytest.raises(SharedPretrainingConsentError):
        await require_shared_pretraining_consent(session, tenant_ids=[ctx.tenant_id])


async def test_fails_closed_on_the_first_tenant_missing_consent_among_several(
    session: AsyncSession,
) -> None:
    ctx_a = await _tenant_context(session)
    ctx_b = await _tenant_context(session)
    await DataUsageConsentRepository(session).add(
        DataUsageConsent(
            consent_type=SHARED_PRETRAINING_CONSENT_TYPE,
            status=ConsentStatus.GRANTED,
            granted_by_user_id=ctx_a.principal_id,
        ),
        tenant_context=ctx_a,
    )
    await session.commit()

    with pytest.raises(SharedPretrainingConsentError) as excinfo:
        await require_shared_pretraining_consent(
            session, tenant_ids=[ctx_a.tenant_id, ctx_b.tenant_id]
        )
    assert excinfo.value.tenant_id == ctx_b.tenant_id


async def test_unrelated_random_tenant_id_is_rejected(session: AsyncSession) -> None:
    with pytest.raises(SharedPretrainingConsentError):
        await require_shared_pretraining_consent(session, tenant_ids=[uuid.uuid4()])
