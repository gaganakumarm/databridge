import io
import json
import logging
from pathlib import Path
from uuid import UUID

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from app.logging import JsonFormatter, configure_logging
from app.main import create_app


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(_env_file=None, environment="test", log_directory=str(tmp_path))  # type: ignore[call-arg]


def test_health_and_correlation(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        first = client.get("/health", headers={"X-Request-ID": "untrusted-secret"})
        second = client.get("/missing")
    assert first.status_code == 200
    assert first.json() == {"status": "ok", "service": "databridge"}
    assert UUID(first.headers["x-request-id"]).version == 4
    assert UUID(second.headers["x-request-id"]).version == 4
    assert first.headers["x-request-id"] != second.headers["x-request-id"]


@pytest.mark.parametrize(
    "values",
    [
        {"environment": "invalid"},
        {"log_level": "TRACE"},
        {"log_max_bytes": 0},
        {"log_backup_count": 0},
        {"log_directory": ""},
    ],
)
def test_invalid_configuration(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, **values)  # type: ignore[arg-type, call-arg]
    assert error.value.errors()[0]["loc"]


def test_structured_logs_and_private_requests(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/missing?token=query-secret",
            json={"password": "payload-secret"},
            headers={"Authorization": "Bearer header-secret"},
        )
    records = [
        json.loads(line)
        for line in (Path(settings.log_directory) / "databridge.log").read_text().splitlines()
    ]
    assert len(records) == 1
    record = records[0]
    assert set(record) == {
        "timestamp",
        "severity",
        "event",
        "request_id",
        "duration_ms",
        "status_code",
    }
    assert record["timestamp"].endswith("+00:00")
    assert record["severity"] == "INFO"
    assert record["event"] == "request_completed"
    assert record["request_id"] == response.headers["x-request-id"]
    assert record["duration_ms"] >= 0
    assert record["status_code"] == 404
    assert "secret" not in json.dumps(records)


def test_errors_are_sanitized(settings: Settings) -> None:
    app = create_app(settings)

    @app.get("/failure")
    async def failure() -> None:
        raise RuntimeError("password=exception-secret")

    with TestClient(app) as client:
        response = client.get("/failure")
    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}
    assert "x-request-id" in response.headers
    output = (Path(settings.log_directory) / "databridge.log").read_text()
    assert "exception-secret" not in output
    assert "Traceback" not in response.text + output
    assert json.loads(output)["error"]["detail"] == "[REDACTED]"


def test_formatter_drops_arbitrary_secrets() -> None:
    record = logging.LogRecord("databridge", logging.ERROR, "", 0, "token=message-secret", (), None)
    record.authorization = "Bearer secret"
    output = JsonFormatter().format(record)
    assert "secret" not in output
    assert json.loads(output)["event"] == "application_event"


def test_handler_setup_and_rotation(settings: Settings) -> None:
    settings.log_max_bytes = 1024
    logger = configure_logging(settings)
    old_handlers = logger.handlers[:]
    logger = configure_logging(settings)
    assert len(logger.handlers) == 2
    assert not logger.propagate
    assert not any(handler in logger.handlers for handler in old_handlers)
    logger.info("application_started")
    path = Path(settings.log_directory) / "databridge.log"
    assert len(path.read_text().splitlines()) == 1
    for _ in range(50):
        logger.info("application_started")
    assert (path.parent / "databridge.log.1").exists()
    assert len(list(path.parent.glob("databridge.log*"))) <= 4


def test_stdout_only(settings: Settings) -> None:
    settings.log_to_file = False
    logger = configure_logging(settings)
    assert len(logger.handlers) == 1
    stream = io.StringIO()
    handler = logger.handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    handler.setStream(stream)
    logger.info("application_started")
    assert json.loads(stream.getvalue())["event"] == "application_started"


def test_ci_required_checks() -> None:
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/ci.yml").read_text())
    assert workflow.get("on", workflow.get(True)) == ["push", "pull_request"]
    assert workflow["permissions"] == {"contents": "read"}
    assert set(workflow["jobs"]) == {"backend", "frontend"}
    for job, commands in {
        "backend": ["ruff check", "ruff format --check", "mypy", "pytest"],
        "frontend": ["npm ci", "npm run lint", "npm run typecheck", "npm run build"],
    }.items():
        steps = workflow["jobs"][job]["steps"]
        runs = "\n".join(str(step.get("run", "")) for step in steps)
        for command in commands:
            assert command in runs
        assert any("actions/checkout@" in step.get("uses", "") for step in steps)
