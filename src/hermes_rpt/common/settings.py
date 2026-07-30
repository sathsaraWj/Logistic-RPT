"""Typed application settings.

All configuration is read from environment variables (optionally via a local `.env` file in
development). Required variables fail startup immediately with a clear error rather than
letting the process start in a half-configured state — see `get_settings()`.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Only ever a valid default outside staging/production — see Settings._reject_insecure_jwt_secret.
_INSECURE_DEV_JWT_SECRET = "local-dev-only-insecure-secret-do-not-use-in-production"  # noqa: S105 # nosec B105


class Environment(StrEnum):
    LOCAL = "local"
    CI = "ci"
    STAGING = "staging"
    PRODUCTION = "production"


class Settings(BaseSettings):
    """Process-wide configuration, shared by apps/api, apps/worker and apps/trainer.

    Values with no default are required: startup fails fast (via pydantic validation) if
    they are missing, rather than the app coming up and failing confusingly on first use.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    environment: Environment = Environment.LOCAL
    service_name: str = "hermes-rpt"

    # Control-plane database. Never used for customer (tenant) data — see
    # docs/ARCHITECTURE.md §1 "Two planes" and src/hermes_rpt/connectors for tenant connections.
    #
    # Plain `str`, not `PostgresDsn`: pydantic's URL parser rejects the empty-host form
    # (`postgresql+asyncpg://user:pass@/dbname?host=/cloudsql/...`) that SQLAlchemy's asyncpg
    # dialect uses for Unix-domain-socket connections — a real, standard Postgres connection
    # shape (Cloud SQL's native connector, or any local Unix socket), not a malformed one. The
    # `_validate_database_url` validator below still fails fast on obviously-wrong input.
    database_url: str = Field(
        default="postgresql+asyncpg://hermes:hermes@localhost:5432/hermes_control"
    )

    log_level: str = "INFO"
    log_json: bool = True

    mlflow_tracking_uri: str = "http://localhost:5000"

    # Independent from `environment` on purpose — some deployments run with
    # ENVIRONMENT=local for reasons unrelated to secret storage (e.g. to enable
    # `issue_dev_token`), and coupling this choice to that flag would make one setting silently
    # control the other. See hermes_rpt.secrets.provider.get_secret_provider.
    secret_provider_backend: str = "local"  # noqa: S105 # nosec B105 - backend name, not a password
    gcp_project_id: str = ""

    cors_allowed_origins: list[str] = Field(default_factory=list)

    request_id_header: str = "X-Request-ID"
    correlation_id_header: str = "X-Correlation-ID"

    # --- Authentication (Phase 3) -----------------------------------------------------------
    # HS256 with a shared secret is the *local development* token verifier
    # (hermes_rpt.auth.verifier.LocalDevTokenVerifier). Production deployments are expected to
    # swap in an OIDC/JWKS-based TokenVerifier (RS256, no shared secret) — see
    # docs/adr/0010-jwt-local-dev-provider.md. Never a custom cryptographic algorithm either
    # way (PyJWT only).
    jwt_secret_key: SecretStr = Field(default=SecretStr(_INSECURE_DEV_JWT_SECRET))
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "https://auth.hermes-rpt.local/dev"
    jwt_audience: str = "hermes-rpt-api"
    jwt_leeway_seconds: int = 30
    access_token_ttl_seconds: int = 900
    service_token_ttl_seconds: int = 3600

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        normalised = value.upper()
        if normalised not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}, got {value!r}")
        return normalised

    @field_validator("database_url")
    @classmethod
    def _validate_database_url(cls, value: str) -> str:
        if not value.startswith(("postgresql://", "postgresql+asyncpg://")):
            raise ValueError(
                "database_url must start with 'postgresql://' or 'postgresql+asyncpg://', "
                f"got {value!r}"
            )
        return value

    @field_validator("secret_provider_backend")
    @classmethod
    def _validate_secret_provider_backend(cls, value: str) -> str:
        allowed = {"local", "google_secret_manager"}
        if value not in allowed:
            raise ValueError(
                f"secret_provider_backend must be one of {sorted(allowed)}, got {value!r}"
            )
        return value

    @field_validator("jwt_algorithm")
    @classmethod
    def _validate_jwt_algorithm(cls, value: str) -> str:
        """A Phase 16 security review noted this field was an unconstrained `str` — PyJWT's own
        `NoneAlgorithm` currently rejects `alg=none` when a signing key is supplied, so nothing
        was actually exploitable today, but that safety was an assumption about PyJWT's
        behavior, not something this codebase enforced itself. Every other algorithm-shaped
        setting in this class (`log_level`, `database_url`) has a validator; this one should
        too."""

        allowed = {"HS256", "HS384", "HS512", "RS256", "RS384", "RS512", "ES256", "ES384"}
        if value not in allowed:
            raise ValueError(f"jwt_algorithm must be one of {sorted(allowed)}, got {value!r}")
        return value

    @model_validator(mode="after")
    def _reject_insecure_jwt_secret_outside_dev(self) -> Settings:
        if (
            self.environment in (Environment.STAGING, Environment.PRODUCTION)
            and self.jwt_secret_key.get_secret_value() == _INSECURE_DEV_JWT_SECRET
        ):
            raise ValueError(
                "JWT_SECRET_KEY must be set explicitly outside local/CI environments — "
                "refusing to start with the insecure development default (fail closed)."
            )
        return self

    @model_validator(mode="after")
    def _require_gcp_project_id_for_google_secret_manager(self) -> Settings:
        if (
            self.secret_provider_backend == "google_secret_manager"  # noqa: S105 # nosec B105
            and not self.gcp_project_id
        ):
            raise ValueError(
                "GCP_PROJECT_ID is required when SECRET_PROVIDER_BACKEND=google_secret_manager"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor. Raising here (via pydantic) at first access — which happens
    during app startup, not lazily mid-request — is the "validate required environment
    variables at startup" requirement from Phase 1."""

    return Settings()
