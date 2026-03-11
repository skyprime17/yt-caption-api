from __future__ import annotations

import html
import json
import logging
import re
import subprocess
import sys
import time
from typing import Any, Literal
from urllib.parse import parse_qs, urljoin, urlparse

from curl_cffi import requests
from requests import Session
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptList
from youtube_transcript_api._errors import NoTranscriptFound

from app.config import Settings
from app.exceptions import (
    TranscriptAuthError,
    TranscriptNotFoundError,
    TranscriptRateLimitedError,
    TranscriptServiceError,
    TranscriptUnavailableError,
    TranscriptUpstreamError,
)
from app.models import TranscriptResponse
from app.services.cache_service import CacheService

logger = logging.getLogger(__name__)

CacheStatus = Literal["HIT", "MISS"]

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
    ) -> tuple[TranscriptResponse, CacheStatus]:
        if use_cache:
            cached_payload = self.cache_service.load(video_id, language, include_auto)
            if cached_payload is not None:
                logger.info(
                    "Transcript cache hit: video_id=%s language=%s include_auto=%s",
                    video_id,
                    language,
                    include_auto,
                )
                return TranscriptResponse.model_validate(cached_payload), "HIT"

        logger.info(
            "Transcript fetch start: video_id=%s language=%s include_auto=%s use_cache=%s",
            video_id,
            language,
            include_auto,
            use_cache,
        )
        video_url = self._build_video_url(video_id)
        info: dict[str, Any] | None = None
        direct_error: TranscriptServiceError | None = None

        try:
            payload = self._extract_transcript_direct_payload(
                video_id=video_id,
                language=language,
                video_url=video_url,
                info=None,
            )
        except TranscriptServiceError as exc:
            logger.warning(
                "Direct transcript path failed, trying yt-dlp fallback: video_id=%s language=%s error=%s cause=%s upstream_status=%s",
                video_id,
                language,
                exc.message,
                exc.cause,
                exc.upstream_status,
            )
            direct_error = exc
        else:
            self.cache_service.save(video_id, language, include_auto, payload.model_dump())
            logger.info(
                "Transcript fetch success via direct path: video_id=%s language=%s transcript_chars=%s",
                video_id,
                payload.language,
                len(payload.transcript),
            )
            return payload, "MISS"

        try:
            info = self._extract_video_info(video_url)
            fallback_payload = self._extract_transcript_via_yt_dlp(
                info=info,
                video_url=video_url,
                preferred_language=language,
                include_auto=include_auto,
            )
        except TranscriptServiceError as exc:
            logger.error(
                "yt-dlp fallback failed: video_id=%s language=%s error=%s cause=%s upstream_status=%s",
                video_id,
                language,
                exc.message,
                exc.cause,
                exc.upstream_status,
            )
            if direct_error is not None:
                raise direct_error
            raise exc

        self.cache_service.save(video_id, language, include_auto, fallback_payload.model_dump())
        logger.info(
            "Transcript fetch success via yt-dlp fallback: video_id=%s language=%s transcript_chars=%s",
            video_id,
            fallback_payload.language,
            len(fallback_payload.transcript),
        )
        return fallback_payload, "MISS"

    @staticmethod
    def shape_response(
            payload: TranscriptResponse | dict[str, Any],
            include_meta: bool,
            max_chars: int | None,
    ) -> TranscriptResponse:
        response_payload = TranscriptResponse.model_validate(payload).model_copy()
        transcript = response_payload.transcript
        if max_chars is not None and max_chars >= 0:
            transcript = transcript[:max_chars].rstrip()
        response_payload.transcript = transcript

        if include_meta:
            return response_payload

        return TranscriptResponse(
            id=response_payload.id,
            title=None,
            uploader=None,
            webpage_url=None,
            language=response_payload.language,
            source=None,
            extension=None,
            transcript=response_payload.transcript,
        )

    @staticmethod
    def _build_video_url(video_id: str) -> str:
        return f"https://www.youtube.com/watch?v={video_id}"

    @staticmethod
    def _build_timeout_session(timeout_seconds: float) -> Session:
        class TimeoutSession(Session):
            def request(self, *args, **kwargs):
                kwargs.setdefault("timeout", timeout_seconds)
                return super().request(*args, **kwargs)

        return TimeoutSession()

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

    @staticmethod
    def _service_error_from_status(
            status_code: int,
            message: str,
            *,
            cause: str | None = None,
    ) -> TranscriptServiceError:
        if status_code == 404:
            return TranscriptNotFoundError(message, cause=cause, upstream_status=status_code)
        if status_code == 403:
            return TranscriptAuthError(message, cause=cause, upstream_status=status_code)
        if status_code == 429:
            return TranscriptRateLimitedError(message, cause=cause, upstream_status=status_code)
        if status_code == 502:
            return TranscriptUpstreamError(message, cause=cause, upstream_status=status_code)
        return TranscriptUpstreamError(message, cause=cause, upstream_status=status_code)

    @staticmethod
    def _build_payload(
            *,
            video_id: str,
            video_url: str,
            language: str,
            source: str,
            extension: str | None,
            transcript: str,
            info: dict[str, Any] | None = None,
    ) -> TranscriptResponse:
        return TranscriptResponse(
            id=(info or {}).get("id") or video_id,
            title=(info or {}).get("title"),
            uploader=(info or {}).get("uploader"),
            webpage_url=(info or {}).get("webpage_url") or video_url,
            language=language,
            source=source,
            extension=extension,
            transcript=transcript,
        )

    def _extract_transcript_via_yt_dlp(
            self,
            *,
            info: dict[str, Any],
            video_url: str,
            preferred_language: str | None,
            include_auto: bool,
    ) -> TranscriptResponse:
        candidates = self._pick_caption_tracks(
            info=info,
            preferred_language=preferred_language,
            include_auto=include_auto,
        )
        logger.info(
            "Caption candidates selected: video_id=%s count=%s candidates=%s",
            info.get("id"),
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
        last_error: TranscriptServiceError | None = None

        for selected_language, track, source in candidates:
            track_url = track.get("url")
            extension = track.get("ext") or ""
            if not track_url:
                logger.warning(
                    "Caption candidate missing URL: video_id=%s language=%s source=%s ext=%s",
                    info.get("id"),
                    selected_language,
                    source,
                    extension,
                )
                last_error = TranscriptNotFoundError("Caption track URL missing")
                continue

            try:
                text = self._fetch_caption_text(track_url, extension)
            except TranscriptServiceError as exc:
                logger.warning(
                    "Caption candidate failed: video_id=%s language=%s source=%s ext=%s error=%s cause=%s upstream_status=%s",
                    info.get("id"),
                    selected_language,
                    source,
                    extension,
                    exc.message,
                    exc.cause,
                    exc.upstream_status,
                )
                last_error = exc
                continue

            logger.info(
                "Transcript fetch success: video_id=%s selected_language=%s source=%s ext=%s transcript_chars=%s",
                info.get("id"),
                selected_language,
                source,
                extension,
                len(text),
            )
            return self._build_payload(
                video_id=info.get("id") or "",
                video_url=video_url,
                language=selected_language,
                source=source,
                extension=extension,
                transcript=text,
                info=info,
            )

        if last_error is not None:
            logger.error(
                "Transcript fetch failed after trying all candidates: video_id=%s language=%s error=%s cause=%s upstream_status=%s",
                info.get("id"),
                preferred_language,
                last_error.message,
                last_error.cause,
                last_error.upstream_status,
            )
            raise last_error

        logger.error(
            "Transcript fetch failed with no usable candidates: video_id=%s language=%s",
            info.get("id"),
            preferred_language,
        )
        raise TranscriptUnavailableError("No usable captions found for this video")


    def _extract_transcript_direct(self, video_id: str, language: str | None) -> str:
        transcript_api = YouTubeTranscriptApi(
            http_client=self._build_timeout_session(self.settings.caption_fetch_timeout_seconds)
        )
        transcript_list = transcript_api.list(video_id)
        transcript = self._pick_direct_transcript(transcript_list, language)
        return self._clean_transcript_text(" ".join(
            s.text.strip()
            for s in transcript
            if getattr(s, "text", None) and s.text.strip()
        ))

    @staticmethod
    def _pick_direct_transcript(transcript_list: TranscriptList, preferred_language: str | None):
        available_transcripts = list(transcript_list)
        if not available_transcripts:
            raise TranscriptNotFoundError("No transcripts found for this video")

        if not preferred_language:
            manual = [item for item in available_transcripts if not item.is_generated]
            if manual:
                return manual[0].fetch()
            return available_transcripts[0].fetch()

        normalized_preferred = preferred_language.lower()
        preferred_prefix = normalized_preferred.split("-")[0]

        def sort_key(transcript) -> tuple[int, int]:
            language_code = transcript.language_code.lower()
            language_prefix = language_code.split("-")[0]

            if language_code == normalized_preferred:
                language_rank = 0
            elif language_prefix == preferred_prefix:
                language_rank = 1
            else:
                language_rank = 2

            generated_rank = 1 if transcript.is_generated else 0
            return language_rank, generated_rank

        sorted_transcripts = sorted(available_transcripts, key=sort_key)
        best_match = sorted_transcripts[0]
        if sort_key(best_match)[0] == 2:
            raise TranscriptNotFoundError(
                "No transcript found for the requested language",
                cause=f"Requested={preferred_language}; available={[item.language_code for item in available_transcripts]}",
                upstream_status=404,
            )

        return best_match.fetch()

    def _extract_transcript_direct_payload(
            self,
            *,
            video_id: str,
            language: str | None,
            video_url: str,
            info: dict[str, Any] | None,
    ) -> TranscriptResponse:
        try:
            transcript = self._extract_transcript_direct(video_id, language)
        except NoTranscriptFound as exc:
            logger.info(
                "Direct transcript language match not found: video_id=%s language=%s available=%s",
                video_id,
                language,
                str(exc),
            )
            raise TranscriptNotFoundError(
                "No transcript found for the requested language",
                cause=str(exc),
                upstream_status=404,
            ) from exc
        except TranscriptNotFoundError:
            raise
        except Exception as exc:
            logger.exception(
                "Direct transcript extraction raised an exception: video_id=%s language=%s",
                video_id,
                language,
            )
            raise TranscriptUpstreamError(
                "Direct transcript extraction failed",
                cause=str(exc),
                upstream_status=502,
            ) from exc

        if not transcript:
            raise TranscriptUnavailableError("Direct transcript extraction returned empty transcript")

        resolved_language = language or (info or {}).get("language") or "unknown"
        return self._build_payload(
            video_id=video_id,
            video_url=video_url,
            language=resolved_language,
            source="youtube_transcript_api",
            extension=None,
            transcript=transcript,
            info=info,
        )


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
        last_error: TranscriptServiceError | None = None
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
                raise TranscriptUpstreamError(
                    "Failed to start yt-dlp",
                    cause=str(exc),
                    upstream_status=502,
                ) from exc

            if completed.returncode == 0:
                try:
                    return json.loads(completed.stdout)
                except json.JSONDecodeError as exc:
                    last_error = TranscriptUpstreamError(
                        "yt-dlp returned invalid JSON",
                        cause=str(exc),
                        upstream_status=502,
                    )
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
            last_error = self._service_error_from_status(
                status_code,
                f"yt-dlp failed: {message}",
                cause=message,
            )
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
        raise TranscriptUpstreamError("yt-dlp failed unexpectedly", upstream_status=502)

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
            raise TranscriptNotFoundError("No captions found for this video")

        def is_translated_track(track: dict[str, Any]) -> bool:
            track_url = track.get("url") or ""
            query = parse_qs(urlparse(track_url).query)
            lang = (query.get("lang", [""])[0] or "").lower()
            tlang = (query.get("tlang", [""])[0] or "").lower()
            return bool(tlang) and tlang != lang

        # Hard reject translated tracks completely
        sources = [item for item in sources if not is_translated_track(item[1])]

        if not sources:
            raise TranscriptUnavailableError("No non-translated captions found for this video")

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
                raise TranscriptUpstreamError(
                    "Caption segment request failed",
                    cause=str(exc),
                    upstream_status=502,
                ) from exc
            if response.status_code == 429:
                raise TranscriptRateLimitedError(
                    "YouTube rate limited caption segment fetch",
                    upstream_status=429,
                )
            if response.status_code == 403:
                raise TranscriptAuthError(
                    "YouTube rejected caption segment fetch",
                    upstream_status=403,
                )
            if response.status_code >= 400:
                logger.warning(
                    "Caption segment fetch returned error: playlist_url=%s segment_index=%s status=%s segment_url=%s",
                    playlist_url,
                    index,
                    response.status_code,
                    segment_url,
                )
                raise TranscriptUpstreamError(
                    f"Caption segment fetch failed with status {response.status_code}",
                    upstream_status=response.status_code,
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
            raise TranscriptUpstreamError(
                "Caption track request failed",
                cause=str(exc),
                upstream_status=502,
            ) from exc
        if response.status_code == 429:
            raise TranscriptRateLimitedError(
                "YouTube rate limited caption fetch",
                upstream_status=429,
            )
        if response.status_code == 403:
            raise TranscriptAuthError(
                "YouTube rejected caption fetch",
                upstream_status=403,
            )
        if response.status_code == 404:
            raise TranscriptNotFoundError(
                "Caption track was unavailable",
                upstream_status=404,
            )
        if response.status_code >= 400:
            logger.warning(
                "Caption track fetch returned error: ext=%s status=%s track_url=%s",
                extension,
                response.status_code,
                track_url,
            )
            raise TranscriptUpstreamError(
                f"Caption track fetch failed with status {response.status_code}",
                upstream_status=response.status_code,
            )

        if extension == "json3":
            try:
                return self._parse_json3_events(response.json())
            except ValueError as exc:
                logger.exception("Caption json3 parse failed: track_url=%s", track_url)
                raise TranscriptUpstreamError(
                    "Caption JSON parse failed",
                    cause=str(exc),
                    upstream_status=502,
                ) from exc

        if response.text.lstrip().startswith("#EXTM3U"):
            return self._resolve_hls_caption_playlist(response.text, track_url)

        if extension == "vtt":
            return self._parse_vtt_text(response.text)

        return self._clean_transcript_text(response.text)
