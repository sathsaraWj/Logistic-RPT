"""External partner service credentials — how a system outside Hermes-RPT (e.g. Hermes VMS's
own backend) authenticates without a human login, in a way that's independently revocable per
integration rather than tied to the platform's single global JWT signing secret. See
`docs/AUTHENTICATION.md` §5a for the full design and `hermes_rpt.auth.service.
ServiceCredentialService` for the issuance/exchange/revocation logic that owns this table.

`client_id` is the public, non-secret half — safe to log, reference in support tickets, or
show in a UI list. `secret_hash` is a plain `hashlib.sha256` digest of the secret half, not a
slow adaptive hash (bcrypt/argon2/passlib): the secret is 256 bits of server-generated
`secrets.token_urlsafe(32)` entropy, not a low-entropy human-chosen password. A slow KDF exists
to defend against offline dictionary/rainbow-table attacks on *guessable* inputs — that threat
doesn't apply to a value this random, and a slow hash would only add latency to the hot
token-exchange path for no corresponding security benefit.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from hermes_rpt.common.db import Base
from hermes_rpt.common.mixins import TenantOwnedMixin, TimestampMixin, UUIDPKMixin


class ServiceCredential(Base, UUIDPKMixin, TimestampMixin, TenantOwnedMixin):
    __tablename__ = "service_credentials"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # Globally unique (not just tenant-scoped) — resolving which tenant a credential belongs to
    # is the entire point of the token-exchange lookup, so it can't itself be tenant-scoped.
    # See ServiceCredentialRepository.find_by_client_id.
    client_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)  # hex SHA-256 digest
    scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
