from __future__ import annotations

import asyncio
import json
import re
import subprocess
import time
from contextlib import asynccontextmanager
from contextlib import suppress
from pathlib import Path
from typing import Any

from curl_cffi import requests
from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
COOKIES_FILE = BASE_DIR / "cookies.txt"
CACHE_DIR = BASE_DIR / "cache"
CACHE_TTL_SECONDS = 7 * 24 * 60 * 60
CACHE_CLEANUP_INTERVAL_SECONDS = 6 * 60 * 60

class TranscriptResponse(BaseModel):
    id: str | None = None
    title: str | None = None
    uploader: str | None = None
    webpage_url: str | None = None
    language: str
    source: str | None = None
    extension: str | None = None
    transcript: str


def resolve_url(url: str) -> str:
    response = requests.head(
        url,
        impersonate="chrome",
        allow_redirects=True,
        timeout=20,
    )
    return str(response.url)


def build_video_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def build_cache_key(video_id: str, language: str | None, include_auto: bool) -> str:
    normalized_language = (language or "default").lower()
    auto_flag = "auto" if include_auto else "manual"
    safe_video_id = re.sub(r"[^A-Za-z0-9_-]", "_", video_id)
    safe_language = re.sub(r"[^A-Za-z0-9_-]", "_", normalized_language)
    return f"{safe_video_id}.{safe_language}.{auto_flag}.json"


def prune_cache() -> None:
    if not CACHE_DIR.exists():
        return

    cutoff = time.time() - CACHE_TTL_SECONDS
    for cache_file in CACHE_DIR.glob("*.json"):
        try:
            if cache_file.stat().st_mtime < cutoff:
                cache_file.unlink()
        except OSError:
            continue


async def cache_cleanup_loop() -> None:
    while True:
        await asyncio.sleep(CACHE_CLEANUP_INTERVAL_SECONDS)
        prune_cache()


@asynccontextmanager
async def lifespan(app: FastAPI):
    prune_cache()
    cleanup_task = asyncio.create_task(cache_cleanup_loop())
    app.state.cache_cleanup_task = cleanup_task
    try:
        yield
    finally:
        cleanup_task.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup_task


app = FastAPI(title="YT Caption API", version="0.1.0", lifespan=lifespan)


def load_cached_transcript(video_id: str, language: str | None, include_auto: bool) -> dict[str, Any] | None:
    cache_file = CACHE_DIR / build_cache_key(video_id, language, include_auto)
    if not cache_file.exists():
        return None

    try:
        if cache_file.stat().st_mtime < time.time() - CACHE_TTL_SECONDS:
            cache_file.unlink()
            return None
    except OSError:
        return None

    try:
        return json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save_cached_transcript(
    video_id: str,
    language: str | None,
    include_auto: bool,
    payload: dict[str, Any],
) -> None:
    prune_cache()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / build_cache_key(video_id, language, include_auto)
    cache_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def shape_response(payload: dict[str, Any], include_meta: bool, max_chars: int | None) -> dict[str, Any]:
    response_payload = dict(payload)
    transcript = response_payload["transcript"]
    if max_chars is not None and max_chars >= 0:
        transcript = transcript[:max_chars].rstrip()
    response_payload["transcript"] = transcript

    if include_meta:
        return response_payload

    return {
        "id": response_payload["id"],
        "title": None,
        "uploader": None,
        "webpage_url": None,
        "language": response_payload["language"],
        "source": None,
        "extension": None,
        "transcript": response_payload["transcript"],
    }


def extract_video_info(url: str) -> dict[str, Any]:
    command = [
        "uv",
        "run",
        "yt-dlp",
        "--skip-download",
        "--dump-single-json",
        "--cookies",
        str(COOKIES_FILE),
        "--js-runtimes",
        "node",
        "--remote-components",
        "ejs:github",
        url,
    ]
    completed = subprocess.run(
        command,
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "Unknown yt-dlp error"
        raise HTTPException(status_code=400, detail=f"yt-dlp failed: {message}")

    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="yt-dlp returned invalid JSON") from exc


