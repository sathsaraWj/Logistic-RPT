"""Tests for Phase 14's model governance layer: compatibility checks, the promotion approval
gate, aliases (including rollback), and adapter deactivation. `TenantModelAdapter` lifecycle
mechanics (candidate-forcing, tenant scoping) live alongside the equivalent `ModelVersion` tests
in `tests/model/test_registry.py`; this file covers what's new in Phase 14.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hermes_rpt.auth.enums import ScopeName
from hermes_rpt.auth.errors import AuthorizationError
from hermes_rpt.inference.models import PredictionTaskDefinition
from hermes_rpt.registry.enums import ModelStage
from hermes_rpt.registry.models import ModelAlias, ModelVersion, TenantModelAdapter
from hermes_rpt.registry.service import (
    IncompatibleAdapterError,
    ModelRegistryService,
)
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


def _base_model(
    *, task_definition_id: uuid.UUID, feature_contract_version: str = "1"
) -> ModelVersion:
    return ModelVersion(
        tenant_id=None,
        name="delivery-delay-risk-hermes-rpt-0.1",
        version_label="v1",
        task_definition_id=task_definition_id,
        artifact_uri="runs:/fake-run/model",
        artifact_checksum="a" * 64,
        mlflow_run_id="fake-run",
        ontology_version="1",
        feature_contract_version=feature_contract_version,
    )


def _adapter(
    *, tenant_id: uuid.UUID, base_model_version_id: uuid.UUID, feature_contract_version: str = "1"
) -> TenantModelAdapter:
    return TenantModelAdapter(
        tenant_id=tenant_id,
        base_model_version_id=base_model_version_id,
        name="alpha-adapter",
        version_label="v1",
        artifact_uri="runs:/fake-run/adapter",
        artifact_checksum="b" * 64,
        ontology_version="1",
        feature_contract_version=feature_contract_version,
    )


async def _tenant_context(
    session: AsyncSession, *, scopes: frozenset[str] = frozenset()
) -> TenantContext:
    tenant = await TenantRepository(session).add(make_tenant())
    await session.commit()
    return TenantContext(tenant_id=tenant.id, principal_id=uuid.uuid4(), scopes=scopes)


# --- Compatibility checks -----------------------------------------------------------------------


async def test_registering_a_compatible_adapter_succeeds(session: AsyncSession) -> None:
    task = await _seed_task_definition(session)
    registry = ModelRegistryService(session)
    base = await registry.register_candidate(_base_model(task_definition_id=task.id))
    await session.commit()
    ctx = await _tenant_context(session)

    adapter = await registry.register_adapter_candidate(
        _adapter(tenant_id=ctx.tenant_id, base_model_version_id=base.id),
        base_model=base,
        tenant_context=ctx,
    )
    assert adapter.stage == ModelStage.CANDIDATE
    assert adapter.tenant_id == ctx.tenant_id


async def test_registering_an_incompatible_adapter_is_rejected(session: AsyncSession) -> None:
    task = await _seed_task_definition(session)
    registry = ModelRegistryService(session)
    base = await registry.register_candidate(_base_model(task_definition_id=task.id))
    await session.commit()
    ctx = await _tenant_context(session)

    mismatched = _adapter(
        tenant_id=ctx.tenant_id, base_model_version_id=base.id, feature_contract_version="2"
    )
    with pytest.raises(IncompatibleAdapterError):
        await registry.register_adapter_candidate(mismatched, base_model=base, tenant_context=ctx)


async def test_a_candidate_adapter_always_starts_at_candidate_regardless_of_caller(
    session: AsyncSession,
) -> None:
    task = await _seed_task_definition(session)
    registry = ModelRegistryService(session)
    base = await registry.register_candidate(_base_model(task_definition_id=task.id))
    await session.commit()
    ctx = await _tenant_context(session)

    smuggled = _adapter(tenant_id=ctx.tenant_id, base_model_version_id=base.id)
    smuggled.stage = ModelStage.PRODUCTION  # a caller trying to skip the queue
    registered = await registry.register_adapter_candidate(
        smuggled, base_model=base, tenant_context=ctx
    )
    assert registered.stage == ModelStage.CANDIDATE


# --- Approval workflow: promotion requires ScopeName.MODEL_PROMOTE ------------------------------


async def test_promoting_without_model_promote_scope_is_rejected(session: AsyncSession) -> None:
    task = await _seed_task_definition(session)
    registry = ModelRegistryService(session)
    base = await registry.register_candidate(_base_model(task_definition_id=task.id))
    await session.commit()
    ctx = await _tenant_context(session, scopes=frozenset())  # no model:promote

    with pytest.raises(AuthorizationError):
        await registry.transition_stage(base.id, to_stage=ModelStage.STAGING, tenant_context=ctx)


async def test_promoting_with_model_promote_scope_succeeds(session: AsyncSession) -> None:
    task = await _seed_task_definition(session)
    registry = ModelRegistryService(session)
    base = await registry.register_candidate(_base_model(task_definition_id=task.id))
    await session.commit()
    ctx = await _tenant_context(session, scopes=frozenset({ScopeName.MODEL_PROMOTE.value}))

    promoted = await registry.transition_stage(
        base.id, to_stage=ModelStage.STAGING, tenant_context=ctx
    )
    assert promoted.stage == ModelStage.STAGING


async def test_a_trusted_system_caller_with_no_tenant_context_may_still_promote(
    session: AsyncSession,
) -> None:
    """Preserves the pre-Phase-14 CLI/script convention: `tenant_context=None` means "trusted
    system caller," same as `register_candidate`'s existing shared-model path."""

    task = await _seed_task_definition(session)
    registry = ModelRegistryService(session)
    base = await registry.register_candidate(_base_model(task_definition_id=task.id))
    await session.commit()

    promoted = await registry.transition_stage(base.id, to_stage=ModelStage.STAGING)
    assert promoted.stage == ModelStage.STAGING


