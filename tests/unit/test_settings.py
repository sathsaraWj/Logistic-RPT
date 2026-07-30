"""Settings validation tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hermes_rpt.common.settings import Environment, Settings


def test_defaults_are_usable_for_local_dev() -> None:
    settings = Settings()
    assert settings.environment == Environment.LOCAL
    assert settings.log_level == "INFO"
    assert str(settings.database_url).startswith("postgresql+asyncpg://")


def test_invalid_log_level_fails_fast() -> None:
    with pytest.raises(ValidationError):
        Settings(log_level="NOT_A_LEVEL")


def test_log_level_is_case_insensitive() -> None:
    settings = Settings(log_level="debug")
    assert settings.log_level == "DEBUG"


def test_algorithm_none_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(jwt_algorithm="none")


def test_an_unrecognised_jwt_algorithm_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(jwt_algorithm="not-a-real-algorithm")


def test_an_unrecognised_secret_provider_backend_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(secret_provider_backend="not-a-real-backend")


def test_google_secret_manager_backend_requires_a_project_id() -> None:
    with pytest.raises(ValidationError):
        Settings(secret_provider_backend="google_secret_manager", gcp_project_id="")


def test_google_secret_manager_backend_with_project_id_is_valid() -> None:
    settings = Settings(secret_provider_backend="google_secret_manager", gcp_project_id="p")
    assert settings.secret_provider_backend == "google_secret_manager"
