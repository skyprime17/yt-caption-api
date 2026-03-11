from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from typing import AsyncIterator

import uvicorn
from fastapi import FastAPI

from app.config import Settings, get_settings
from app.routers.transcript import router as transcript_router
from app.services.cache_service import CacheService
from app.services.transcript_service import TranscriptService

logger = logging.getLogger(__name__)


async def cache_cleanup_loop(cache_service: CacheService, interval_seconds: int) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        cache_service.prune()


def create_lifespan(settings: Settings):
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if not settings.cookies_file.exists():
            raise RuntimeError(f"Missing cookies file at {settings.cookies_file}")

        settings.cache_dir.mkdir(parents=True, exist_ok=True)
        cache_service = CacheService(settings)
        transcript_service = TranscriptService(settings, cache_service)
        cache_service.prune()
        cleanup_task = asyncio.create_task(
            cache_cleanup_loop(cache_service, settings.cache_cleanup_interval_seconds)
        )

        app.state.settings = settings
        app.state.cache_service = cache_service
        app.state.transcript_service = transcript_service
        app.state.cache_cleanup_task = cleanup_task
        logger.info(
            "App startup complete: cookies=%s cache_dir=%s cache_ttl=%ss cleanup_interval=%ss",
            settings.cookies_file,
            settings.cache_dir,
            settings.cache_ttl_seconds,
            settings.cache_cleanup_interval_seconds,
        )
        try:
            yield
        finally:
            cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await cleanup_task
            logger.info("App shutdown complete")

    return lifespan


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or get_settings()
    logging.basicConfig(
        level=getattr(logging, app_settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    app = FastAPI(
        title=app_settings.app_name,
        version=app_settings.app_version,
        lifespan=create_lifespan(app_settings),
    )
    app.include_router(transcript_router)
    return app


app = create_app()


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)
