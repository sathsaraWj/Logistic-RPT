# Security Incident Response

Status: written in Phase 16 (Security Hardening), closing the forward-reference left in
[docs/runbooks/INCIDENT_RUNBOOK.md](runbooks/INCIDENT_RUNBOOK.md). That runbook covers general,
operational triage from a monitoring alert; this document is the stricter path for a **suspected
security incident specifically**: a confirmed or suspected breach, credential compromise, or
cross-tenant data exposure. Follow the general runbook's §1–3 (where signals come from, first
five minutes, connection/secret failures) first — this document layers on top once §2 of that
runbook flags something as adversarial rather than a misconfiguration.

## 1. What counts as a security incident here

Per [docs/THREAT_MODEL.md](THREAT_MODEL.md) §1, the platform's central risk is cross-tenant data
exposure, so that's the bar: any of the following is a security incident, not routine
operational noise —

* Confirmed or strongly suspected cross-tenant data access (Tenant A read, inferred, or
  influenced Tenant B's data, mappings, features, predictions, or model behavior).
* A customer database credential, the platform's own JWT signing secret, or a service-account
  key is confirmed or suspected compromised.
* A sustained, pattern-shaped rise in `hermes_cross_tenant_access_attempts_total`,
  `hermes_authentication_failures_total`, or `hermes_authorization_failures_total`
  ([docs/MONITORING.md](MONITORING.md)) that looks adversarial (systematic UUID enumeration,
  credential stuffing shape) rather than a single misconfigured integration retrying.
* Evidence of a substituted or tampered model artifact (an `ArtifactIntegrityError` that wasn't
  caused by a known-benign cause like an interrupted MLflow upload).
* Any of [docs/SECURITY_REVIEW.md](SECURITY_REVIEW.md) §5's adversarial-test scenarios observed
  succeeding against a real deployment, not just failing safely in the test suite.

## 2. Severity classification

| Severity | Definition | Example |
|---|---|---|
| **SEV-1** | Confirmed cross-tenant data exposure, or a Critical-asset credential (customer DB credential, JWT signing secret) confirmed compromised and usable | An attacker actually read another tenant's rows; the JWT secret leaked and a forged token was used successfully |
| **SEV-2** | Strong suspicion of the above, not yet confirmed, or a contained/self-limiting exposure (e.g. one tenant's own misconfigured token, no cross-tenant reach) | Adversarial-pattern spike in cross-tenant-access-attempt metrics, all rejected (404, not 403 — per ADR-0003) with no confirmed success |
| **SEV-3** | A security-relevant bug found (e.g. via `tests/security` or a review) with no evidence of exploitation, or a deferred finding from SECURITY_REVIEW.md §3 that needs re-prioritizing | A new Medium finding discovered in code review, not observed being exploited |

## 3. Response steps (SEV-1 / SEV-2)

1. **Contain first, investigate in parallel — don't wait for full understanding to act.**
   - Credential compromise: rotate immediately (`POST /v1/connections/{id}/rotate-secret` for a
     customer DB credential; for the platform's own `JWT_SECRET_KEY`, rotate the Secret Manager
     secret per [docs/DEPLOYMENT.md](DEPLOYMENT.md) §4 — this invalidates every currently-issued
     token, a real availability cost, and is still the correct call for a confirmed secret
     leak).
   - Suspected malicious mapping/connection/model version: suspend it
     (`mapping_suspensions_total`-style lifecycle transition) rather than deleting — preserves
     evidence for the timeline in step 2.
   - Suspected compromised background job or worker: nothing in this codebase runs jobs as a
     durable external queue yet (`hermes_rpt.schemas.jobs` is in-process,
     `asyncio.create_task`-scheduled — see that module's docstring), so containment here means
     stopping/redeploying the affected process, not cancelling a queued job elsewhere.
2. **Build the timeline** from the audit trail (`AuditEventRepository`) for every affected
   `tenant_id`, cross-referenced against the relevant metric series
   ([docs/MONITORING.md](MONITORING.md)). Every mutating action and every prediction has an
   `AuditEvent`; this is the authoritative record, not logs (which are for debugging, not
   evidence — and are redacted by design, see [docs/DATA_PROTECTION.md](DATA_PROTECTION.md) §4).
3. **Determine actual blast radius**: exactly which tenant(s) were affected, exactly which
   asset(s) (per the classification table in [docs/DATA_PROTECTION.md](DATA_PROTECTION.md) §1),
   and — critically — which tenants were confirmed **not** affected. Don't extrapolate from
   "the vulnerability existed" to "every tenant was exposed" without evidence either way; both
   an under-claim and an over-claim are harmful to the tenants involved.
4. **Fix the root cause**, not just the immediate symptom — add a regression test in
   `tests/security` for the specific exploited path (matching the standing pattern established
   throughout Phase 16: every fix in [docs/SECURITY_REVIEW.md](SECURITY_REVIEW.md) §2/§3 has a
   named test). If the incident reveals a gap not already tracked in SECURITY_REVIEW.md §3,
   add it there even if it can't be fixed immediately.
5. **Disclosure.** Only affected tenants are told they were affected, and only what happened to
   *their* data — never confirm or deny involvement of other tenants to a tenant who asks (see
   [docs/runbooks/INCIDENT_RUNBOOK.md](runbooks/INCIDENT_RUNBOOK.md) §4, restated below because
   it's the single easiest rule to violate under pressure during an active incident). What a
   disclosure includes: what happened, what data/timeframe was affected, what's been done about
   it (rotation, patch, added test), and what the tenant should do if anything (e.g. rotate
   their own DB credential if it was the compromised one). What it never includes: another
   tenant's identity, any raw data value, or internal system detail beyond what's needed to
   understand the impact.
6. **Postmortem**, written up against platform IDs only (never raw customer data or another
   tenant's identity — same rule as step 5), covering: timeline, root cause, blast radius as
   determined in step 3, the fix and its test, and whether any SECURITY_REVIEW.md deferred
   finding contributed (if so, that finding's priority should be revisited).

## 4. What must never leave the internal boundary

Restated from [docs/runbooks/INCIDENT_RUNBOOK.md](runbooks/INCIDENT_RUNBOOK.md) §4 because it
governs every step above, not just the general runbook:

* A specific tenant's identity, in any external-facing notification (a public status page, a
  channel other tenants can see, a ticket visible to a different customer).
* Any raw customer data value, a connection string, or a secret — in a paging message, a ticket,
  or a postmortem doc. Reference platform IDs (`tenant_id`, `connection_id`, `prediction_id`)
  instead.
* An admission of which *other* tenants were or weren't affected, to a tenant asking about their
  own incident.

## 5. SEV-3 handling

No containment or disclosure step applies — route through the normal fix-and-test workflow
described in [docs/SECURITY_REVIEW.md](SECURITY_REVIEW.md): fix immediately if it's newly
Critical/High (that document's own bar for "not complete"), or add it to that document's §3
deferred-findings table with reasoning if it's Medium/Low and genuinely not urgent.

## 6. Related documents

* [docs/runbooks/INCIDENT_RUNBOOK.md](runbooks/INCIDENT_RUNBOOK.md) — general operational
  triage; start here for any alert, security or not.
* [docs/SECURITY_REVIEW.md](SECURITY_REVIEW.md) — the current state of known findings; check §3
  first when triaging whether something is a new incident or a known, tracked gap.
* [docs/DATA_PROTECTION.md](DATA_PROTECTION.md) — the asset classification referenced in §3's
  blast-radius assessment.
* [docs/MONITORING.md](MONITORING.md) — the metric families referenced throughout.
