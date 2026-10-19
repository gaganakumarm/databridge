import json
import logging
import math
import sys
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Lock
from uuid import UUID

from app.config import Settings
from app.connectors.types import ConnectorKind, ErrorCode
from app.persistence.models import AuditAction

_setup_lock = Lock()
# Only fixed event names and explicitly approved metadata can reach the output.
_CONNECTOR_EVENTS = {
    "connector_started",
    "connector_page_fetched",
    "connector_retry",
    "connector_completed",
    "connector_failed",
}
_EVENTS = {
    "request_completed",
    "request_failed",
    "application_started",
    "audit_event",
} | _CONNECTOR_EVENTS


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        event = (
            record.msg
            if isinstance(record.msg, str) and record.msg in _EVENTS
            else "application_event"
        )
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
        if event == "audit_event":
            try:
                data["request_id"] = str(UUID(str(getattr(record, "request_id", ""))))
            except ValueError:
                data["request_id"] = None
            action = getattr(record, "audit_action", None)
            if isinstance(action, AuditAction):
                data["action"] = action.value
            for key in ("audit_id", "entity_id"):
                value = getattr(record, key, None)
                if isinstance(value, UUID):
                    data[key] = str(value)
        if event in _CONNECTOR_EVENTS:
            identifier = getattr(record, "request_id", None)
            data["request_id"] = str(identifier) if isinstance(identifier, UUID) else None
            duration = getattr(record, "duration_ms", None)
            data["duration_ms"] = (
                duration
                if isinstance(duration, (int, float))
                and math.isfinite(duration)
                and 0 <= duration <= 300_000
                else None
            )
            kind = getattr(record, "connector_kind", None)
            if isinstance(kind, ConnectorKind):
                data["connector"] = kind.value
            for key in ("page_number", "record_count"):
                value = getattr(record, key, None)
                if type(value) is int and 0 <= value <= 10_000:
                    data[key] = value
            category = getattr(record, "status_category", None)
            if isinstance(category, str) and category in {
                "local",
                "transport",
                "http_2xx",
                "http_3xx",
                "http_4xx",
                "http_5xx",
            }:
                data["status_category"] = category
            error = getattr(record, "connector_error", None)
            if isinstance(error, ErrorCode):
                data["error_code"] = error.value
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


def suppress_http_transport_logs() -> None:
    """DataBridge owns HTTP telemetry; third-party debug/request logs contain raw URLs."""
    for name in ("httpx", "httpcore"):
        transport_logger = logging.getLogger(name)
        transport_logger.handlers = [logging.NullHandler()]
        transport_logger.propagate = False
