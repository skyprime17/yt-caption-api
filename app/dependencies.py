from __future__ import annotations

from fastapi import Request

from app.config import Settings
from app.services.cache_service import CacheService
from app.services.transcript_service import TranscriptService


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_cache_service(request: Request) -> CacheService:
    return request.app.state.cache_service


def get_transcript_service(request: Request) -> TranscriptService:
    return request.app.state.transcript_service
