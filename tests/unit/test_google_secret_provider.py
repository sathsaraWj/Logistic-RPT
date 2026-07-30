"""GoogleSecretManagerSecretProvider tests — against a mocked async client, never real GCP."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from google.api_core.exceptions import NotFound

from hermes_rpt.secrets.google_provider import GoogleSecretManagerSecretProvider
from hermes_rpt.secrets.provider import SecretNotFoundError


def _provider(client: AsyncMock) -> GoogleSecretManagerSecretProvider:
    return GoogleSecretManagerSecretProvider(project_id="test-project", client=client)


async def test_store_creates_a_secret_and_adds_a_version() -> None:
    client = AsyncMock()
    provider = _provider(client)

    reference = await provider.store(secret_value="s3cr3t-password")

    assert reference.startswith("hermes-rpt-conn-")
    client.create_secret.assert_awaited_once()
    create_kwargs = client.create_secret.await_args.kwargs["request"]
    assert create_kwargs["parent"] == "projects/test-project"
    assert create_kwargs["secret_id"] == reference

    client.add_secret_version.assert_awaited_once()
    version_kwargs = client.add_secret_version.await_args.kwargs["request"]
    assert version_kwargs["parent"] == f"projects/test-project/secrets/{reference}"
    assert version_kwargs["payload"]["data"] == b"s3cr3t-password"


async def test_store_generates_a_distinct_reference_each_call() -> None:
    client = AsyncMock()
    provider = _provider(client)

    first = await provider.store(secret_value="a")
    second = await provider.store(secret_value="b")

    assert first != second


async def test_resolve_returns_the_stored_value() -> None:
    client = AsyncMock()
    client.access_secret_version.return_value.payload.data = b"s3cr3t-password"
    provider = _provider(client)

    value = await provider.resolve("hermes-rpt-conn-abc")

    assert value == "s3cr3t-password"
    client.access_secret_version.assert_awaited_once_with(
        request={"name": "projects/test-project/secrets/hermes-rpt-conn-abc/versions/latest"}
    )


async def test_resolve_unknown_reference_raises_secret_not_found() -> None:
    client = AsyncMock()
    client.access_secret_version.side_effect = NotFound("no such secret")
    provider = _provider(client)

    with pytest.raises(SecretNotFoundError):
        await provider.resolve("hermes-rpt-conn-does-not-exist")


async def test_delete_removes_the_secret() -> None:
    client = AsyncMock()
    provider = _provider(client)

    await provider.delete("hermes-rpt-conn-abc")

    client.delete_secret.assert_awaited_once_with(
        request={"name": "projects/test-project/secrets/hermes-rpt-conn-abc"}
    )


async def test_delete_is_idempotent_when_already_gone() -> None:
    client = AsyncMock()
    client.delete_secret.side_effect = NotFound("no such secret")
    provider = _provider(client)

    await provider.delete("hermes-rpt-conn-already-gone")  # must not raise
