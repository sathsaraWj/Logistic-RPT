# Incident Runbook

Status: implements part of Phase 15 of [IMPLEMENTATION_PLAN.md](../IMPLEMENTATION_PLAN.md)
("add an incident runbook"). General, operational — triggered by any monitoring alert. For
security-specific incidents (a suspected breach, credential compromise, confirmed cross-tenant
data exposure), also follow [docs/INCIDENT_RESPONSE.md](../INCIDENT_RESPONSE.md) (Phase 16),
which layers stricter severity classification, containment, and disclosure steps on top of the
triage flow below.

## 1. Where signals come from

* **Metrics** (`hermes_rpt.monitoring.metrics`) — scraped from `GET /metrics`
  (`ScopeName.MONITORING_READ`, operator-only — see
  [docs/DEPLOYMENT.md](../DEPLOYMENT.md) §3 for why this scope must only ever be issued to
  trusted internal scraper credentials, never a tenant token). Every tenant-relevant metric
  carries a `tenant_id` label so an alert can name the affected tenant internally.
* **Audit trail** (`hermes_rpt.audit`) — the authoritative, queryable record behind every
  metric; `AuditEvent` rows persist even if a metric series is lost or a dashboard misconfigured.
* **`GET /v1/monitoring/summary`** — what a specific tenant's own operator/support contact sees
  for that tenant (never another tenant's).

## 2. Triage — first five minutes

1. **Identify the affected tenant(s)**, from the alert's `tenant_id` label (internal alerting
   only — see §4 on what must never leave that boundary).
2. **Classify the signal** against the metric families in
   [docs/MONITORING.md](../MONITORING.md):
   - `hermes_cross_tenant_access_attempts_total`, `hermes_authentication_failures_total`,
     `hermes_authorization_failures_total` rising → possible attack or a broken client;
     escalate to the security-incident path (§4, and [../INCIDENT_RESPONSE.md](../INCIDENT_RESPONSE.md))
     if the rate or pattern looks adversarial rather than a single misconfigured integration.
   - `hermes_secret_resolution_failures_total`, `hermes_unexpected_connection_usage_total` →
     likely a credential rotation gone wrong or a customer-side database outage; see §5 for the
     connection-specific checks.
   - `hermes_schema_fingerprint_changes_total`, `hermes_mapping_suspensions_total` → schema
     drift; go to [SCHEMA_DRIFT_RUNBOOK.md](SCHEMA_DRIFT_RUNBOOK.md).
   - `hermes_prediction_requests_total{status="error"}`, elevated `hermes_prediction_latency_
     seconds`, or `hermes_model_calibration_drift` → model/serving issue; go to
     [MODEL_ROLLBACK_RUNBOOK.md](MODEL_ROLLBACK_RUNBOOK.md) if a specific model version is
     implicated.
3. **Pull the audit trail** for the affected tenant and time window (`AuditEventRepository.
   list_for_tenant`, or a direct read against `audit_events` if the API surface for this doesn't
   exist yet) to build a precise timeline before acting.

## 3. Connection / secret failures (§2's second bullet)

1. Check `CustomerDatabaseConnection.status`/`.last_error` for the affected connection
   (`GET /v1/connections/{id}`).
2. If `status=ERROR` and `last_error` mentions authentication: the credential may have rotated
   on the customer's side without a corresponding `POST /v1/connections/{id}/rotate-secret`
   call here — coordinate with the tenant to get a fresh credential, then rotate.
3. If `hermes_secret_resolution_failures_total` fired but the connection *looks* healthy: check
   Secret Manager (§3 of docs/DEPLOYMENT.md) directly for the referenced key — a manually
   deleted secret outside this platform's own lifecycle is the most likely cause.
4. Never log or display the resolved secret value while debugging — `hermes_rpt.common.
   logging`'s redaction processor strips known-secret-shaped values automatically, but do not
   work around it (e.g. by printing to a terminal outside the logging pipeline).

## 4. What must never leave the internal boundary

* A specific tenant's identity, in any external-facing notification (a public status page, a
  channel other tenants can see, a ticket visible to a different customer).
* Any raw customer data value, a connection string, or a secret — in a paging message, a ticket,
  or a postmortem doc. Reference the platform's own IDs (`tenant_id`, `connection_id`,
  `prediction_id`) instead; anyone with the right access can look up detail from there.
* An admission of which *other* tenants were or weren't affected, to a tenant asking about their
  own incident — "cross-tenant access attempts" existing at all should not itself confirm which
  tenant was targeted.

## 5. After the incident

1. Record what happened as an audit-visible fact where possible (most response *actions* —
   deactivating a model, suspending a mapping, rotating a secret — already produce their own
   `AuditEvent` automatically; nothing extra to do there).
2. If a metric or alert threshold needs adjusting as a result, update
   [docs/MONITORING.md](../MONITORING.md) in the same change.
3. If the incident involved a security boundary (auth, tenant isolation, secrets), also complete
   the disclosure/containment steps in [../INCIDENT_RESPONSE.md](../INCIDENT_RESPONSE.md).
