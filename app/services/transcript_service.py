from __future__ import annotations

import html
import json
import re
import subprocess
import sys
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

from curl_cffi import requests
from fastapi import HTTPException

from app.config import Settings
from app.services.cache_service import CacheService

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
                return cached_payload, "HIT"

        video_url = self._build_video_url(video_id)
        info = self._extract_video_info(video_url)

        candidates = self._pick_caption_tracks(
            info=info,
            preferred_language=language,
            include_auto=include_auto,
        )
        last_error: HTTPException | None = None

        for selected_language, track, source in candidates:
            track_url = track.get("url")
            extension = track.get("ext") or ""
            if not track_url:
                last_error = HTTPException(status_code=404, detail="Caption track URL missing")
                continue

            try:
                text = self._fetch_caption_text(track_url, extension)
            except HTTPException as exc:
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
            return payload, "MISS"

        if last_error is not None:
            raise last_error

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
        completed = subprocess.run(
            command,
            cwd=self.settings.cookies_file.parent,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            message = completed.stderr.strip() or completed.stdout.strip() or "Unknown yt-dlp error"
            lowered = message.lower()
            if "too many requests" in lowered or "429" in lowered:
                status_code = 429
            elif "forbidden" in lowered or "sign in" in lowered or "login" in lowered:
                status_code = 403
            elif "not available" in lowered or "unavailable" in lowered:
                status_code = 404
            else:
                status_code = 502
            raise HTTPException(status_code=status_code, detail=f"yt-dlp failed: {message}")

        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=502, detail="yt-dlp returned invalid JSON") from exc

    @staticmethod
    def _effective_track_language(lang: str, track: dict[str, Any]) -> str:
        track_url = track.get("url") or ""
        query = parse_qs(urlparse(track_url).query)
        return (query.get("tlang", [lang])[0] or lang).lower()

    def _pick_caption_tracks(
            self,
            info: dict[str, Any],
            preferred_language: str | None,
            include_auto: bool,
    ) -> list[tuple[str, dict[str, Any], str]]:
        subtitles = info.get("subtitles") or {}
        automatic_captions = info.get("automatic_captions") or {}
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
            return self._effective_track_language("", track) != (
                    parse_qs(urlparse(track.get("url") or "").query).get("lang", [""])[0] or "").lower()

        def match_priority(item: tuple[str, dict[str, Any], str], normalized: str) -> int:
            language_code = self._effective_track_language(item[0], item[1])
            requested_prefix = normalized.split("-")[0]
            language_prefix = language_code.split("-")[0]
            translated = is_translated_track(item[1])

            if language_code == normalized and not translated:
                return 0
            if language_prefix == requested_prefix and not translated:
                return 1
            if language_code == normalized and translated:
                return 2
            if language_prefix == requested_prefix and translated:
                return 3
            return 4

        def sort_key(
                item: tuple[str, dict[str, Any], str],
                normalized: str | None = None,
        ) -> tuple[int, int, int, int]:
            _, track, source = item
            extension_priority = TRACK_EXTENSION_PRIORITY.get(track.get("ext"), 99)
            translated_priority = 1 if is_translated_track(track) else 0
            source_priority = 0 if source == "subtitles" else 1
            language_priority = 0 if normalized is None else match_priority(item, normalized)
            return language_priority, translated_priority, source_priority, extension_priority

        if preferred_language:
            normalized = preferred_language.lower()
            matching_sources = [
                item
                for item in sources
                if self._effective_track_language(item[0], item[1]).split("-")[0] == normalized.split("-")[0]
            ]
            if matching_sources:
                return sorted(
                    [
                        (self._effective_track_language(lang, track), track, source)
                        for lang, track, source in matching_sources
                    ],
                    key=lambda item: sort_key((item[0], item[1], item[2]), normalized),
                )

        return sorted(
            [
                (self._effective_track_language(lang, track), track, source)
                for lang, track, source in sources
            ],
            key=lambda item: sort_key((item[0], item[1], item[2])),
        )

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

        parts: list[str] = []
        for segment_url in segment_urls:
            response = requests.get(
                segment_url,
                impersonate=self.settings.curl_impersonate,
                timeout=self.settings.caption_fetch_timeout_seconds,
            )
            if response.status_code == 429:
                raise HTTPException(status_code=429, detail="YouTube rate limited caption segment fetch")
            if response.status_code == 403:
                raise HTTPException(status_code=403, detail="YouTube rejected caption segment fetch")
            response.raise_for_status()
            parts.append(self._parse_vtt_text(response.text))

        return self._clean_transcript_text(" ".join(part for part in parts if part))

    def _fetch_caption_text(self, track_url: str, extension: str) -> str:
        response = requests.get(
            track_url,
            impersonate=self.settings.curl_impersonate,
            timeout=self.settings.caption_fetch_timeout_seconds,
        )
        if response.status_code == 429:
            raise HTTPException(status_code=429, detail="YouTube rate limited caption fetch")
        if response.status_code == 403:
            raise HTTPException(status_code=403, detail="YouTube rejected caption fetch")
        if response.status_code == 404:
            raise HTTPException(status_code=404, detail="Caption track was unavailable")
        response.raise_for_status()

        if extension == "json3":
            return self._parse_json3_events(response.json())

        if response.text.lstrip().startswith("#EXTM3U"):
            return self._resolve_hls_caption_playlist(response.text, track_url)

        if extension == "vtt":
            return self._parse_vtt_text(response.text)

        return self._clean_transcript_text(response.text)
