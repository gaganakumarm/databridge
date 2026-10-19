import json
import logging
import sys
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Lock

from app.config import Settings

_setup_lock = Lock()
# Only fixed event names and explicitly approved metadata can reach the output.
_EVENTS = {"request_completed", "request_failed", "application_started"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        event = record.msg if record.msg in _EVENTS else "application_event"
        data: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "severity": record.levelname,
            "event": event,
            "request_id": getattr(record, "request_id", None),
            "duration_ms": getattr(record, "duration_ms", None),
        }
        status = getattr(record, "status_code", None)
        if isinstance(status, int):
            data["status_code"] = status
        if event == "request_failed":
            data["error"] = {"code": "internal_error", "detail": "[REDACTED]"}
        return json.dumps(data)


def configure_logging(settings: Settings) -> logging.Logger:
    with _setup_lock:
        logger = logging.getLogger("databridge")
        logger.setLevel(settings.log_level)
        logger.propagate = False
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            handler.close()
        handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
        if settings.log_to_file:
            directory = Path(settings.log_directory)
            directory.mkdir(parents=True, exist_ok=True)
            handlers.append(
                RotatingFileHandler(
                    directory / "databridge.log",
                    maxBytes=settings.log_max_bytes,
                    backupCount=settings.log_backup_count,
                    encoding="utf-8",
                )
            )
        for handler in handlers:
            handler.setFormatter(JsonFormatter())
            logger.addHandler(handler)
        return logger
