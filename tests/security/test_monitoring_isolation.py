"""Security tests for the monitoring surface (Phase 15): scope enforcement, and — the actual
"dashboards must not expose one tenant to another" requirement — that
`GET /v1/monitoring/summary` never reflects another tenant's activity.
"""

from __future__ import annotations

import uuid

from hermes_rpt.auth.enums import ScopeName
from tests.security.conftest import AuthFixture


def test_metrics_endpoint_requires_monitoring_read_scope(auth_fixture: AuthFixture) -> None:
    token = auth_fixture.token_for(scopes=[])
    response = auth_fixture.client.get("/metrics", headers=auth_fixture.auth_headers(token))
    assert response.status_code == 403


def test_metrics_endpoint_is_reachable_with_the_scope(auth_fixture: AuthFixture) -> None:
    token = auth_fixture.token_for(scopes=[ScopeName.MONITORING_READ.value])
    response = auth_fixture.client.get("/metrics", headers=auth_fixture.auth_headers(token))
    assert response.status_code == 200
    assert "hermes_" in response.text


def test_summary_endpoint_requires_monitoring_read_scope(auth_fixture: AuthFixture) -> None:
    token = auth_fixture.token_for(scopes=[])
    response = auth_fixture.client.get(
        "/v1/monitoring/summary", headers=auth_fixture.auth_headers(token)
    )
    assert response.status_code == 403


def test_summary_reflects_this_tenants_own_authorization_denials_only(
    auth_fixture: AuthFixture,
) -> None:
    # Trigger a real authorization denial as tenant A (missing tenant:admin for POST
    # /v1/memberships) — this records a tenant-A-scoped AuditEvent the summary should count.
    token_a_no_scope = auth_fixture.token_for(tenant_id=auth_fixture.tenant_a.id, scopes=[])
    denied = auth_fixture.client.post(
        "/v1/memberships",
        json={"user_id": str(auth_fixture.user_a.id), "role": "operator"},
        headers=auth_fixture.auth_headers(token_a_no_scope),
    )
    assert denied.status_code == 403

    token_a_monitoring = auth_fixture.token_for(
        tenant_id=auth_fixture.tenant_a.id, scopes=[ScopeName.MONITORING_READ.value]
    )
    token_b_monitoring = auth_fixture.token_for(
        tenant_id=auth_fixture.tenant_b.id, scopes=[ScopeName.MONITORING_READ.value]
    )

    summary_a = auth_fixture.client.get(
        "/v1/monitoring/summary", headers=auth_fixture.auth_headers(token_a_monitoring)
    ).json()
    summary_b = auth_fixture.client.get(
        "/v1/monitoring/summary", headers=auth_fixture.auth_headers(token_b_monitoring)
    ).json()

    assert summary_a["tenant_id"] == str(auth_fixture.tenant_a.id)
    assert summary_a["authorization_denials"] >= 1
    assert summary_b["tenant_id"] == str(auth_fixture.tenant_b.id)
    assert summary_b["authorization_denials"] == 0


def test_recording_an_outcome_for_another_tenants_prediction_is_not_found(
    auth_fixture: AuthFixture,
) -> None:
    token_b = auth_fixture.token_for(
        tenant_id=auth_fixture.tenant_b.id, scopes=[ScopeName.PREDICTION_EXECUTE.value]
    )
    response = auth_fixture.client.post(
        f"/v1/monitoring/predictions/{uuid.uuid4()}/outcome",
        json={"actual_label": True},
        headers=auth_fixture.auth_headers(token_b),
    )
    # Whether or not a prediction with that id exists at all for tenant A, tenant B must never
    # get anything other than "not found" — never a 200, never a 403 confirming existence.
    assert response.status_code == 404
