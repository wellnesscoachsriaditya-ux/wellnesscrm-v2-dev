"""Typed application configuration.

NFR-075: identical configuration *shape* across environments; only values differ.
NFR-034: secrets come from the environment, never from source control.

This module is the only place environment variables are read. Everything else
receives configuration through :func:`get_settings`, which makes configuration a
declared dependency rather than an ambient global — and makes it trivially
overridable in tests.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    """Deployment environment. Drives fail-safe defaults, not feature flags."""

    LOCAL = "local"
    STAGING = "staging"
    PRODUCTION = "production"

    @property
    def is_production_like(self) -> bool:
        """Staging is treated as production for safety checks.

        Staging holds no real clinical data (Arch §16.3), but it must exercise
        the same guarantees, or the checks are never really tested.
        """
        return self in (Environment.STAGING, Environment.PRODUCTION)


class Settings(BaseSettings):
    """Application settings, validated at startup.

    Invalid configuration fails immediately and loudly. A misconfigured
    production instance that boots is far more dangerous than one that does not.
    """

    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ─── Application ─────────────────────────────────────────────────────
    app_env: Environment = Environment.LOCAL
    app_debug: bool = True
    app_base_url: str = "http://localhost:8000"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # ─── Database ────────────────────────────────────────────────────────
    database_url: SecretStr = SecretStr(
        "postgresql+psycopg://app_user:postgres@localhost:5432/wellnesscrm"
    )
    database_migration_url: SecretStr | None = None
    database_pool_size: int = Field(default=10, ge=1, le=50)
    database_max_overflow: int = Field(default=5, ge=0, le=50)

    # ─── Identity ────────────────────────────────────────────────────────
    # Arch §6.1: three separate signing keys, one per realm. A practitioner
    # token presented to an operator endpoint must fail *signature verification*,
    # not merely a claim check — realm confusion becomes cryptographically
    # impossible rather than a conditional someone can forget.
    jwt_secret_practitioner: SecretStr = SecretStr("dev-only-practitioner-key")
    jwt_secret_client: SecretStr = SecretStr("dev-only-client-key")
    jwt_secret_operator: SecretStr = SecretStr("dev-only-operator-key")

    access_token_ttl_minutes: int = Field(default=15, ge=1, le=60)
    refresh_token_ttl_days: int = Field(default=30, ge=1, le=90)
    # Approved refinement: magic links expire in 15–30 minutes. Short enough that
    # a forwarded link is near-useless; short enough that self-service re-request
    # (EC-M7-01) is a routine path, not an error path.
    magic_link_ttl_minutes: int = Field(default=20, ge=5, le=30)

    # ─── Supabase (infrastructure, not a backend — ADR-02) ───────────────
    supabase_url: str | None = None
    supabase_anon_key: SecretStr | None = None
    supabase_service_key: SecretStr | None = None
    supabase_storage_bucket: str = "wellnesscrm-files"

    # ─── Worker ──────────────────────────────────────────────────────────
    worker_poll_interval_seconds: int = Field(default=60, ge=1, le=300)
    worker_concurrency: int = Field(default=1, ge=1, le=16)
    worker_lease_multiplier: int = Field(default=2, ge=2, le=10)

    # ─── Observability ───────────────────────────────────────────────────
    sentry_dsn: str | None = None
    sentry_traces_sample_rate: float = Field(default=0.1, ge=0.0, le=1.0)

    # ─── Region ──────────────────────────────────────────────────────────
    # FR-M0-014: regional compliance is configuration, never hardcoded logic.
    default_region_code: str = "IN"
    default_timezone: str = "Asia/Kolkata"
    default_currency_code: str = "INR"

    @field_validator("app_base_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @model_validator(mode="after")
    def _enforce_production_safety(self) -> Settings:
        """Refuse to start with unsafe production configuration.

        These are the settings whose accidental persistence into production
        would be most damaging, and least visible.
        """
        if not self.app_env.is_production_like:
            return self

        problems: list[str] = []

        if self.app_debug:
            problems.append("APP_DEBUG must be false outside local development")

        dev_key_prefix = "dev-only-"
        for name, secret in (
            ("JWT_SECRET_PRACTITIONER", self.jwt_secret_practitioner),
            ("JWT_SECRET_CLIENT", self.jwt_secret_client),
            ("JWT_SECRET_OPERATOR", self.jwt_secret_operator),
        ):
            if secret.get_secret_value().startswith(dev_key_prefix):
                problems.append(f"{name} is still the development placeholder")

        # Arch §6.1 — distinct keys are the mechanism, so identical keys silently
        # collapse three realms into one.
        realm_keys = {
            self.jwt_secret_practitioner.get_secret_value(),
            self.jwt_secret_client.get_secret_value(),
            self.jwt_secret_operator.get_secret_value(),
        }
        if len(realm_keys) != 3:
            problems.append(
                "the three realm signing keys must all differ "
                "(identical keys defeat realm separation — Arch §6.1)"
            )

        if not self.app_base_url.startswith("https://"):
            problems.append("APP_BASE_URL must use HTTPS outside local development")

        if problems:
            raise ValueError(
                "Unsafe configuration for " f"{self.app_env.value}:\n  - " + "\n  - ".join(problems)
            )

        return self

    @property
    def migration_url(self) -> str:
        """DDL connection string.

        Falls back to the application URL locally, where one role is acceptable.
        In production these are distinct roles: `app_user` has no DDL rights and
        no BYPASSRLS (DB §2.4).
        """
        if self.database_migration_url is not None:
            return self.database_migration_url.get_secret_value()
        return self.database_url.get_secret_value()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached settings singleton.

    Cached because configuration is immutable for a process lifetime. Tests
    override by calling ``get_settings.cache_clear()`` after patching the
    environment.
    """
    return Settings()
