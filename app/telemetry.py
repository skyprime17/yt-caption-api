from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI
    from app.config import Settings

logger = logging.getLogger(__name__)


def init_telemetry(settings: Settings, app: FastAPI) -> None:
    """Initializes HyperDX OpenTelemetry exporter, logging handler, and FastAPI instrumentation."""
    endpoint = settings.otel_exporter_otlp_endpoint or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    api_key = settings.hyperdx_api_key or os.environ.get("HYPERDX_API_KEY")

    if not endpoint and not api_key:
        logger.info("OpenTelemetry telemetry disabled (neither OTEL_EXPORTER_OTLP_ENDPOINT nor HYPERDX_API_KEY set)")
        return

    if settings.otel_service_name:
        os.environ.setdefault("OTEL_SERVICE_NAME", settings.otel_service_name)
    if settings.otel_exporter_otlp_endpoint:
        os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", settings.otel_exporter_otlp_endpoint)
    if settings.hyperdx_api_key:
        os.environ.setdefault("HYPERDX_API_KEY", settings.hyperdx_api_key)
    if settings.hyperdx_enable_advanced_network_capture:
        os.environ.setdefault("HYPERDX_ENABLE_ADVANCED_NETWORK_CAPTURE", "1")

    log_level_num = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.getLogger().setLevel(log_level_num)

    try:
        from hyperdx.opentelemetry import configure_opentelemetry
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        configure_opentelemetry()
        FastAPIInstrumentor.instrument_app(app)

        # Ensure root logger and OpenTelemetry log handlers receive logs down to log_level_num
        root_logger = logging.getLogger()
        root_logger.setLevel(log_level_num)
        for handler in root_logger.handlers:
            handler.setLevel(log_level_num)

        logger.info(
            "OpenTelemetry initialized for service=%s endpoint=%s log_level=%s",
            os.environ.get("OTEL_SERVICE_NAME", settings.otel_service_name),
            os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"),
            settings.log_level,
        )
    except Exception:
        logger.exception("Failed to initialize OpenTelemetry telemetry")
