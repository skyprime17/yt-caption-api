from __future__ import annotations


class TranscriptServiceError(Exception):
    def __init__(
        self,
        message: str,
        *,
        cause: str | None = None,
        upstream_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.cause = cause
        self.upstream_status = upstream_status


class TranscriptNotFoundError(TranscriptServiceError):
    pass


class TranscriptAuthError(TranscriptServiceError):
    pass


class TranscriptRateLimitedError(TranscriptServiceError):
    pass


class TranscriptUpstreamError(TranscriptServiceError):
    pass


class TranscriptUnavailableError(TranscriptServiceError):
    pass
