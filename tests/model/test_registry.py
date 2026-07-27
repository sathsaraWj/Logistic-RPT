"""Tests for the model registry lifecycle and tenant model access isolation (Phase 10)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from hermes_rpt.common.repository import TenantMismatchError
from hermes_rpt.inference.models import PredictionTaskDefinition
from hermes_rpt.registry.enums import ModelStage
from hermes_rpt.registry.models import ModelVersion, TenantModelAdapter
from hermes_rpt.registry.repository import ModelVersionRepository, TenantModelAdapterRepository
from hermes_rpt.registry.service import InvalidModelStageTransitionError, ModelRegistryService
from hermes_rpt.tenants.context import TenantContext
from hermes_rpt.tenants.repository import TenantRepository
from tests.factories import make_tenant


async def _seed_task_definition(session: AsyncSession) -> PredictionTaskDefinition:
    task = PredictionTaskDefinition(
        task_key="delivery-delay-risk", name="Delivery Delay Risk", feature_contract_version="1"
    )
    session.add(task)
    await session.flush()
    return task


def _model_version(*, tenant_id: uuid.UUID | None, task_definition_id: uuid.UUID) -> ModelVersion:
    return ModelVersion(
        tenant_id=tenant_id,
        name="delivery-delay-risk-logistic_regression",
        version_label="v1",
        task_definition_id=task_definition_id,
        artifact_uri="runs:/fake-run/model",
        artifact_checksum="a" * 64,
        mlflow_run_id="fake-run",
        ontology_version="1",
        feature_contract_version="1",
    )


async def test_register_candidate_always_starts_at_candidate_stage(session: AsyncSession) -> None:
    task = await _seed_task_definition(session)
    model_version = _model_version(tenant_id=None, task_definition_id=task.id)
    model_version.stage = ModelStage.PRODUCTION  # a caller trying to skip the queue

    registered = await ModelRegistryService(session).register_candidate(model_version)
    assert registered.stage == ModelStage.CANDIDATE


async def test_registry_metadata_round_trips(session: AsyncSession) -> None:
    task = await _seed_task_definition(session)
    model_version = _model_version(tenant_id=None, task_definition_id=task.id)
    registered = await ModelRegistryService(session).register_candidate(model_version)
    await session.commit()

    fetched = await ModelVersionRepository(session).get(registered.id)
    assert fetched is not None
    assert fetched.artifact_checksum == "a" * 64
    assert fetched.mlflow_run_id == "fake-run"
    assert fetched.ontology_version == "1"
    assert fetched.feature_contract_version == "1"


async def test_valid_stage_transitions_succeed(session: AsyncSession) -> None:
    task = await _seed_task_definition(session)
    registry = ModelRegistryService(session)
    model_version = await registry.register_candidate(
        _model_version(tenant_id=None, task_definition_id=task.id)
    )
    await session.commit()

    await registry.transition_stage(model_version.id, to_stage=ModelStage.STAGING)
    assert model_version.stage == ModelStage.STAGING
    await registry.transition_stage(model_version.id, to_stage=ModelStage.PRODUCTION)
    assert model_version.stage == ModelStage.PRODUCTION
    await registry.transition_stage(model_version.id, to_stage=ModelStage.ARCHIVED)
    assert model_version.stage == ModelStage.ARCHIVED


async def test_invalid_stage_transition_is_rejected(session: AsyncSession) -> None:
    task = await _seed_task_definition(session)
    registry = ModelRegistryService(session)
    model_version = await registry.register_candidate(
        _model_version(tenant_id=None, task_definition_id=task.id)
    )
    await session.commit()

    with pytest.raises(InvalidModelStageTransitionError):
        # candidate -> production directly is not an allowed transition
        await registry.transition_stage(model_version.id, to_stage=ModelStage.PRODUCTION)


async def test_archived_is_terminal(session: AsyncSession) -> None:
    task = await _seed_task_definition(session)
    registry = ModelRegistryService(session)
    model_version = await registry.register_candidate(
        _model_version(tenant_id=None, task_definition_id=task.id)
    )
    await registry.transition_stage(model_version.id, to_stage=ModelStage.ARCHIVED)

    with pytest.raises(InvalidModelStageTransitionError):
        await registry.transition_stage(model_version.id, to_stage=ModelStage.STAGING)


async def test_transitioning_an_unknown_model_version_raises(session: AsyncSession) -> None:
    with pytest.raises(ValueError, match="not found"):
        await ModelRegistryService(session).transition_stage(
            uuid.uuid4(), to_stage=ModelStage.STAGING
        )


async def test_tenant_sees_shared_and_own_models_but_not_another_tenants(
    session: AsyncSession,
) -> None:
    task = await _seed_task_definition(session)
    tenant_a = await TenantRepository(session).add(make_tenant())
    tenant_b = await TenantRepository(session).add(make_tenant())
    await session.commit()
    ctx_b = TenantContext(tenant_id=tenant_b.id, principal_id=uuid.uuid4())

    registry = ModelRegistryService(session)
    shared = await registry.register_candidate(
        _model_version(tenant_id=None, task_definition_id=task.id)
    )
    private_to_a = await registry.register_candidate(
        _model_version(tenant_id=tenant_a.id, task_definition_id=task.id)
    )
    await session.commit()

    available_to_b = await ModelVersionRepository(session).list_available_for_tenant(
        tenant_context=ctx_b
    )
    available_ids = {m.id for m in available_to_b}
    assert shared.id in available_ids
    assert private_to_a.id not in available_ids


async def test_get_available_for_tenant_hides_another_tenants_private_model(
    session: AsyncSession,
) -> None:
    task = await _seed_task_definition(session)
    tenant_a = await TenantRepository(session).add(make_tenant())
    tenant_b = await TenantRepository(session).add(make_tenant())
    await session.commit()
    ctx_b = TenantContext(tenant_id=tenant_b.id, principal_id=uuid.uuid4())

    private_to_a = await ModelRegistryService(session).register_candidate(
        _model_version(tenant_id=tenant_a.id, task_definition_id=task.id)
    )
    await session.commit()

    result = await ModelVersionRepository(session).get_available_for_tenant(
        private_to_a.id, tenant_context=ctx_b
    )
    assert result is None  # indistinguishable from "not found" — never a 403-style leak

    with pytest.raises(TenantMismatchError):
        await ModelVersionRepository(session).require_available_for_tenant(
            private_to_a.id, tenant_context=ctx_b
        )


async def test_tenant_model_adapter_is_strictly_tenant_scoped(session: AsyncSession) -> None:
    task = await _seed_task_definition(session)
    tenant_a = await TenantRepository(session).add(make_tenant())
    tenant_b = await TenantRepository(session).add(make_tenant())
    await session.commit()
    ctx_a = TenantContext(tenant_id=tenant_a.id, principal_id=uuid.uuid4())
    ctx_b = TenantContext(tenant_id=tenant_b.id, principal_id=uuid.uuid4())

    base_model = await ModelRegistryService(session).register_candidate(
        _model_version(tenant_id=None, task_definition_id=task.id)
    )
    await session.commit()

    adapters = TenantModelAdapterRepository(session)
    adapter = await adapters.add(
        TenantModelAdapter(
            base_model_version_id=base_model.id,
            name="alpha-adapter",
            version_label="v1",
            artifact_uri="runs:/fake-run/adapter",
            artifact_checksum="b" * 64,
            ontology_version="1",
            feature_contract_version="1",
        ),
        tenant_context=ctx_a,
    )
    await session.commit()

    # Tenant A can read its own adapter.
    assert await adapters.get(adapter.id, tenant_context=ctx_a) is not None
    # Tenant B cannot — indistinguishable from "not found," per TenantScopedRepository.
    assert await adapters.get(adapter.id, tenant_context=ctx_b) is None
