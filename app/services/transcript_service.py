from __future__ import annotations

import html
import json
import logging
import re
import subprocess
import sys
import time
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

from curl_cffi import requests
from fastapi import HTTPException

from app.config import Settings
from app.services.cache_service import CacheService

logger = logging.getLogger(__name__)

TRACK_EXTENSION_PRIORITY = {
    "json3": 0,
    "srv3": 1,
    "srv2": 2,
    "srv1": 3,
    "vtt": 4,
    "ttml": 5,
}


class TranscriptService:
    def __init__(self, settings: Settings, cache_service: CacheService) -> None:
        self.settings = settings
        self.cache_service = cache_service

    def get_transcript_payload(
            self,
            video_id: str,
            language: str | None,
            include_auto: bool,
            use_cache: bool,
    ) -> tuple[dict[str, Any], str]:
        if use_cache:
            cached_payload = self.cache_service.load(video_id, language, include_auto)
            if cached_payload is not None:
                logger.info(
                    "Transcript cache hit: video_id=%s language=%s include_auto=%s",
                    video_id,
                    language,
                    include_auto,
                )
                return cached_payload, "HIT"

        logger.info(
            "Transcript fetch start: video_id=%s language=%s include_auto=%s use_cache=%s",
            video_id,
            language,
            include_auto,
            use_cache,
        )
        video_url = self._build_video_url(video_id)
        info = self._extract_video_info(video_url)

        candidates = self._pick_caption_tracks(
            info=info,
            preferred_language=language,
            include_auto=include_auto,
        )
        logger.info(
            "Caption candidates selected: video_id=%s count=%s candidates=%s",
            video_id,
            len(candidates),
            [
                {
                    "language": selected_language,
                    "source": source,
                    "ext": track.get("ext"),
                    "client": track.get("__yt_dlp_client"),
                }
                for selected_language, track, source in candidates[:5]
            ],
        )
        last_error: HTTPException | None = None

        for selected_language, track, source in candidates:
            track_url = track.get("url")
            extension = track.get("ext") or ""
            if not track_url:
                logger.warning(
                    "Caption candidate missing URL: video_id=%s language=%s source=%s ext=%s",
                    video_id,
                    selected_language,
                    source,
                    extension,
                )
                last_error = HTTPException(status_code=404, detail="Caption track URL missing")
                continue

            try:
                text = self._fetch_caption_text(track_url, extension)
            except HTTPException as exc:
                logger.warning(
                    "Caption candidate failed: video_id=%s language=%s source=%s ext=%s status=%s detail=%s",
                    video_id,
                    selected_language,
                    source,
                    extension,
                    exc.status_code,
                    exc.detail,
                )
                last_error = exc
                continue

            payload = {
                "id": info.get("id"),
                "title": info.get("title"),
                "uploader": info.get("uploader"),
                "webpage_url": info.get("webpage_url") or video_url,
                "language": selected_language,
                "source": source,
                "extension": extension,
                "transcript": text,
            }
            self.cache_service.save(video_id, language, include_auto, payload)
            logger.info(
                "Transcript fetch success: video_id=%s selected_language=%s source=%s ext=%s transcript_chars=%s",
                video_id,
                selected_language,
                source,
                extension,
                len(text),
            )
            return payload, "MISS"

        if last_error is not None:
            logger.error(
                "Transcript fetch failed after trying all candidates: video_id=%s language=%s status=%s detail=%s",
                video_id,
                language,
                last_error.status_code,
                last_error.detail,
            )
            raise last_error

        logger.error(
            "Transcript fetch failed with no usable candidates: video_id=%s language=%s",
            video_id,
            language,
        )
        raise HTTPException(status_code=404, detail="No usable captions found for this video")

    @staticmethod
    def shape_response(
            payload: dict[str, Any],
            include_meta: bool,
            max_chars: int | None,
    ) -> dict[str, Any]:
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

    @staticmethod
    def _build_video_url(video_id: str) -> str:
        return f"https://www.youtube.com/watch?v={video_id}"

    @staticmethod
    def _map_yt_dlp_error(message: str) -> tuple[int, bool]:
        lowered = message.lower()
        if "too many requests" in lowered or "429" in lowered:
            return 429, True
        if "forbidden" in lowered or "sign in" in lowered or "login" in lowered:
            return 403, False
        if "not available" in lowered or "unavailable" in lowered:
            return 404, False
        return 502, True

    def _extract_video_info(self, url: str) -> dict[str, Any]:
        command = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--skip-download",
            "--dump-single-json",
            "--cookies",
            str(self.settings.cookies_file),
            "--js-runtimes",
            self.settings.yt_dlp_js_runtime,
            "--remote-components",
            self.settings.yt_dlp_remote_component,
            url,
        ]
        last_error: HTTPException | None = None
        attempts = max(1, self.settings.yt_dlp_retry_attempts)

        for attempt in range(1, attempts + 1):
            try:
                completed = subprocess.run(
                    command,
                    cwd=self.settings.cookies_file.parent,
                    capture_output=True,
                    text=True,
                    check=False,
                )
            except OSError as exc:
                logger.exception("yt-dlp subprocess launch failed: url=%s", url)
                raise HTTPException(status_code=502, detail="Failed to start yt-dlp") from exc

            if completed.returncode == 0:
                try:
                    return json.loads(completed.stdout)
                except json.JSONDecodeError as exc:
                    last_error = HTTPException(status_code=502, detail="yt-dlp returned invalid JSON")
                    should_retry = attempt < attempts
                    logger.warning(
                        "yt-dlp returned invalid JSON: url=%s attempt=%s/%s stdout_prefix=%r retry=%s",
                        url,
                        attempt,
                        attempts,
                        completed.stdout[:300],
                        should_retry,
                    )
                    if should_retry:
                        time.sleep(self.settings.yt_dlp_retry_delay_seconds)
                        continue
                    raise last_error from exc

            message = completed.stderr.strip() or completed.stdout.strip() or "Unknown yt-dlp error"
            status_code, retryable = self._map_yt_dlp_error(message)
            last_error = HTTPException(status_code=status_code, detail=f"yt-dlp failed: {message}")
            logger.warning(
                "yt-dlp extraction failed: url=%s attempt=%s/%s status=%s retryable=%s message=%s",
                url,
                attempt,
                attempts,
                status_code,
                retryable,
                message,
            )
            if retryable and attempt < attempts:
                time.sleep(self.settings.yt_dlp_retry_delay_seconds)
                continue
            raise last_error

        if last_error is not None:
            raise last_error
        raise HTTPException(status_code=502, detail="yt-dlp failed unexpectedly")

    @staticmethod
    def _effective_track_language(lang: str, track: dict[str, Any]) -> str:
        track_url = track.get("url") or ""
        query = parse_qs(urlparse(track_url).query)
        return (query.get("tlang", [lang])[0] or lang).lower()

    @staticmethod
    def _get_native_language(info: dict[str, Any]) -> str | None:
        for key in ("language", "original_language"):
            value = info.get(key)
            if isinstance(value, str) and value.strip():
                return value.lower()
        return None

    def _pick_caption_tracks(
            self,
            info: dict[str, Any],
            preferred_language: str | None,
            include_auto: bool,
    ) -> list[tuple[str, dict[str, Any], str]]:
        subtitles = info.get("subtitles") or {}
        automatic_captions = info.get("automatic_captions") or {}
        native_language = self._get_native_language(info)

        sources: list[tuple[str, dict[str, Any], str]] = []

        for lang, tracks in subtitles.items():
            for track in tracks:
                if track.get("ext") in TRACK_EXTENSION_PRIORITY:
                    sources.append((lang, track, "subtitles"))

        if include_auto:
            for lang, tracks in automatic_captions.items():
                for track in tracks:
                    if track.get("ext") in TRACK_EXTENSION_PRIORITY:
                        sources.append((lang, track, "automatic_captions"))

        if not sources:
            raise HTTPException(status_code=404, detail="No captions found for this video")

        def is_translated_track(track: dict[str, Any]) -> bool:
            track_url = track.get("url") or ""
            query = parse_qs(urlparse(track_url).query)
            lang = (query.get("lang", [""])[0] or "").lower()
            tlang = (query.get("tlang", [""])[0] or "").lower()
            return bool(tlang) and tlang != lang

        # Hard reject translated tracks completely
        sources = [item for item in sources if not is_translated_track(item[1])]

        if not sources:
            raise HTTPException(status_code=404, detail="No non-translated captions found for this video")

        normalized_preferred = preferred_language.lower() if preferred_language else None
        preferred_prefix = normalized_preferred.split("-")[0] if normalized_preferred else None
        native_prefix = native_language.split("-")[0] if native_language else None

        normalized_sources = [
            (self._effective_track_language(lang, track), track, source)
            for lang, track, source in sources
        ]

        def language_rank(track_language: str) -> int:
            track_prefix = track_language.split("-")[0]

            # 1) requested language
            if normalized_preferred:
                if track_language == normalized_preferred:
                    return 0
                if track_prefix == preferred_prefix:
                    return 1

            # 2) native/original language
            if native_language:
                if track_language == native_language:
                    return 2
                if track_prefix == native_prefix:
                    return 3

            # 3) any other non-translated language
            return 4

        def sort_key(item: tuple[str, dict[str, Any], str]) -> tuple[int, int, int]:
            lang, track, source = item
            source_priority = 0 if source == "subtitles" else 1
            extension_priority = TRACK_EXTENSION_PRIORITY.get(track.get("ext"), 99)
            return (
                language_rank(lang),
                source_priority,
                extension_priority,
            )

        return sorted(normalized_sources, key=sort_key)

    def _parse_json3_events(self, payload: dict[str, Any]) -> str:
        parts: list[str] = []
        for event in payload.get("events", []):
            segments = event.get("segs") or []
            text = "".join(segment.get("utf8", "") for segment in segments).strip()
            text = self._clean_caption_fragment(text)
            if text:
                parts.append(text)
        return self._clean_transcript_text(" ".join(parts))

    @staticmethod
    def _clean_caption_fragment(text: str) -> str:
        text = re.sub(r"<[^>]+>", "", text)
        return html.unescape(text).strip()

    @staticmethod
    def _clean_transcript_text(text: str) -> str:
        text = text.replace("\r", " ").replace("\n", " ")
        text = re.sub(r"\s+", " ", text)
        text = re.sub(
            r"\[(music|applause|laughter|cheering)\]",
            " ",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            r"\((music|applause|laughter|cheering)\)",
            " ",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(r"\s+([,.;:!?])", r"\1", text)
        text = re.sub(r"([(\[{])\s+", r"\1", text)
        text = re.sub(r"\s+([)\]}])", r"\1", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def _parse_vtt_text(self, text: str) -> str:
        lines: list[str] = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line == "WEBVTT":
                continue
            if line.startswith(("NOTE", "Kind:", "Language:", "X-TIMESTAMP-MAP=")):
                continue
            if "-->" in line:
                continue
            if re.fullmatch(r"\d+", line):
                continue
            cleaned_line = self._clean_caption_fragment(line)
            if cleaned_line:
                lines.append(cleaned_line)

        return self._clean_transcript_text(" ".join(lines))

    def _resolve_hls_caption_playlist(self, playlist_text: str, playlist_url: str) -> str:
        segment_urls = [
            urljoin(playlist_url, line.strip())
            for line in playlist_text.splitlines()
            if line.strip() and not line.startswith("#")
        ]
        if not segment_urls:
            return self._clean_transcript_text(playlist_text)

        logger.info(
            "Resolving HLS caption playlist: playlist_url=%s segments=%s",
            playlist_url,
            len(segment_urls),
        )
        parts: list[str] = []
        for index, segment_url in enumerate(segment_urls, start=1):
            try:
                response = requests.get(
                    segment_url,
                    impersonate=self.settings.curl_impersonate,
                    timeout=self.settings.caption_fetch_timeout_seconds,
                )
            except Exception as exc:
                logger.exception(
                    "Caption segment request failed: playlist_url=%s segment_index=%s segment_url=%s",
                    playlist_url,
                    index,
                    segment_url,
                )
                raise HTTPException(status_code=502, detail="Caption segment request failed") from exc
            if response.status_code == 429:
                raise HTTPException(status_code=429, detail="YouTube rate limited caption segment fetch")
            if response.status_code == 403:
                raise HTTPException(status_code=403, detail="YouTube rejected caption segment fetch")
            if response.status_code >= 400:
                logger.warning(
                    "Caption segment fetch returned error: playlist_url=%s segment_index=%s status=%s segment_url=%s",
                    playlist_url,
                    index,
                    response.status_code,
                    segment_url,
                )
                raise HTTPException(
                    status_code=502,
                    detail=f"Caption segment fetch failed with status {response.status_code}",
                )
            parts.append(self._parse_vtt_text(response.text))

        return self._clean_transcript_text(" ".join(part for part in parts if part))

    def _fetch_caption_text(self, track_url: str, extension: str) -> str:
        try:
            response = requests.get(
                track_url,
                impersonate=self.settings.curl_impersonate,
                timeout=self.settings.caption_fetch_timeout_seconds,
            )
        except Exception as exc:
            logger.exception("Caption track request failed: ext=%s track_url=%s", extension, track_url)
            raise HTTPException(status_code=502, detail="Caption track request failed") from exc
        if response.status_code == 429:
            raise HTTPException(status_code=429, detail="YouTube rate limited caption fetch")
        if response.status_code == 403:
            raise HTTPException(status_code=403, detail="YouTube rejected caption fetch")
        if response.status_code == 404:
            raise HTTPException(status_code=404, detail="Caption track was unavailable")
        if response.status_code >= 400:
            logger.warning(
                "Caption track fetch returned error: ext=%s status=%s track_url=%s",
                extension,
                response.status_code,
                track_url,
            )
            raise HTTPException(
                status_code=502,
                detail=f"Caption track fetch failed with status {response.status_code}",
            )

        if extension == "json3":
            try:
                return self._parse_json3_events(response.json())
            except ValueError as exc:
                logger.exception("Caption json3 parse failed: track_url=%s", track_url)
                raise HTTPException(status_code=502, detail="Caption JSON parse failed") from exc

        if response.text.lstrip().startswith("#EXTM3U"):
            return self._resolve_hls_caption_playlist(response.text, track_url)

        if extension == "vtt":
            return self._parse_vtt_text(response.text)

        return self._clean_transcript_text(response.text)
