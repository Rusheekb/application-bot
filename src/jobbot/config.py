"""Process-level configuration read from environment variables (and an optional .env file).

Secrets and deployment settings live here. Everything a user tunes day to day (filters, AI and
auto-apply options, notifications) lives in the database and is edited from the web UI instead.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Env(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="JOBBOT_", extra="ignore")

    data_dir: Path = Path("data")
    host: str = "127.0.0.1"
    port: int = 8000
    # When set, every page requires HTTP basic auth with this password (any username).
    password: str | None = None
    # Extra Host header values to accept besides localhost (comma separated), e.g. "jobs.example.com".
    allowed_hosts: str = ""
    # Public URL of the web UI, used for links in notifications.
    base_url: str = ""
    # Use a specific Chrome/Chromium binary for the auto-apply browser.
    chromium_path: str | None = None
    log_level: str = "INFO"

    adzuna_app_id: str | None = Field(
        None, validation_alias=AliasChoices("ADZUNA_APP_ID", "JOBBOT_ADZUNA_APP_ID")
    )
    adzuna_app_key: str | None = Field(
        None, validation_alias=AliasChoices("ADZUNA_APP_KEY", "JOBBOT_ADZUNA_APP_KEY")
    )
    jsearch_api_key: str | None = Field(
        None, validation_alias=AliasChoices("JSEARCH_API_KEY", "RAPIDAPI_KEY", "JOBBOT_JSEARCH_API_KEY")
    )

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jobbot.db"

    @property
    def resume_dir(self) -> Path:
        return self.data_dir / "resume"

    @property
    def screenshot_dir(self) -> Path:
        return self.data_dir / "screenshots"

    @property
    def documents_dir(self) -> Path:
        return self.data_dir / "documents"

    @property
    def extra_allowed_hosts(self) -> list[str]:
        return [h.strip() for h in self.allowed_hosts.split(",") if h.strip()]

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.resume_dir, self.screenshot_dir, self.documents_dir):
            d.mkdir(parents=True, exist_ok=True)


def anthropic_credentials_present() -> bool:
    """True when the Anthropic SDK has a credential source it can resolve on its own."""
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    if os.environ.get("ANTHROPIC_PROFILE"):
        return True
    # `ant auth login` stores profiles here; the SDK picks them up automatically.
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return (config_home / "anthropic").is_dir()


@lru_cache(maxsize=1)
def get_env() -> Env:
    load_dotenv(override=False)
    return Env()
