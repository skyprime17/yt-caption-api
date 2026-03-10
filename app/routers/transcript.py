from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.openapi.models import APIKey

from app.dependencies import get_transcript_service, require_access_token
from app.models import TranscriptResponse
from app.services.transcript_service import TranscriptService


router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get(
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
    _: APIKey = Depends(require_access_token),
    transcript_service: TranscriptService = Depends(get_transcript_service),
):
    video_id = video_id.strip()
    if not video_id:
        raise HTTPException(status_code=400, detail="Missing YouTube video ID")

    if max_chars is not None and max_chars < 0:
        raise HTTPException(status_code=400, detail="max_chars must be >= 0")

    payload, cache_status = transcript_service.get_transcript_payload(
        video_id=video_id,
        language=language,
        include_auto=include_auto,
        use_cache=use_cache,
    )
    response.headers["X-Cache"] = cache_status
    return transcript_service.shape_response(
        payload=payload,
        include_meta=include_meta,
        max_chars=max_chars,
    )
