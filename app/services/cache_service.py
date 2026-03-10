from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from app.config import Settings


class CacheService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def prune(self) -> None:
        cache_dir = self.settings.cache_dir
        if not cache_dir.exists():
            return

        cutoff = time.time() - self.settings.cache_ttl_seconds
        for cache_file in cache_dir.glob("*.json"):
            try:
                if cache_file.stat().st_mtime < cutoff:
                    cache_file.unlink()
            except OSError:
                continue

    def load(
        self,
        video_id: str,
        language: str | None,
        include_auto: bool,
    ) -> dict[str, Any] | None:
        cache_file = self._cache_file(video_id, language, include_auto)
        if not cache_file.exists():
            return None

        try:
            if cache_file.stat().st_mtime < time.time() - self.settings.cache_ttl_seconds:
                cache_file.unlink()
                return None
        except OSError:
            return None

        try:
            return json.loads(cache_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def save(
        self,
        video_id: str,
        language: str | None,
        include_auto: bool,
        payload: dict[str, Any],
    ) -> None:
        self.prune()
        self.settings.cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = self._cache_file(video_id, language, include_auto)
        cache_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def _cache_file(self, video_id: str, language: str | None, include_auto: bool) -> Path:
        normalized_language = (language or "default").lower()
        auto_flag = "auto" if include_auto else "manual"
        safe_video_id = re.sub(r"[^A-Za-z0-9_-]", "_", video_id)
        safe_language = re.sub(r"[^A-Za-z0-9_-]", "_", normalized_language)
        return self.settings.cache_dir / f"{safe_video_id}.{safe_language}.{auto_flag}.json"
