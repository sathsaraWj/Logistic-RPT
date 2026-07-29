"""Issuance, exchange, and revocation of `ServiceCredential` rows — how an external partner
(e.g. Hermes VMS's backend) authenticates to Hermes-RPT without a human login. See
`docs/AUTHENTICATION.md` §5a for the full design; `hermes_rpt.auth.models.ServiceCredential`
for the persisted shape and why `secret_hash` is plain SHA-256, not a slow adaptive hash.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from hermes_rpt.audit.enums import AuditOutcome
from hermes_rpt.audit.service import AuditService
from hermes_rpt.auth.enums import ScopeName
from hermes_rpt.auth.errors import AuthenticationError
from hermes_rpt.auth.models import ServiceCredential
from hermes_rpt.auth.repository import ServiceCredentialRepository
from hermes_rpt.common.settings import Settings
from hermes_rpt.tenants.context import TenantContext

# A machine credential must never be able to mint or manage other credentials, or perform
# tenant-membership governance — both materially higher-blast-radius than anything a
# service-to-service integration legitimately needs.
_GRANTABLE_SCOPES = frozenset(ScopeName) - {
    ScopeName.TENANT_ADMIN,
    ScopeName.SERVICE_CREDENTIAL_MANAGE,
}

# Fixed-length placeholder compared against on an unknown client_id, so "no such client" and
# "wrong secret" take roughly the same amount of time — reduces, doesn't eliminate, the timing
# side channel (same residual-risk framing docs/THREAT_MODEL.md §5 already uses elsewhere).
_DUMMY_SECRET_HASH = hashlib.sha256(b"hermes-rpt-dummy-comparison-target").hexdigest()


class UngrantableScopeError(Exception):
    def __init__(self, requested: Iterable[str]) -> None:
        self.requested = frozenset(requested)
        super().__init__(
            f"Scope(s) not grantable to a service credential: {sorted(self.requested)}"
        )


class ExpiryInThePastError(Exception):
    def __init__(self, expires_at: datetime) -> None:
        self.expires_at = expires_at
        super().__init__(f"expires_at {expires_at.isoformat()} is already in the past")


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


class ServiceCredentialService:
    def __init__(self, session: AsyncSession, *, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._credentials = ServiceCredentialRepository(session)
        self._audit = AuditService(session)

    async def issue_credential(
        self,
        *,
        tenant_context: TenantContext,
        name: str,
        scopes: Iterable[str],
        expires_at: datetime | None = None,
    ) -> tuple[ServiceCredential, str]:
        requested = frozenset(scopes)
        ungrantable = requested - _GRANTABLE_SCOPES
        if ungrantable:
            raise UngrantableScopeError(ungrantable)
        if expires_at is not None and expires_at <= datetime.now(UTC):
            raise ExpiryInThePastError(expires_at)

        client_secret = secrets.token_urlsafe(32)
        # 18 bytes (144 bits) of entropy — a collision is astronomically unlikely, on the same
        # order as a UUID4 collision, which the rest of this codebase also never retries around.
        # A retry-on-collision loop would need a mid-transaction session rollback to recover
        # from the resulting IntegrityError, which is unsafe here: this service runs inside a
        # single request-scoped session that the router commits once at the end, and a rollback
        # would silently discard any other work already pending in that same request.
        credential = ServiceCredential(
            tenant_id=tenant_context.tenant_id,
            name=name,
            client_id=f"hrpt_svc_{secrets.token_urlsafe(18)}",
            secret_hash=_hash_secret(client_secret),
            scopes=sorted(requested),
            expires_at=expires_at,
        )
        credential = await self._credentials.add(credential, tenant_context=tenant_context)

        await self._audit.record(
            action="service_credential.create",
            outcome=AuditOutcome.SUCCESS,
            tenant_id=tenant_context.tenant_id,
            principal_id=tenant_context.principal_id,
            principal_type=tenant_context.principal_type,
            resource_type="ServiceCredential",
            resource_id=credential.id,
            correlation_id=tenant_context.correlation_id,
            details={"name": name, "scopes": sorted(requested)},
        )
        return credential, client_secret

    async def authenticate(self, *, client_id: str, client_secret: str) -> ServiceCredential:
        credential = await self._credentials.find_by_client_id(client_id)
        candidate_hash = _hash_secret(client_secret)

        if credential is None:
            hmac.compare_digest(candidate_hash, _DUMMY_SECRET_HASH)  # burn comparable time
            await self._audit.record(
                action="auth.service_token_exchange",
                outcome=AuditOutcome.DENIED,
                principal_type="service_credential",
                details={"reason": "unknown_client_id"},
            )
            raise AuthenticationError("service_credential_not_found")

        if not hmac.compare_digest(candidate_hash, credential.secret_hash):
            await self._audit.record(
                action="auth.service_token_exchange",
                outcome=AuditOutcome.DENIED,
                tenant_id=credential.tenant_id,
                principal_id=credential.id,
                principal_type="service_credential",
                resource_type="ServiceCredential",
                resource_id=credential.id,
                details={"reason": "wrong_secret"},
            )
            raise AuthenticationError("service_credential_wrong_secret")

        if not credential.is_active:
            await self._audit.record(
                action="auth.service_token_exchange",
                outcome=AuditOutcome.DENIED,
                tenant_id=credential.tenant_id,
                principal_id=credential.id,
                principal_type="service_credential",
                resource_type="ServiceCredential",
                resource_id=credential.id,
                details={"reason": "revoked"},
            )
            raise AuthenticationError("service_credential_revoked")

        now = datetime.now(UTC)
        if credential.expires_at is not None and credential.expires_at <= now:
            await self._audit.record(
                action="auth.service_token_exchange",
                outcome=AuditOutcome.DENIED,
                tenant_id=credential.tenant_id,
                principal_id=credential.id,
                principal_type="service_credential",
                resource_type="ServiceCredential",
                resource_id=credential.id,
                details={"reason": "expired"},
            )
            raise AuthenticationError("service_credential_expired")

        credential.last_used_at = now
        await self._audit.record(
            action="auth.service_token_exchange",
            outcome=AuditOutcome.SUCCESS,
            tenant_id=credential.tenant_id,
            principal_id=credential.id,
            principal_type="service_credential",
            resource_type="ServiceCredential",
            resource_id=credential.id,
        )
        return credential

    async def revoke(
        self, credential_id: uuid.UUID, *, tenant_context: TenantContext
    ) -> ServiceCredential:
        credential = await self._credentials.require(credential_id, tenant_context=tenant_context)
        credential.is_active = False
        credential.revoked_at = datetime.now(UTC)
        await self._audit.record(
            action="service_credential.revoke",
            outcome=AuditOutcome.SUCCESS,
            tenant_id=tenant_context.tenant_id,
            principal_id=tenant_context.principal_id,
            principal_type=tenant_context.principal_type,
            resource_type="ServiceCredential",
            resource_id=credential.id,
            correlation_id=tenant_context.correlation_id,
        )
        return credential

    async def list_for_tenant(self, *, tenant_context: TenantContext) -> list[ServiceCredential]:
        return await self._credentials.list_for_tenant(tenant_context=tenant_context)
