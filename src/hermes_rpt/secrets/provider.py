"""`SecretProvider`: the only way any part of the platform stores or resolves a customer
database credential.

The control-plane database (`DatabaseCredentialReference.reference_key`) stores only an opaque
reference string a provider issued — never the credential itself
(docs/adr/0005-secret-provider-abstraction.md). `resolve()` is the single choke point every
connector goes through to get a usable secret at connection time; nothing else in the codebase
should hold onto a resolved value longer than it takes to open a connection.
"""

from __future__ import annotations

import asyncio
import uuid
from functools import lru_cache
from typing import Protocol


class SecretNotFoundError(Exception):
    def __init__(self, reference_key: str) -> None:
        # Never interpolate the reference_key's *value* space into anything logged as an
        # error a client could see beyond "not found" — the key itself is opaque, not secret,
        # so it's fine here, just not the resolved secret.
        super().__init__(f"No secret found for reference {reference_key!r}")


class SecretProvider(Protocol):
    """`store`/`delete` are control-plane operations (called from the connection lifecycle
    manager when registering, rotating, or deleting a connection). `resolve` is the only method
    the connector pool-creation path calls."""

    async def store(self, *, secret_value: str) -> str:
        """Persists `secret_value` somewhere the provider controls and returns an opaque
        reference string. The control plane stores only the returned reference."""
        ...

    async def resolve(self, reference_key: str) -> str:
        """Returns the actual secret value for a previously stored reference. Never logged,
        never returned through any API — callers use it only to open a connection."""
        ...

    async def delete(self, reference_key: str) -> None: ...


class LocalDevSecretProvider:
    """In-process, in-memory secret store for local development and tests.

    Secrets live only in this process's memory and are lost on restart — acceptable *only*
    because local development never touches real customer data (synthetic Alpha/Beta fixtures
    throughout). Not suitable for any shared or persistent environment; see
    `AzureKeyVaultSecretProvider` / `GoogleSecretManagerSecretProvider` below for the interfaces
    a real deployment implements instead.
    """

    def __init__(self) -> None:
        self._secrets: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def store(self, *, secret_value: str) -> str:
        reference_key = f"local-dev:{uuid.uuid4()}"
        async with self._lock:
            self._secrets[reference_key] = secret_value
        return reference_key

    async def resolve(self, reference_key: str) -> str:
        async with self._lock:
            try:
                return self._secrets[reference_key]
            except KeyError as exc:
                raise SecretNotFoundError(reference_key) from exc

    async def delete(self, reference_key: str) -> None:
        async with self._lock:
            self._secrets.pop(reference_key, None)


class AzureKeyVaultSecretProvider:
    """Interface reservation only (Phase 4 requirement) — not functional yet. A real
    implementation would use `azure-keyvault-secrets` + `azure-identity`, storing the vault
    secret name/version as the reference and resolving via the Key Vault client at connection
    time. Deliberately raises rather than silently behaving like the local provider."""

    async def store(self, *, secret_value: str) -> str:
        raise NotImplementedError("Azure Key Vault secret provider is not implemented yet")

    async def resolve(self, reference_key: str) -> str:
        raise NotImplementedError("Azure Key Vault secret provider is not implemented yet")

    async def delete(self, reference_key: str) -> None:
        raise NotImplementedError("Azure Key Vault secret provider is not implemented yet")


class GoogleSecretManagerSecretProvider:
    """Interface reservation only (Phase 4 requirement) — not functional yet. A real
    implementation would use `google-cloud-secret-manager`, storing the secret resource name as
    the reference."""

    async def store(self, *, secret_value: str) -> str:
        raise NotImplementedError("Google Secret Manager provider is not implemented yet")

    async def resolve(self, reference_key: str) -> str:
        raise NotImplementedError("Google Secret Manager provider is not implemented yet")

    async def delete(self, reference_key: str) -> None:
        raise NotImplementedError("Google Secret Manager provider is not implemented yet")


@lru_cache
def get_secret_provider() -> SecretProvider:
    """Process-wide singleton, chosen via `settings.secret_provider_backend` — "local" (the
    default, safe for local dev/CI/tests) or "google_secret_manager" (real persistent storage,
    required for any environment where a customer database connection needs to survive a
    container restart)."""

    from hermes_rpt.common.settings import get_settings

    settings = get_settings()
    if settings.secret_provider_backend == "google_secret_manager":  # noqa: S105 # nosec B105
        from hermes_rpt.secrets.google_provider import GoogleSecretManagerSecretProvider

        return GoogleSecretManagerSecretProvider(project_id=settings.gcp_project_id)
    return LocalDevSecretProvider()
