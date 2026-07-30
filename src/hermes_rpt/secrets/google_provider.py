"""Google Secret Manager-backed `SecretProvider` — the production implementation.

Each `store()` call creates a brand-new Secret Manager secret (one secret per stored
credential, never a shared reference) with a single version holding the value; the returned
reference key is that secret's short ID. `resolve()` always reads the `latest` version;
`delete()` removes the secret (and every version) entirely, mirroring
`LocalDevSecretProvider`'s delete-makes-it-unresolvable semantics.

Requires the running service's IAM identity to hold `roles/secretmanager.admin` (or an
equivalent custom role covering create/access/delete) on `settings.gcp_project_id` — Secret
Manager has no per-resource ACL to scope at creation time the way per-connection secrets would
ideally want, so this is a project-wide grant, the same trust level the service already has over
its own control-plane database credentials.
"""

from __future__ import annotations

import contextlib
import uuid

from google.api_core.exceptions import NotFound
from google.cloud.secretmanager_v1 import SecretManagerServiceAsyncClient

from hermes_rpt.secrets.provider import SecretNotFoundError

_SECRET_ID_PREFIX = "hermes-rpt-conn-"  # noqa: S105 # nosec B105 - a name prefix, not a password


class GoogleSecretManagerSecretProvider:
    def __init__(
        self, *, project_id: str, client: SecretManagerServiceAsyncClient | None = None
    ) -> None:
        self._project_id = project_id
        self._client = client or SecretManagerServiceAsyncClient()

    def _secret_name(self, secret_id: str) -> str:
        return f"projects/{self._project_id}/secrets/{secret_id}"

    async def store(self, *, secret_value: str) -> str:
        secret_id = f"{_SECRET_ID_PREFIX}{uuid.uuid4()}"
        await self._client.create_secret(
            request={
                "parent": f"projects/{self._project_id}",
                "secret_id": secret_id,
                "secret": {"replication": {"automatic": {}}},
            }
        )
        await self._client.add_secret_version(
            request={
                "parent": self._secret_name(secret_id),
                "payload": {"data": secret_value.encode("utf-8")},
            }
        )
        return secret_id

    async def resolve(self, reference_key: str) -> str:
        try:
            response = await self._client.access_secret_version(
                request={"name": f"{self._secret_name(reference_key)}/versions/latest"}
            )
        except NotFound as exc:
            raise SecretNotFoundError(reference_key) from exc
        return response.payload.data.decode("utf-8")

    async def delete(self, reference_key: str) -> None:
        # already gone -> no-op: delete is idempotent, matching LocalDevSecretProvider
        with contextlib.suppress(NotFound):
            await self._client.delete_secret(request={"name": self._secret_name(reference_key)})
