"""Tenant isolation for Hermes-RPT-0.1's live relational-context inference path (Phase 19).

`RelationalContextBuilder.build_example` was, until now, only ever called from training-time
callers (`HermesRPTTrainingService`, `apps/trainer/main.py`). Wiring it into live, per-request
inference (`PredictionService.predict`) introduces a new caller of the same tenant-scoped fetch
machinery — this proves that caller doesn't reopen the class of tenant-identity-spoofing bug a
Phase 16 security review found in dataset-building. Uses two real, separately-provisioned
synthetic tenants with their own customer databases (`provision_and_build_dataset`) rather than
the lightweight control-plane-only fixture `test_tenant_isolation.py` uses elsewhere in this
directory — this needs a real second tenant database to attempt crossing into.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from apps.api.deps import (  # noqa: E402
    get_connection_lifecycle_manager,
    get_model_loader,
)
from apps.api.main import create_app  # noqa: E402
from scripts.build_synthetic_dataset import provision_and_build_dataset  # noqa: E402

from hermes_rpt.auth.dev_tokens import issue_dev_token  # noqa: E402
from hermes_rpt.auth.enums import ScopeName  # noqa: E402
from hermes_rpt.common.db import get_session  # noqa: E402
from hermes_rpt.common.settings import get_settings  # noqa: E402
from hermes_rpt.connectors.service import ConnectionLifecycleManager  # noqa: E402
from hermes_rpt.features.contract import DELIVERY_DELAY_RISK_CONTRACT  # noqa: E402
from hermes_rpt.inference.model_loading import ModelLoader  # noqa: E402
from hermes_rpt.inference.models import PredictionTaskDefinition  # noqa: E402
from hermes_rpt.models.transformer.context import (  # noqa: E402
    RelationalContextBuilder,
    TargetRecordNotFoundError,
)
from hermes_rpt.models.transformer.model import TINY  # noqa: E402
from hermes_rpt.models.transformer.training import HermesRPTTrainingService  # noqa: E402
from hermes_rpt.registry.enums import ModelStage  # noqa: E402
from hermes_rpt.registry.service import ModelRegistryService  # noqa: E402
from hermes_rpt.tenants.context import TenantContext  # noqa: E402
from tests.security.conftest import _SingleSessionFactory  # noqa: E402


class _TwoTenants:
    def __init__(
        self,
        *,
        client: TestClient,
        alpha_ctx: TenantContext,
        alpha_manager: ConnectionLifecycleManager,
        alpha_trip_id: str,
        beta_trip_id: str,
        beta_tenant_id: object,
    ) -> None:
        self.client = client
        self.alpha_ctx = alpha_ctx
        self.alpha_manager = alpha_manager
        self.alpha_trip_id = alpha_trip_id
        self.beta_trip_id = beta_trip_id
        self.beta_tenant_id = beta_tenant_id


@pytest_asyncio.fixture
async def two_tenants(session: AsyncSession, tmp_path: Path) -> AsyncIterator[_TwoTenants]:
    mlflow_tracking_uri = f"sqlite:///{(tmp_path / 'mlflow.db').as_posix()}"

    session.add(
        PredictionTaskDefinition(
            task_key=DELIVERY_DELAY_RISK_CONTRACT.task_key,
            name="Delivery Delay Risk",
            feature_contract_version=DELIVERY_DELAY_RISK_CONTRACT.version,
        )
    )
    await session.flush()

    alpha_ctx, alpha_manager, alpha_built = await provision_and_build_dataset(
        lambda: session, tenant_slug="isoalpha", seed=6161, data_dir=tmp_path
    )
    beta_ctx, _beta_manager, beta_built = await provision_and_build_dataset(
        lambda: session, tenant_slug="isobeta", seed=6262, data_dir=tmp_path
    )
    alpha_trip_id = (
        alpha_built.splits.train or alpha_built.splits.validation or alpha_built.splits.test
    )[0].business_reference
    beta_trip_id = (
        beta_built.splits.train or beta_built.splits.validation or beta_built.splits.test
    )[0].business_reference

    context_builder = RelationalContextBuilder(session, connection_manager=alpha_manager)
    training = HermesRPTTrainingService(
        session, context_builder=context_builder, mlflow_tracking_uri=mlflow_tracking_uri
    )
    result = await training.train(
        alpha_built,
        config=TINY,
        tenant_context=alpha_ctx,
        code_revision="test-revision",
        task_key=DELIVERY_DELAY_RISK_CONTRACT.task_key,
        checkpoint_dir=tmp_path / "checkpoints",
    )
    await session.commit()

    registry = ModelRegistryService(session)
    await registry.transition_stage(result.model_version_id, to_stage=ModelStage.STAGING)
    await registry.transition_stage(result.model_version_id, to_stage=ModelStage.PRODUCTION)
    await session.commit()

    app = create_app()

    async def _override_get_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[get_connection_lifecycle_manager] = lambda: alpha_manager
    app.dependency_overrides[get_model_loader] = lambda: ModelLoader(
        mlflow_tracking_uri=mlflow_tracking_uri
    )
    app.state.db_sessionmaker = _SingleSessionFactory(session)

    token = issue_dev_token(
        settings=get_settings(),
        tenant_id=alpha_ctx.tenant_id,
        principal_id=alpha_ctx.principal_id,
        scopes=[ScopeName.PREDICTION_EXECUTE.value],
    )

    with TestClient(app) as client:
        client.headers.update({"Authorization": f"Bearer {token}"})
        yield _TwoTenants(
            client=client,
            alpha_ctx=alpha_ctx,
            alpha_manager=alpha_manager,
            alpha_trip_id=alpha_trip_id,
            beta_trip_id=beta_trip_id,
            beta_tenant_id=beta_ctx.tenant_id,
        )


async def test_building_relational_context_for_another_tenants_trip_finds_nothing(
    session: AsyncSession, two_tenants: _TwoTenants
) -> None:
    """Alpha's own `RelationalContextBuilder`, bound to Alpha's connection manager and
    `TenantContext`, must not be able to resolve Beta's trip at all — structurally impossible to
    cross over (Alpha's mappings/connection only ever resolve Alpha's own customer database), not
    merely filtered after the fact."""

    builder = RelationalContextBuilder(session, connection_manager=two_tenants.alpha_manager)

    with pytest.raises(TargetRecordNotFoundError):
        await builder.build_example(
            business_reference=two_tenants.beta_trip_id,
            prediction_time=datetime(2026, 6, 1, tzinfo=UTC),
            label=None,
            tenant_context=two_tenants.alpha_ctx,
            max_records_per_relation=TINY.max_records_per_relation,
        )


async def test_predicting_another_tenants_trip_through_the_live_endpoint_returns_404_not_the_data(
    two_tenants: _TwoTenants,
) -> None:
    response = two_tenants.client.post(
        "/v1/predictions/delivery-delay",
        json={"trip_id": two_tenants.beta_trip_id, "prediction_time": "2026-06-01T00:00:00Z"},
    )

    assert response.status_code == 404
    assert str(two_tenants.beta_tenant_id) not in response.text
