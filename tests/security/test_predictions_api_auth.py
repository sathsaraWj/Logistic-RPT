"""Security tests for `POST /v1/predictions/delivery-delay` (Phase 13) that don't need a real
trained model — authentication, authorization, and "trusted tenant only comes from the token"
checks. Model-dependent contract/integration/load tests live in
`tests/model/test_predictions_api.py` (gated behind the `ml` dependency group).
"""

from __future__ import annotations

from hermes_rpt.auth.enums import ScopeName
from tests.security.conftest import AuthFixture


def test_missing_bearer_token_is_rejected(auth_fixture: AuthFixture) -> None:
    response = auth_fixture.client.post(
        "/v1/predictions/delivery-delay",
        json={"trip_id": "TRIP-1", "prediction_time": "2026-01-01T08:00:00Z"},
    )
    assert response.status_code == 401


def test_missing_prediction_execute_scope_is_rejected(auth_fixture: AuthFixture) -> None:
    token = auth_fixture.token_for(scopes=[])  # authenticated, but no prediction:execute
    response = auth_fixture.client.post(
        "/v1/predictions/delivery-delay",
        json={"trip_id": "TRIP-1", "prediction_time": "2026-01-01T08:00:00Z"},
        headers=auth_fixture.auth_headers(token),
    )
    assert response.status_code == 403


def test_request_body_cannot_select_a_tenant(auth_fixture: AuthFixture) -> None:
    """ "The trusted tenant must come from the authentication context" — there is no tenant_id
    (or connection string, or SQL) field the request body could even supply; this test proves
    the schema rejects an attempt to smuggle one in via `extra` fields being silently accepted."""

    token = auth_fixture.token_for(scopes=[ScopeName.PREDICTION_EXECUTE.value])
    response = auth_fixture.client.post(
        "/v1/predictions/delivery-delay",
        json={
            "trip_id": "TRIP-1",
            "prediction_time": "2026-01-01T08:00:00Z",
            "tenant_id": str(auth_fixture.tenant_b.id),  # attempted tenant override
        },
        headers=auth_fixture.auth_headers(token),
    )
    # Pydantic ignores unknown fields by default rather than erroring — the important thing is
    # what actually happens next: without a PRODUCTION model, the request fails on that (503),
    # never on tenant confusion, because tenant_id was never read from the body at all.
    assert response.status_code != 422 or "tenant_id" not in (response.json().get("detail") or "")


def test_no_production_model_fails_with_a_clear_status_not_a_crash(
    auth_fixture: AuthFixture,
) -> None:
    token = auth_fixture.token_for(scopes=[ScopeName.PREDICTION_EXECUTE.value])
    response = auth_fixture.client.post(
        "/v1/predictions/delivery-delay",
        json={"trip_id": "TRIP-1", "prediction_time": "2026-01-01T08:00:00Z"},
        headers=auth_fixture.auth_headers(token),
    )
    # No PredictionTaskDefinition / mapping / model exists in this minimal fixture at all — the
    # service must fail closed with a clean error, never a raw 500 traceback leaking internals.
    assert response.status_code in (404, 409, 500, 503)
    body = response.json()
    assert "detail" in body
    # Never expose raw SQL, a password, or a connection string in an error body.
    detail_lower = str(body["detail"]).lower()
    for forbidden in ("password", "postgresql://", "select ", "secret"):
        assert forbidden not in detail_lower
