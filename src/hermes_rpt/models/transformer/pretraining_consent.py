"""Consent enforcement for shared (multi-tenant) pretraining (Phase 12).

"Pretraining datasets remain tenant-isolated by default. Shared pretraining requires an
explicit approved dataset class and consent record." Tenant-isolated pretraining (the default —
one tenant's own data, `hermes_rpt.models.transformer.pretraining_training.
HermesRPTPretrainingService.pretrain` with a single tenant) never calls this at all; it exists
only for the (not yet built) path where a pretraining run would combine more than one tenant's
data, and is a hard stop, not an advisory check.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from hermes_rpt.tenants.repository import DataUsageConsentRepository

SHARED_PRETRAINING_CONSENT_TYPE = "shared_pretraining"


class SharedPretrainingConsentError(Exception):
    def __init__(self, tenant_id: uuid.UUID) -> None:
        super().__init__(
            f"Tenant {tenant_id} has no granted {SHARED_PRETRAINING_CONSENT_TYPE!r} "
            "consent record — shared pretraining requires an explicit, approved consent "
            "record from every contributing tenant"
        )
        self.tenant_id = tenant_id


async def require_shared_pretraining_consent(
    session: AsyncSession, *, tenant_ids: Iterable[uuid.UUID]
) -> None:
    """Raises `SharedPretrainingConsentError` on the first tenant missing a `GRANTED`
    `shared_pretraining` consent row. Never partially proceeds — a shared pretraining run either
    has consent from every contributing tenant or it does not run at all."""

    consents = DataUsageConsentRepository(session)
    for tenant_id in tenant_ids:
        if not await consents.has_granted_consent(
            tenant_id, consent_type=SHARED_PRETRAINING_CONSENT_TYPE
        ):
            raise SharedPretrainingConsentError(tenant_id)