def pick_caption_track(
    info: dict[str, Any],
    preferred_language: str | None,
    include_auto: bool,
) -> tuple[str, dict[str, Any], str]:
    subtitles = info.get("subtitles") or {}
    automatic_captions = info.get("automatic_captions") or {}
    sources: list[tuple[str, dict[str, Any], str]] = []

    for lang, tracks in subtitles.items():
        for track in tracks:
            if track.get("ext") in {"json3", "srv1", "srv2", "srv3", "ttml", "vtt"}:
                sources.append((lang, track, "subtitles"))

    if include_auto:
        for lang, tracks in automatic_captions.items():
            for track in tracks:
                if track.get("ext") in {"json3", "srv1", "srv2", "srv3", "ttml", "vtt"}:
                    sources.append((lang, track, "automatic_captions"))

    if not sources:
        raise HTTPException(status_code=404, detail="No captions found for this video")

    if preferred_language:
        normalized = preferred_language.lower()
        exact_match = next(
            (
                item
                for item in sources
                if item[0].lower() == normalized
            ),
            None,
        )
        if exact_match:
            return exact_match

        prefix_match = next(
            (
                item
                for item in sources
                if item[0].lower().split("-")[0] == normalized.split("-")[0]
            ),
            None,
        )
        if prefix_match:
            return prefix_match

    return sources[0]


def parse_json3_events(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    for event in payload.get("events", []):
        segments = event.get("segs") or []
        text = "".join(segment.get("utf8", "") for segment in segments).strip()
        if text:
            parts.append(text)
    return clean_transcript_text(" ".join(parts))


def clean_transcript_text(text: str) -> str:
    # Collapse caption-style hard breaks and repeated whitespace into readable prose.
    text = text.replace("\r", " ").replace("\n", " ")
    text = re.sub(r"\s+", " ", text)

    # Remove common subtitle cues and speaker-turn markers.
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"(^|\s)-\s+", " ", text)

    # Remove spaces before punctuation introduced by segment joins.
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)

    # Normalize spaces around brackets/quotes a bit.
    text = re.sub(r"([(\[{])\s+", r"\1", text)
    text = re.sub(r"\s+([)\]}])", r"\1", text)
    text = re.sub(r"\s+", " ", text)

    return text.strip()


def fetch_caption_text(track_url: str, extension: str) -> str:
    response = requests.get(
        track_url,
        impersonate="chrome",
        timeout=30,
    )
    response.raise_for_status()

    if extension == "json3":
        return parse_json3_events(response.json())

    return clean_transcript_text(response.text)


def get_transcript_payload(
    video_id: str,
    language: str | None,
    include_auto: bool,
    use_cache: bool,
) -> tuple[dict[str, Any], str]:
    if use_cache:
        cached_payload = load_cached_transcript(video_id, language, include_auto)
        if cached_payload is not None:
            return cached_payload, "HIT"

    resolved_url = resolve_url(build_video_url(video_id))
    info = extract_video_info(resolved_url)

    selected_language, track, source = pick_caption_track(
        info=info,
        preferred_language=language,
        include_auto=include_auto,
    )

    track_url = track.get("url")
    extension = track.get("ext") or ""
    if not track_url:
        raise HTTPException(status_code=404, detail="Caption track URL missing")

    try:
        text = fetch_caption_text(track_url, extension)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to fetch captions: {exc}") from exc

    payload = {
        "id": info.get("id"),
        "title": info.get("title"),
        "uploader": info.get("uploader"),
        "webpage_url": info.get("webpage_url") or resolved_url,
        "language": selected_language,
        "source": source,
        "extension": extension,
        "transcript": text,
    }
    save_cached_transcript(video_id, language, include_auto, payload)
    return payload, "MISS"


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get(
    "/transcript/{video_id}",
    response_model=TranscriptResponse,
    response_model_exclude_none=True,
)
def transcript(
    response: Response,
    video_id: str,
    language: str | None = None,
    include_auto: bool = True,
    include_meta: bool = True,
    max_chars: int | None = None,
    use_cache: bool = True,
):
    if not COOKIES_FILE.exists():
        raise HTTPException(
            status_code=500,
            detail=f"Missing cookies file at {COOKIES_FILE}",
        )

    video_id = video_id.strip()
    if not video_id:
        raise HTTPException(status_code=400, detail="Missing YouTube video ID")

    if max_chars is not None and max_chars < 0:
        raise HTTPException(status_code=400, detail="max_chars must be >= 0")

    payload, cache_status = get_transcript_payload(video_id, language, include_auto, use_cache)
    response.headers["X-Cache"] = cache_status
    shaped_payload = shape_response(payload, include_meta=include_meta, max_chars=max_chars)
    return TranscriptResponse.model_validate(shaped_payload)
