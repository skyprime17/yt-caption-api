from __future__ import annotations

from pydantic import BaseModel


class TranscriptResponse(BaseModel):
    id: str | None = None
    title: str | None = None
    uploader: str | None = None
    webpage_url: str | None = None
    language: str
    source: str | None = None
    extension: str | None = None
    transcript: str
