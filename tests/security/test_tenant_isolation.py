"""Negative security tests: prove Tenant A cannot access Tenant B's records.

Phase 2 requirement 12. Every test here follows the same shape — create data under one
tenant's `TenantContext`, then try to read/list/delete it under a *different* tenant's
`TenantContext`, and assert that it fails exactly as if the row didn't exist
(`TenantMismatchError`, or simply absent from a list), never that it leaks data or raises a
different, information-revealing error. This is the direct test evidence for
docs/adr/0003-tenant-isolation-defense-in-depth.md's application-layer control.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from hermes_rpt.audit.enums import AuditOutcome
from hermes_rpt.audit.repository import AuditEventRepository
from hermes_rpt.audit.service import AuditService
from hermes_rpt.common.repository import TenantMismatchError
from hermes_rpt.inference.enums import PredictionStatus
from hermes_rpt.inference.models import PredictionRequest, PredictionTaskDefinition
from hermes_rpt.inference.repository import (
    PredictionRequestRepository,
    PredictionTaskDefinitionRepository,
)
from hermes_rpt.registry.models import ModelVersion
from hermes_rpt.registry.repository import ModelVersionRepository
from hermes_rpt.tenants.context import TenantContext
from hermes_rpt.tenants.models import DataAccessPolicy
from hermes_rpt.tenants.repository import (
    DataAccessPolicyRepository,
    TenantRepository,
    UserRepository,
)
from tests.factories import make_tenant, make_user


async def _two_tenant_contexts(session: AsyncSession) -> tuple[TenantContext, TenantContext]:
    tenants = TenantRepository(session)
    users = UserRepository(session)
    tenant_a = await tenants.add(make_tenant(name="Tenant Alpha", slug="tenant-alpha"))
    tenant_b = await tenants.add(make_tenant(name="Tenant Beta", slug="tenant-beta"))
    user_a = await users.add(make_user(email="alpha@example.com"))
    user_b = await users.add(make_user(email="beta@example.com"))
    return (
        TenantContext(tenant_id=tenant_a.id, principal_id=user_a.id),
        TenantContext(tenant_id=tenant_b.id, principal_id=user_b.id),
    )


async def test_get_across_tenants_returns_none_not_the_row(session: AsyncSession) -> None:
    ctx_a, ctx_b = await _two_tenant_contexts(session)
    repo = DataAccessPolicyRepository(session)
    policy = await repo.add(DataAccessPolicy(name="Alpha policy"), tenant_context=ctx_a)

    assert await repo.get(policy.id, tenant_context=ctx_b) is None
    # And Alpha can still read its own row — this isn't a case where nobody can read it.
    assert (await repo.get(policy.id, tenant_context=ctx_a)) is not None


async def test_require_across_tenants_raises_tenant_mismatch(session: AsyncSession) -> None:
    ctx_a, ctx_b = await _two_tenant_contexts(session)
    repo = DataAccessPolicyRepository(session)
    policy = await repo.add(DataAccessPolicy(name="Alpha policy"), tenant_context=ctx_a)

    with pytest.raises(TenantMismatchError):
        await repo.require(policy.id, tenant_context=ctx_b)


async def test_list_for_tenant_never_includes_another_tenants_rows(session: AsyncSession) -> None:
    ctx_a, ctx_b = await _two_tenant_contexts(session)
    repo = DataAccessPolicyRepository(session)
    await repo.add(DataAccessPolicy(name="Alpha policy 1"), tenant_context=ctx_a)
    await repo.add(DataAccessPolicy(name="Alpha policy 2"), tenant_context=ctx_a)
    await repo.add(DataAccessPolicy(name="Beta policy"), tenant_context=ctx_b)

    alpha_rows = await repo.list_for_tenant(tenant_context=ctx_a)
    beta_rows = await repo.list_for_tenant(tenant_context=ctx_b)

    assert {row.name for row in alpha_rows} == {"Alpha policy 1", "Alpha policy 2"}
    assert {row.name for row in beta_rows} == {"Beta policy"}


async def test_add_rejects_explicitly_mismatched_tenant_id(session: AsyncSession) -> None:
    """Reject mismatched tenant and resource IDs (Phase 2 requirement 6): even if calling code
    explicitly sets tenant_id to someone else's tenant on the entity, the repository refuses
    to create it under a different TenantContext rather than silently honouring either value."""

    ctx_a, ctx_b = await _two_tenant_contexts(session)
    repo = DataAccessPolicyRepository(session)

    forged = DataAccessPolicy(name="Forged", tenant_id=ctx_b.tenant_id)
    with pytest.raises(TenantMismatchError):
        await repo.add(forged, tenant_context=ctx_a)


async def test_delete_across_tenants_is_rejected(session: AsyncSession) -> None:
    ctx_a, ctx_b = await _two_tenant_contexts(session)
    repo = DataAccessPolicyRepository(session)
    policy = await repo.add(DataAccessPolicy(name="Alpha policy"), tenant_context=ctx_a)

    with pytest.raises(TenantMismatchError):
        await repo.delete(policy, tenant_context=ctx_b)

    # Still there — the rejected delete must not have partially applied.
    assert await repo.get(policy.id, tenant_context=ctx_a) is not None


async def test_prediction_request_isolation(session: AsyncSession) -> None:
    ctx_a, ctx_b = await _two_tenant_contexts(session)
    task = await PredictionTaskDefinitionRepository(session).add(
        PredictionTaskDefinition(
            task_key="delivery-delay-risk",
            name="Delivery delay risk",
            feature_contract_version="v1",
        )
    )
    repo = PredictionRequestRepository(session)
    request = await repo.add(
        PredictionRequest(
            task_definition_id=task.id,
            business_reference="TRIP-1",
            prediction_time=datetime.now(UTC),
            status=PredictionStatus.PENDING,
            requested_by_principal_id=ctx_a.principal_id,
        ),
        tenant_context=ctx_a,
    )

    assert await repo.get(request.id, tenant_context=ctx_b) is None
    with pytest.raises(TenantMismatchError):
        await repo.require(request.id, tenant_context=ctx_b)


async def test_model_version_shared_base_visible_to_all_tenants(session: AsyncSession) -> None:
    ctx_a, ctx_b = await _two_tenant_contexts(session)
    task = await PredictionTaskDefinitionRepository(session).add(
        PredictionTaskDefinition(
            task_key="delivery-delay-risk-2",
            name="Delivery delay risk",
            feature_contract_version="v1",
        )
    )
    repo = ModelVersionRepository(session)
    shared = await repo.add(
        ModelVersion(
            tenant_id=None,
            name="hermes-rpt-base",
            version_label="0.1.0",
            task_definition_id=task.id,
            artifact_uri="s3://models/shared/0.1.0",
            artifact_checksum="deadbeef",
            ontology_version="v1",
            feature_contract_version="v1",
        )
    )

    assert await repo.get_available_for_tenant(shared.id, tenant_context=ctx_a) is not None
    assert await repo.get_available_for_tenant(shared.id, tenant_context=ctx_b) is not None


async def test_model_version_private_model_not_visible_to_other_tenant(
    session: AsyncSession,
) -> None:
    ctx_a, ctx_b = await _two_tenant_contexts(session)
    task = await PredictionTaskDefinitionRepository(session).add(
        PredictionTaskDefinition(
            task_key="delivery-delay-risk-3",
            name="Delivery delay risk",
            feature_contract_version="v1",
        )
    )
    repo = ModelVersionRepository(session)
    private_model = await repo.add(
        ModelVersion(
            tenant_id=ctx_a.tenant_id,
            name="alpha-private-model",
            version_label="0.1.0",
            task_definition_id=task.id,
            artifact_uri="s3://models/alpha/0.1.0",
            artifact_checksum="deadbeef",
            ontology_version="v1",
            feature_contract_version="v1",
        )
    )

    assert await repo.get_available_for_tenant(private_model.id, tenant_context=ctx_a) is not None
    assert await repo.get_available_for_tenant(private_model.id, tenant_context=ctx_b) is None
    with pytest.raises(TenantMismatchError):
        await repo.require_available_for_tenant(private_model.id, tenant_context=ctx_b)


async def test_audit_log_isolation_excludes_other_tenant_and_platform_events(
    session: AsyncSession,
) -> None:
    ctx_a, ctx_b = await _two_tenant_contexts(session)
    audit_service = AuditService(session)

    await audit_service.record(
        action="prediction.execute",
        outcome=AuditOutcome.SUCCESS,
        tenant_id=ctx_a.tenant_id,
        principal_id=ctx_a.principal_id,
    )
    await audit_service.record(
        action="prediction.execute",
        outcome=AuditOutcome.SUCCESS,
        tenant_id=ctx_b.tenant_id,
        principal_id=ctx_b.principal_id,
    )
    await audit_service.record(
        action="auth.failure",
        outcome=AuditOutcome.DENIED,
        tenant_id=None,  # platform-level: no tenant could be resolved yet
    )

    events_repo = AuditEventRepository(session)
    alpha_events = await events_repo.list_for_tenant(tenant_context=ctx_a)

    assert len(alpha_events) == 1
    assert alpha_events[0].tenant_id == ctx_a.tenant_id
    assert all(event.tenant_id == ctx_a.tenant_id for event in alpha_events)
