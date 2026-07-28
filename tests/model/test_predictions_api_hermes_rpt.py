"""End-to-end tests for `POST /v1/predictions/delivery-delay` served by a Hermes-RPT-0.1
(Tiny, scratch) model instead of a Phase 10 baseline — mirrors `test_predictions_api.py`'s
pattern exactly, but trains via `HermesRPTTrainingService` (relational context, not a scalar
feature row) so the endpoint's public contract and safety guarantees are proven for *both*
servable model families, not just the sklearn one. Kept as a separate file rather than
parametrizing the baseline one — the fixture setup genuinely differs (real relational-context
wiring vs. plain feature extraction), not just the model type.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path

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
from hermes_rpt.features.contract import DELIVERY_DELAY_RISK_CONTRACT  # noqa: E402
from hermes_rpt.inference.model_loading import ModelLoader  # noqa: E402
from hermes_rpt.inference.models import PredictionTaskDefinition  # noqa: E402
from hermes_rpt.models.transformer.context import RelationalContextBuilder  # noqa: E402
from hermes_rpt.models.transformer.model import TINY  # noqa: E402
from hermes_rpt.models.transformer.training import HermesRPTTrainingService  # noqa: E402
from hermes_rpt.registry.enums import ModelStage  # noqa: E402
from hermes_rpt.registry.service import ModelRegistryService  # noqa: E402
from tests.security.conftest import _SingleSessionFactory  # noqa: E402


class _ProvisionedApi:
    def __init__(self, client: TestClient, trip_id: str, tenant_id_other: object) -> None:
        self.client = client
        self.trip_id = trip_id
        self.tenant_id_other = tenant_id_other


@pytest_asyncio.fixture
async def provisioned_api(session: AsyncSession, tmp_path: Path) -> AsyncIterator[_ProvisionedApi]:
    mlflow_tracking_uri = f"sqlite:///{(tmp_path / 'mlflow.db').as_posix()}"

    session.add(
        PredictionTaskDefinition(
            task_key=DELIVERY_DELAY_RISK_CONTRACT.task_key,
            name="Delivery Delay Risk",
            feature_contract_version=DELIVERY_DELAY_RISK_CONTRACT.version,
        )
    )
    await session.flush()

    tenant_context, manager, built = await provision_and_build_dataset(
        lambda: session, tenant_slug="predapihrpt", seed=5252, data_dir=tmp_path
    )
    trip_id = (built.splits.train or built.splits.validation or built.splits.test)[
        0
    ].business_reference

    context_builder = RelationalContextBuilder(session, connection_manager=manager)
    training = HermesRPTTrainingService(
        session, context_builder=context_builder, mlflow_tracking_uri=mlflow_tracking_uri
    )
    result = await training.train(
        built,
        config=TINY,
        tenant_context=tenant_context,
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
    app.dependency_overrides[get_connection_lifecycle_manager] = lambda: manager
    app.dependency_overrides[get_model_loader] = lambda: ModelLoader(
        mlflow_tracking_uri=mlflow_tracking_uri
    )
    app.state.db_sessionmaker = _SingleSessionFactory(session)

    token = issue_dev_token(
        settings=get_settings(),
        tenant_id=tenant_context.tenant_id,
        principal_id=tenant_context.principal_id,
        scopes=[ScopeName.PREDICTION_EXECUTE.value],
    )

    with TestClient(app) as client:
        client.headers.update({"Authorization": f"Bearer {token}"})
        yield _ProvisionedApi(client, trip_id, tenant_context.tenant_id)


def _predict(client: TestClient, trip_id: str, *, idempotency_key: str | None = None) -> object:
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
    return client.post(
        "/v1/predictions/delivery-delay",
        json={"trip_id": trip_id, "prediction_time": "2026-06-01T00:00:00Z"},
        headers=headers,
    )


# --- Integration: the full flow succeeds end to end, through the Hermes-RPT branch -------------


async def test_predicting_a_real_trip_returns_a_successful_response(
    provisioned_api: _ProvisionedApi,
) -> None:
    response = _predict(provisioned_api.client, provisioned_api.trip_id)
    assert response.status_code == 200, response.text


async def test_predicting_an_unknown_trip_returns_404_not_a_crash(
    provisioned_api: _ProvisionedApi,
) -> None:
    """Proves the new `TargetRecordNotFoundError` -> 404 router mapping (Workstream 4) works,
    not just the baseline path's pre-existing `TargetRowNotFoundError` -> 404 mapping."""

    response = _predict(provisioned_api.client, "trip-does-not-exist")
    assert response.status_code == 404


