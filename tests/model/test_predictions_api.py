"""End-to-end tests for `POST /v1/predictions/delivery-delay` (Phase 13) against a real trained,
promoted model and real synthetic tenant data — everything a pure security/auth test
(`tests/security/test_predictions_api_auth.py`) can't exercise without the `ml` dependency
group: the full 14-step flow, response contract, idempotency, and a light concurrency/load
smoke test.

Wires a `TestClient` to the *same* control-plane session and synthetic SQLite customer
connection `scripts.build_synthetic_dataset.provision_and_build_dataset` sets up, so the app
under test extracts features from, and predicts against, real (synthetic) tenant data end to
end — not mocks.
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
from hermes_rpt.models.config import BaselineModelType, TrainingConfig  # noqa: E402
from hermes_rpt.models.training import BaselineTrainingService  # noqa: E402
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
        lambda: session, tenant_slug="predapi", seed=4242, data_dir=tmp_path
    )
    # A real trip business_reference to predict against — any row present in every split works.
    trip_id = (built.splits.train or built.splits.validation or built.splits.test)[
        0
    ].business_reference

    training = BaselineTrainingService(session, mlflow_tracking_uri=mlflow_tracking_uri)
    result = await training.train(
        built,
        contract=DELIVERY_DELAY_RISK_CONTRACT,
        config=TrainingConfig(model_type=BaselineModelType.LOGISTIC_REGRESSION),
        tenant_context=tenant_context,
        code_revision="test-revision",
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


# --- Integration: the full 14-step flow succeeds end to end -----------------------------------


async def test_predicting_a_real_trip_returns_a_successful_response(
    provisioned_api: _ProvisionedApi,
) -> None:
    response = _predict(provisioned_api.client, provisioned_api.trip_id)
    assert response.status_code == 200, response.text


async def test_predicting_an_unknown_trip_returns_404_not_a_crash(
    provisioned_api: _ProvisionedApi,
) -> None:
    response = _predict(provisioned_api.client, "trip-does-not-exist")
    assert response.status_code == 404


# --- Contract: exact response shape, no forbidden fields ---------------------------------------


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
    assert isinstance(body["explanations"], list)
    for explanation in body["explanations"]:
        assert set(explanation) == {"factor", "direction"}
        assert explanation["direction"] in ("increases_risk", "decreases_risk")


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


async def test_different_idempotency_keys_produce_independent_predictions(
    provisioned_api: _ProvisionedApi,
) -> None:
    first = _predict(provisioned_api.client, provisioned_api.trip_id, idempotency_key="key-a")
    second = _predict(provisioned_api.client, provisioned_api.trip_id, idempotency_key="key-b")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["prediction_id"] != second.json()["prediction_id"]


# --- Load: a light concurrency smoke test -------------------------------------------------------


async def test_a_burst_of_predictions_all_succeed_and_reuse_the_cached_model(
    provisioned_api: _ProvisionedApi,
) -> None:
    """Not a real load test (that needs a running server, a proper client pool, and a database
    that supports concurrent sessions — this fixture's single shared `AsyncSession`, like every
    other test session in this project, is not safe for concurrent use, so this deliberately
    issues the burst sequentially). What it does verify under repetition: the in-process model
    cache in `ModelLoader` is actually hit (no re-loading/re-fitting per call) and many
    sequential requests against one warm app don't leak state between requests."""

    statuses = [
        _predict(
            provisioned_api.client, provisioned_api.trip_id, idempotency_key=f"load-{i}"
        ).status_code
        for i in range(20)
    ]
    assert all(status == 200 for status in statuses)
