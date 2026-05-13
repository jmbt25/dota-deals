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