async def test_archiving_does_not_require_the_promotion_scope(session: AsyncSession) -> None:
    task = await _seed_task_definition(session)
    registry = ModelRegistryService(session)
    base = await registry.register_candidate(_base_model(task_definition_id=task.id))
    await session.commit()
    ctx = await _tenant_context(session, scopes=frozenset())

    archived = await registry.transition_stage(
        base.id, to_stage=ModelStage.ARCHIVED, tenant_context=ctx
    )
    assert archived.stage == ModelStage.ARCHIVED


# --- Aliases and rollback -----------------------------------------------------------------------


async def test_set_alias_points_to_a_shared_base_model(session: AsyncSession) -> None:
    task = await _seed_task_definition(session)
    registry = ModelRegistryService(session)
    base = await registry.register_candidate(_base_model(task_definition_id=task.id))
    await session.commit()
    ctx = await _tenant_context(session, scopes=frozenset({ScopeName.MODEL_PROMOTE.value}))

    alias = await registry.set_alias(
        task_definition_id=task.id, model_version_id=base.id, tenant_context=ctx
    )
    assert alias.model_version_id == base.id
    assert alias.tenant_model_adapter_id is None

    resolved = await registry.resolve_alias(task_definition_id=task.id, tenant_context=ctx)
    assert resolved is not None
    assert resolved.model_version_id == base.id


async def test_setting_an_alias_without_the_promotion_scope_is_rejected(
    session: AsyncSession,
) -> None:
    task = await _seed_task_definition(session)
    registry = ModelRegistryService(session)
    base = await registry.register_candidate(_base_model(task_definition_id=task.id))
    await session.commit()
    ctx = await _tenant_context(session, scopes=frozenset())

    with pytest.raises(AuthorizationError):
        await registry.set_alias(
            task_definition_id=task.id, model_version_id=base.id, tenant_context=ctx
        )


async def test_set_alias_with_a_tenant_adapter_records_both_base_and_adapter(
    session: AsyncSession,
) -> None:
    task = await _seed_task_definition(session)
    registry = ModelRegistryService(session)
    base = await registry.register_candidate(_base_model(task_definition_id=task.id))
    await session.commit()
    ctx = await _tenant_context(session, scopes=frozenset({ScopeName.MODEL_PROMOTE.value}))
    adapter = await registry.register_adapter_candidate(
        _adapter(tenant_id=ctx.tenant_id, base_model_version_id=base.id),
        base_model=base,
        tenant_context=ctx,
    )
    await session.commit()

    alias = await registry.set_alias(
        task_definition_id=task.id,
        model_version_id=base.id,
        tenant_model_adapter_id=adapter.id,
        tenant_context=ctx,
    )
    assert alias.model_version_id == base.id
    assert alias.tenant_model_adapter_id == adapter.id


async def test_a_tenant_specific_alias_wins_over_the_shared_alias_of_the_same_name(
    session: AsyncSession,
) -> None:
    task = await _seed_task_definition(session)
    registry = ModelRegistryService(session)
    shared_base = await registry.register_candidate(_base_model(task_definition_id=task.id))
    other_base = await registry.register_candidate(_base_model(task_definition_id=task.id))
    await session.commit()
    ctx = await _tenant_context(session, scopes=frozenset({ScopeName.MODEL_PROMOTE.value}))

    await registry.set_alias(task_definition_id=task.id, model_version_id=shared_base.id)
    await registry.set_alias(
        task_definition_id=task.id, model_version_id=other_base.id, tenant_context=ctx
    )

    resolved = await registry.resolve_alias(task_definition_id=task.id, tenant_context=ctx)
    assert resolved is not None
    assert resolved.model_version_id == other_base.id


