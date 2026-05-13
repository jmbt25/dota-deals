"""Environment-driven settings.

All env-var access in the codebase goes through :class:`Settings`. No other
module reads ``os.environ`` directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

type LogFormat = Literal["console", "json"]


class Settings(BaseSettings):
    """Runtime configuration loaded from ``.env`` and the process environment.

    Defaults match the values documented in ``docs/ARCHITECTURE.md`` and
    ``.env.example``. Field-level validators enforce reasonable ranges; a
    model-level validator enforces that the ingest cadence divides 24 evenly
    so polling slots align to a fixed pattern within a UTC day.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    db_path: Path = Field(default=Path("./data/dota_deals.db"))

    steam_concurrency: int = Field(default=2, ge=1, le=10)
    request_timeout_s: float = Field(default=15.0, gt=0.0)
    cooldown_429_s: float = Field(default=60.0, gt=0.0)
    steam_currency_id: int = Field(default=1)
    steam_country: str = Field(default="US", min_length=2, max_length=2)

    ingest_cadence_hours: int = Field(default=8, ge=1, le=24)

    log_format: LogFormat = Field(default="console")

    # Cloudflare R2 (S3-compatible). All optional — when unset the R2 client
    # raises a clear configuration error rather than guessing at endpoints.
    r2_endpoint: str | None = Field(default=None)
    r2_bucket: str | None = Field(default=None)
    r2_access_key_id: str | None = Field(default=None)
    r2_secret_access_key: str | None = Field(default=None)
    r2_db_key: str = Field(default="dota_deals.db")

    # Cloudflare D1 (HTTP REST). All four fields below must be set together
    # for the D1 client to start. The client raises :class:`D1ConfigError`
    # if any are missing at construction time — keeping the failure at the
    # configuration boundary rather than the first request.
    cloudflare_account_id: str | None = Field(default=None)
    cloudflare_d1_database_id: str | None = Field(default=None)
    cloudflare_d1_api_token: str | None = Field(default=None)

    # D1 request timeout, seconds. Default 30s — D1 latency p99 is well under
    # this; the headroom is for cold-start cases and pathological queries.
    d1_timeout_s: float = Field(default=30.0, gt=0.0)

    # Max statements per batch request. D1's documented practical batch
    # ceiling is around 100; staying well under it amortizes HTTP latency
    # without risking the request size cap.
    d1_max_batch_size: int = Field(default=100, ge=1, le=100)

    @model_validator(mode="after")
    def _check_cadence_divides_day(self) -> Self:
        """Polling slots must align to a fixed pattern within a UTC day."""
        if 24 % self.ingest_cadence_hours != 0:
            raise ValueError(
                f"ingest_cadence_hours={self.ingest_cadence_hours} does not divide "
                "24 evenly; pick one of 1, 2, 3, 4, 6, 8, 12, 24."
            )
        return self


def load_settings() -> Settings:
    """Return populated :class:`Settings`.

    Reads ``.env`` from the current working directory and overlays any values
    set in the process environment.

    :raises pydantic.ValidationError: if a value fails type or range validation.
    """
    return Settings()