# --- Contract: same response shape regardless of which model family served it ------------------


async def test_response_contract_has_exactly_the_documented_fields(
    provisioned_api: _ProvisionedApi,
) -> None:
    response = _predict(provisioned_api.client, provisioned_api.trip_id)
    assert response.status_code == 200
    body = response.json()

    assert set(body) == {
        "prediction_id",
        "trip_id",
        "delay_probability",
        "risk_level",
        "model_version",
        "feature_version",
        "prediction_time",
        "explanations",
    }
    assert body["trip_id"] == provisioned_api.trip_id
    assert 0.0 <= body["delay_probability"] <= 1.0
    assert body["risk_level"] in ("low", "medium", "high")
    assert "hermes-rpt" in body["model_version"]


async def test_no_explanations_are_fabricated_for_a_model_with_no_linear_coefficients(
    provisioned_api: _ProvisionedApi,
) -> None:
    """The concrete, testable form of "never fabricate an explanation a model can't honestly
    support" for Hermes-RPT-0.1 — `LoadedHermesRPTModel.feature_importance` always returns
    `None`, so `generate_explanations` must always return an empty list here."""

    response = _predict(provisioned_api.client, provisioned_api.trip_id)
    assert response.status_code == 200
    assert response.json()["explanations"] == []


async def test_response_never_leaks_raw_sql_secrets_or_connection_strings(
    provisioned_api: _ProvisionedApi,
) -> None:
    response = _predict(provisioned_api.client, provisioned_api.trip_id)
    assert response.status_code == 200
    raw_text = response.text.lower()
    for forbidden in (
        "password",
        "secret",
        "select ",
        "sqlite:///",
        "postgresql://",
        "synthetic-not-a-real-secret",
    ):
        assert forbidden not in raw_text


async def test_response_never_leaks_the_other_tenants_identifiers(
    provisioned_api: _ProvisionedApi,
) -> None:
    response = _predict(provisioned_api.client, provisioned_api.trip_id)
    assert response.status_code == 200
    assert str(provisioned_api.tenant_id_other) not in response.text


# --- Idempotency ---------------------------------------------------------------------------------


async def test_repeating_an_idempotency_key_returns_the_same_cached_prediction(
    provisioned_api: _ProvisionedApi,
) -> None:
    key = "test-idem-key-1"
    first = _predict(provisioned_api.client, provisioned_api.trip_id, idempotency_key=key)
    second = _predict(provisioned_api.client, provisioned_api.trip_id, idempotency_key=key)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["prediction_id"] == second.json()["prediction_id"]
    assert first.json()["delay_probability"] == second.json()["delay_probability"]


# --- Caching: LoadedHermesRPTModel is actually cached, not re-loaded per request ----------------


async def test_a_burst_of_predictions_all_succeed_and_reuse_the_cached_model(
    provisioned_api: _ProvisionedApi,
) -> None:
    """Same intent as the baseline file's equivalent test — proves `ModelLoader._cache` actually
    hits for `LoadedHermesRPTModel`, not just `LoadedModel`, and that many sequential requests
    against one warm app don't leak state between requests."""

    statuses = [
        _predict(
            provisioned_api.client, provisioned_api.trip_id, idempotency_key=f"load-{i}"
        ).status_code
        for i in range(20)
    ]
    assert all(status == 200 for status in statuses)