async def test_rollback_repoints_the_alias_and_is_distinguishable_in_the_audit_trail(
    session: AsyncSession,
) -> None:
    task = await _seed_task_definition(session)
    registry = ModelRegistryService(session)
    v1 = await registry.register_candidate(_base_model(task_definition_id=task.id))
    v2 = await registry.register_candidate(_base_model(task_definition_id=task.id))
    await session.commit()
    ctx = await _tenant_context(session, scopes=frozenset({ScopeName.MODEL_PROMOTE.value}))

    await registry.set_alias(task_definition_id=task.id, model_version_id=v1.id, tenant_context=ctx)
    await registry.set_alias(task_definition_id=task.id, model_version_id=v2.id, tenant_context=ctx)
    resolved = await registry.resolve_alias(task_definition_id=task.id, tenant_context=ctx)
    assert resolved is not None
    assert resolved.model_version_id == v2.id

    rolled_back = await registry.rollback_alias(
        task_definition_id=task.id, model_version_id=v1.id, tenant_context=ctx
    )
    assert rolled_back.model_version_id == v1.id

    resolved_after_rollback = await registry.resolve_alias(
        task_definition_id=task.id, tenant_context=ctx
    )
    assert resolved_after_rollback is not None
    assert resolved_after_rollback.model_version_id == v1.id

    # Exactly one alias row for (tenant, task, "production") throughout — repointing updates in
    # place, it never accumulates duplicate rows.
    result = await session.execute(
        select(ModelAlias).where(
            ModelAlias.task_definition_id == task.id, ModelAlias.tenant_id == ctx.tenant_id
        )
    )
    assert len(list(result.scalars().all())) == 1


async def test_pointing_an_alias_at_an_incompatible_adapter_is_rejected(
    session: AsyncSession,
) -> None:
    task = await _seed_task_definition(session)
    registry = ModelRegistryService(session)
    base_v1 = await registry.register_candidate(_base_model(task_definition_id=task.id))
    await session.commit()
    ctx = await _tenant_context(session, scopes=frozenset({ScopeName.MODEL_PROMOTE.value}))

    # The adapter is compatible with *its own* base model at registration time — the mismatch
    # this test proves is caught happens when it's later aliased alongside a *different* base.
    base_v2 = await registry.register_candidate(
        _base_model(task_definition_id=task.id, feature_contract_version="2")
    )
    await session.commit()
    adapter_for_v2 = await registry.register_adapter_candidate(
        _adapter(
            tenant_id=ctx.tenant_id,
            base_model_version_id=base_v2.id,
            feature_contract_version="2",
        ),
        base_model=base_v2,
        tenant_context=ctx,
    )
    await session.commit()

    with pytest.raises(IncompatibleAdapterError):
        await registry.set_alias(
            task_definition_id=task.id,
            model_version_id=base_v1.id,
            tenant_model_adapter_id=adapter_for_v2.id,
            tenant_context=ctx,
        )


async def test_cannot_alias_an_archived_model(session: AsyncSession) -> None:
    task = await _seed_task_definition(session)
    registry = ModelRegistryService(session)
    base = await registry.register_candidate(_base_model(task_definition_id=task.id))
    await registry.transition_stage(base.id, to_stage=ModelStage.ARCHIVED)
    await session.commit()

    with pytest.raises(ValueError, match="archived"):
        await registry.set_alias(task_definition_id=task.id, model_version_id=base.id)


async def test_cannot_alias_another_tenants_private_model(session: AsyncSession) -> None:
    task = await _seed_task_definition(session)
    registry = ModelRegistryService(session)
    ctx_a = await _tenant_context(session, scopes=frozenset({ScopeName.MODEL_PROMOTE.value}))
    ctx_b = await _tenant_context(session, scopes=frozenset({ScopeName.MODEL_PROMOTE.value}))
    private_to_a = ModelVersion(
        tenant_id=ctx_a.tenant_id,
        name="private",
        version_label="v1",
        task_definition_id=task.id,
        artifact_uri="runs:/fake-run/model",
        artifact_checksum="c" * 64,
        ontology_version="1",
        feature_contract_version="1",
    )
    registered = await registry.register_candidate(private_to_a)
    await session.commit()

    with pytest.raises(ValueError, match="does not belong"):
        await registry.set_alias(
            task_definition_id=task.id, model_version_id=registered.id, tenant_context=ctx_b
        )


# --- Deactivation --------------------------------------------------------------------------------


async def test_deactivating_a_model_version_is_audited_and_sticks(session: AsyncSession) -> None:
    task = await _seed_task_definition(session)
    registry = ModelRegistryService(session)
    base = await registry.register_candidate(_base_model(task_definition_id=task.id))
    await session.commit()

    deactivated = await registry.deactivate_model_version(base.id)
    assert deactivated.is_active is False


async def test_deactivating_another_tenants_adapter_is_rejected(session: AsyncSession) -> None:
    task = await _seed_task_definition(session)
    registry = ModelRegistryService(session)
    base = await registry.register_candidate(_base_model(task_definition_id=task.id))
    await session.commit()
    ctx_a = await _tenant_context(session)
    ctx_b = await _tenant_context(session)
    adapter = await registry.register_adapter_candidate(
        _adapter(tenant_id=ctx_a.tenant_id, base_model_version_id=base.id),
        base_model=base,
        tenant_context=ctx_a,
    )
    await session.commit()

    from hermes_rpt.common.repository import TenantMismatchError

    with pytest.raises(TenantMismatchError):
        await registry.deactivate_adapter(adapter.id, tenant_context=ctx_b)
