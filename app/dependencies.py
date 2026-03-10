from __future__ import annotations

from fastapi import Depends, HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader

from app.config import Settings
from app.services.cache_service import CacheService
from app.services.transcript_service import TranscriptService


access_token_header = APIKeyHeader(name="X-AccessToken", auto_error=False)


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_cache_service(request: Request) -> CacheService:
    return request.app.state.cache_service


def get_transcript_service(request: Request) -> TranscriptService:
    return request.app.state.transcript_service


def require_access_token(
    api_key_header: str | None = Security(access_token_header),
    settings: Settings = Depends(get_app_settings),
) -> None:
    if api_key_header != settings.access_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Could not validate API key",
        )
