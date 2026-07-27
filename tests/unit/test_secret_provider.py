"""LocalDevSecretProvider round-trip and error-handling tests."""

from __future__ import annotations

import pytest

from hermes_rpt.secrets.provider import LocalDevSecretProvider, SecretNotFoundError


async def test_store_and_resolve_round_trip() -> None:
    provider = LocalDevSecretProvider()
    reference = await provider.store(secret_value="s3cr3t-password")
    assert await provider.resolve(reference) == "s3cr3t-password"


async def test_reference_key_does_not_contain_the_secret_value() -> None:
    provider = LocalDevSecretProvider()
    reference = await provider.store(secret_value="s3cr3t-password")
    assert "s3cr3t-password" not in reference


async def test_resolve_unknown_reference_raises() -> None:
    provider = LocalDevSecretProvider()
    with pytest.raises(SecretNotFoundError):
        await provider.resolve("local-dev:does-not-exist")


async def test_delete_makes_the_reference_unresolvable() -> None:
    provider = LocalDevSecretProvider()
    reference = await provider.store(secret_value="s3cr3t-password")
    await provider.delete(reference)
    with pytest.raises(SecretNotFoundError):
        await provider.resolve(reference)


async def test_delete_is_idempotent() -> None:
    provider = LocalDevSecretProvider()
    await provider.delete("local-dev:never-existed")  # must not raise
