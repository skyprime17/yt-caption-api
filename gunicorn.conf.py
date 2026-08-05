import os

bind = os.environ.get("BIND", "0.0.0.0:8000")
workers = int(os.environ.get("WORKERS", "1"))
worker_class = "uvicorn.workers.UvicornWorker"
timeout = 60


def post_fork(server, worker):
    """Configures OpenTelemetry for each pre-forked Gunicorn worker process."""
    try:
        import logging
        from hyperdx.opentelemetry import configure_opentelemetry

        log_level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
        log_level_num = getattr(logging, log_level_name, logging.INFO)

        configure_opentelemetry()

        root_logger = logging.getLogger()
        root_logger.setLevel(log_level_num)
        for handler in root_logger.handlers:
            handler.setLevel(log_level_num)

        print(f"[Worker {worker.pid}] OpenTelemetry telemetry initialized with log_level={log_level_name}.")
    except Exception as e:
        print(f"[Worker {worker.pid}] OpenTelemetry telemetry init failed: {e}")
