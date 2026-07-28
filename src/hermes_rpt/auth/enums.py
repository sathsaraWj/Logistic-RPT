from __future__ import annotations

from enum import StrEnum


class ScopeName(StrEnum):
    """Scopes from prompts.txt Phase 3. Distinct from `RoleName`
    (hermes_rpt.tenants.enums) — roles are "who you are within a tenant," scopes are
    "what this specific token is allowed to do." A token normally carries the scopes implied
    by its principal's role(s), but keeping them separate lets a token be issued with a
    narrower scope set than the principal's full role would allow (e.g. a short-lived,
    single-purpose service token)."""

    TENANT_READ = "tenant:read"
    TENANT_ADMIN = "tenant:admin"
    CONNECTION_MANAGE = "connection:manage"
    SCHEMA_DISCOVER = "schema:discover"
    MAPPING_MANAGE = "mapping:manage"
    PREDICTION_EXECUTE = "prediction:execute"
    MODEL_TRAIN = "model:train"
    MODEL_PROMOTE = "model:promote"
    AUDIT_READ = "audit:read"
    MONITORING_READ = "monitoring:read"


class PrincipalType(StrEnum):
    HUMAN = "human"
    SERVICE = "service"
