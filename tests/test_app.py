from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.services.cache_service import CacheService
from app.services.transcript_service import TranscriptService


class FakeTranscriptService:
    def __init__(self) -> None:
        self.calls = 0

    def get_transcript_payload(
        self,
        video_id: str,
        language: str | None,
        include_auto: bool,
        use_cache: bool,
    ):
        self.calls += 1
        cache_status = "MISS" if self.calls == 1 else "HIT"
        return (
            {
                "id": video_id,
                "title": "Example",
                "uploader": "Uploader",
                "webpage_url": f"https://www.youtube.com/watch?v={video_id}",
                "language": language or "en",
                "source": "subtitles",
                "extension": "json3",
                "transcript": "abcdefghijklmnopqrstuvwxyz",
            },
            cache_status,
        )

    def shape_response(self, payload: dict, include_meta: bool, max_chars: int | None):
        transcript = payload["transcript"]
        if max_chars is not None:
            transcript = transcript[:max_chars]
        if include_meta:
            return {**payload, "transcript": transcript}
        return {
            "id": payload["id"],
            "title": None,
            "uploader": None,
            "webpage_url": None,
            "language": payload["language"],
            "source": None,
            "extension": None,
            "transcript": transcript,
        }


def make_settings(tmp_path: Path, cookies_exists: bool = True) -> Settings:
    cookies_file = tmp_path / "cookies.txt"
    if cookies_exists:
        cookies_file.write_text("cookie-data", encoding="utf-8")
    return Settings(
        cookies_file=cookies_file,
        cache_dir=tmp_path / "cache",
        cache_cleanup_interval_seconds=1,
    )


def test_startup_fails_without_cookies(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path, cookies_exists=False))
    with pytest.raises(RuntimeError, match="Missing cookies file"):
        with TestClient(app):
            pass


def test_transcript_route_returns_cache_headers_and_reduced_shape(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path))
    fake_service = FakeTranscriptService()

    with TestClient(app) as client:
        app.state.transcript_service = fake_service

        response_one = client.get(
            "/transcript/demo123",
            params={"language": "en", "include_meta": "false", "max_chars": 5},
        )
        assert response_one.status_code == 200
        assert response_one.headers["X-Cache"] == "MISS"
        assert response_one.json() == {
            "id": "demo123",
            "language": "en",
            "transcript": "abcde",
        }

        response_two = client.get(
            "/transcript/demo123",
            params={"language": "en", "include_meta": "false", "max_chars": 5},
        )
        assert response_two.status_code == 200
        assert response_two.headers["X-Cache"] == "HIT"


def test_cache_service_ignores_and_prunes_expired_entries(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings.cache_ttl_seconds = 60
    cache_service = CacheService(settings)

    payload = {"id": "demo123", "language": "en", "transcript": "cached"}
    cache_service.save("demo123", "en", True, payload)

    cache_file = next(settings.cache_dir.glob("*.json"))
    old_time = time.time() - 120
    cache_file.touch()
    import os

    os.utime(cache_file, (old_time, old_time))

    assert cache_service.load("demo123", "en", True) is None
    assert not cache_file.exists()


def test_transcript_service_shapes_response(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    cache_service = CacheService(settings)
    service = TranscriptService(settings, cache_service)

    payload = {
        "id": "demo123",
        "title": "Example",
        "uploader": "Uploader",
        "webpage_url": "https://example.com",
        "language": "en",
        "source": "subtitles",
        "extension": "json3",
        "transcript": "abcdef",
    }

    assert service.shape_response(payload, include_meta=False, max_chars=3) == {
        "id": "demo123",
        "title": None,
        "uploader": None,
        "webpage_url": None,
        "language": "en",
        "source": None,
        "extension": None,
        "transcript": "abc",
    }
