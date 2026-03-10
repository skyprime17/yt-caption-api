from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "YT Caption API"
    app_version: str = "0.1.0"
    cookies_file: Path = Field(default_factory=lambda: PROJECT_ROOT / "cookies.txt")
    cache_dir: Path = Field(default_factory=lambda: PROJECT_ROOT / "cache")
    cache_ttl_seconds: int = 7 * 24 * 60 * 60
    cache_cleanup_interval_seconds: int = 6 * 60 * 60
    yt_dlp_js_runtime: str = "node"
    yt_dlp_remote_component: str = "ejs:github"
    curl_impersonate: str = "chrome"
    resolve_url_timeout_seconds: int = 20
    caption_fetch_timeout_seconds: int = 30


@lru_cache
def get_settings() -> Settings:
    return Settings()
