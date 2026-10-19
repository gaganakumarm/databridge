import io
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from alembic import command
from conftest import migration_config
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import delete, event, inspect, select, text, update
from sqlalchemy.exc import IntegrityError, InvalidRequestError
from sqlalchemy.pool import QueuePool
from sqlalchemy.sql import Executable

from app.config import Settings
from app.logging import JsonFormatter, configure_logging
from app.main import create_app
from app.persistence.database import Database, TransactionDependency, UnitOfWork
from app.persistence.models import AuditEvent, Checkpoint, Connection, Pipeline, SyncRun
from app.persistence.repositories import ConflictError
from app.persistence.service import MetadataService


def seed(database: Database) -> tuple[UUID, UUID, UUID, UUID]:
    with UnitOfWork(database.sessions) as work:
        service = MetadataService(work)
        source = service.create_connection("source", "source")
        destination = service.create_connection("destination", "destination")
        pipeline = service.create_pipeline("pipeline", source.id, destination.id)
        run = service.start_run(pipeline.id)
        return source.id, destination.id, pipeline.id, run.id


def advance(
    work: UnitOfWork, pipeline: UUID, run: UUID, version: int, cursor: str = "cursor-secret"
) -> Checkpoint:
    return MetadataService(work).advance_checkpoint(
        pipeline_id=pipeline,
        run_id=run,
        stream="stream-secret",
        cursor=cursor,
        expected_version=version,
        records_processed=7,
    )


def test_repository_create_read_update_pagination(database: Database) -> None:
    source_id, _, pipeline_id, run_id = seed(database)
    with UnitOfWork(database.sessions) as work:
        source = work.connections.get(source_id)
        assert source is not None
        assert source.created_at.tzinfo is not None
        source.name = "renamed"
        assert work.connections.get(uuid4()) is None
        first = work.connections.list(limit=1)
        second = work.connections.list(limit=1, offset=1)
        assert len(first) == len(second) == 1
        assert first[0].id != second[0].id
        pipeline = work.pipelines.get(pipeline_id)
        run = work.runs.get(run_id)
        assert pipeline is not None and pipeline.status == "draft"
        assert run is not None and run.status == "running" and run.records_processed == 0
        with pytest.raises(ValueError, match="pagination"):
            work.connections.list(limit=101)
        with pytest.raises(ValueError, match="pagination"):
            work.connections.list(offset=-1)
    with UnitOfWork(database.sessions) as work:
        saved = work.connections.get(source_id)
        assert saved is not None and saved.name == "renamed"


@pytest.mark.parametrize("kind,name", [("invalid", "a"), ("source", " ")])
def test_connection_checks(database: Database, kind: str, name: str) -> None:
    with pytest.raises(IntegrityError), UnitOfWork(database.sessions) as work:
        work.connections.add(Connection(name=name, kind=kind))


def test_uniqueness_and_restricted_deletion(database: Database) -> None:
    source, destination, pipeline, _ = seed(database)
    with pytest.raises(IntegrityError), UnitOfWork(database.sessions) as work:
        work.connections.add(Connection(name="source", kind="source"))
    with pytest.raises(IntegrityError), UnitOfWork(database.sessions) as work:
        work.pipelines.add(Pipeline(name="pipeline", source_id=source, destination_id=destination))
    with pytest.raises(IntegrityError), UnitOfWork(database.sessions) as work:
        work.runs.add(SyncRun(pipeline_id=pipeline))
    with pytest.raises(IntegrityError), database.engine.begin() as connection:
        connection.execute(delete(Connection).where(Connection.id == source))
    with pytest.raises(IntegrityError), database.engine.begin() as connection:
        connection.execute(delete(Pipeline).where(Pipeline.id == pipeline))
    # Unreferenced metadata can be deleted; history references are protected.
    with UnitOfWork(database.sessions) as work:
        spare = work.connections.add(Connection(name="spare", kind="source"))
    with database.engine.begin() as connection:
        assert connection.execute(delete(Connection).where(Connection.id == spare.id)).rowcount == 1


def test_foreign_keys_and_pipeline_checks(database: Database) -> None:
    source, destination, _, _ = seed(database)
    for source_id, destination_id, status in [
        (uuid4(), destination, "draft"),
        (source, uuid4(), "draft"),
        (source, source, "draft"),
        (source, destination, "bad"),
    ]:
        with pytest.raises(IntegrityError), UnitOfWork(database.sessions) as work:
            work.pipelines.add(
                Pipeline(
                    name="invalid",
                    source_id=source_id,
                    destination_id=destination_id,
                    status=status,
                )
            )
    with pytest.raises(IntegrityError), UnitOfWork(database.sessions) as work:
        work.runs.add(SyncRun(pipeline_id=uuid4()))
    with UnitOfWork(database.sessions) as work:
        service = MetadataService(work)
        with pytest.raises(ValueError, match="does not exist"):
            service.create_pipeline("invalid", uuid4(), destination)
        with pytest.raises(ValueError, match="roles"):
            service.create_pipeline("invalid", destination, source)


def test_commit_rollback_and_session_cleanup(database: Database) -> None:
    pool = database.engine.pool
    assert isinstance(pool, QueuePool)
    with pytest.raises(RuntimeError, match="abort"), UnitOfWork(database.sessions) as work:
        MetadataService(work).create_connection("rollback-secret", "source")
        raise RuntimeError("abort")
    assert pool.checkedout() == 0
    with pytest.raises(InvalidRequestError):
        work.session.execute(select(Connection))
    with UnitOfWork(database.sessions) as read:
        assert read.connections.list() == []
        assert read.audits.list() == []
    with pytest.raises(IntegrityError), UnitOfWork(database.sessions) as failed:
        failed.connections.add(Connection(name="bad", kind="invalid"))
    assert pool.checkedout() == 0
    with pytest.raises(InvalidRequestError):
        failed.session.execute(select(Connection))
    # A failure deferred until commit must also release the connection.
    with pytest.raises(IntegrityError), UnitOfWork(database.sessions) as deferred:
        deferred.session.add(Connection(name="bad", kind="invalid"))
    assert pool.checkedout() == 0
    with UnitOfWork(database.sessions) as successful:
        successful.connections.add(Connection(name="good", kind="source"))
    assert pool.checkedout() == 0
    with pytest.raises(InvalidRequestError):
        successful.session.execute(select(Connection))


def test_checkpoint_atomicity_stale_updates_and_lifecycle(database: Database) -> None:
    _, _, pipeline, run = seed(database)
    with UnitOfWork(database.sessions) as work:
        first = advance(work, pipeline, run, 0)
        identifier = first.id
        assert first.version == 1
    for version in [0, 99]:
        # Even if a caller catches the conflict, the savepoint rolls back progress.
        with UnitOfWork(database.sessions) as work:
            with pytest.raises(ConflictError, match="version conflict"):
                advance(work, pipeline, run, version, "never-committed")
    with UnitOfWork(database.sessions) as work:
        second = advance(work, pipeline, run, 1, "second-secret")
        assert second.id == identifier and second.version == 2
        assert second.updated_at >= second.created_at
    with pytest.raises(RuntimeError), UnitOfWork(database.sessions) as work:
        advance(work, pipeline, run, 2, "rolled-back")
        raise RuntimeError("cancel")
    with UnitOfWork(database.sessions) as work:
        saved = work.checkpoints.get(identifier)
        saved_run = work.runs.get(run)
        assert saved is not None and saved.cursor == "second-secret" and saved.version == 2
        assert saved_run is not None and saved_run.records_processed == 14
        assert len(work.audits.list()) == 6
        service = MetadataService(work)
        with pytest.raises(ConflictError, match="belong"):
            advance(work, uuid4(), run, 2)
        with pytest.raises(ConflictError, match="belong"):
            advance(work, pipeline, uuid4(), 2)
        with pytest.raises(ValueError, match="nonnegative"):
            service.advance_checkpoint(
                pipeline_id=pipeline,
                run_id=run,
                stream="a",
                cursor="x",
                expected_version=0,
                records_processed=-1,
            )
        with pytest.raises(ValueError, match="nonnegative"):
            advance(work, pipeline, run, -1)
        service.finish_run(run, succeeded=True)
        with pytest.raises(ConflictError, match="not running"):
            advance(work, pipeline, run, 2)
        with pytest.raises(ConflictError, match="not running"):
            service.finish_run(run, succeeded=False)
    with UnitOfWork(database.sessions) as work:
        saved_run = work.runs.get(run)
        assert saved_run is not None and saved_run.status == "succeeded"
        assert saved_run.finished_at is not None
        next_run = MetadataService(work).start_run(pipeline)
        MetadataService(work).finish_run(next_run.id, succeeded=False)


def test_competing_checkpoint_writers(database: Database) -> None:
    _, _, pipeline, run = seed(database)
    with UnitOfWork(database.sessions) as work:
        advance(work, pipeline, run, 0)
    barrier = Barrier(2)

    def writer() -> str:
        try:
            with UnitOfWork(database.sessions) as work:
                barrier.wait(timeout=10)
                advance(work, pipeline, run, 1)
            return "committed"
        except ConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: writer(), range(2)))
    assert sorted(outcomes) == ["committed", "conflict"]
    with UnitOfWork(database.sessions) as work:
        checkpoint = work.checkpoints.list()[0]
        saved_run = work.runs.get(run)
        assert checkpoint.version == 2
        assert saved_run is not None and saved_run.records_processed == 14
        assert len(work.audits.list()) == 6


def test_checkpoint_database_constraints(database: Database) -> None:
    source, destination, pipeline, run = seed(database)
    with UnitOfWork(database.sessions) as work:
        other = MetadataService(work).create_pipeline("other", source, destination)
    for pipeline_id, run_id, stream, version in [
        (other.id, run, "a", 1),
        (pipeline, uuid4(), "a", 1),
        (pipeline, run, " ", 1),
        (pipeline, run, "a", 0),
    ]:
        with pytest.raises(IntegrityError), UnitOfWork(database.sessions) as work:
            work.checkpoints.add(
                Checkpoint(
                    pipeline_id=pipeline_id,
                    run_id=run_id,
                    stream=stream,
                    cursor="secret",
                    version=version,
                )
            )
    with UnitOfWork(database.sessions) as work:
        advance(work, pipeline, run, 0)
    with pytest.raises(IntegrityError), UnitOfWork(database.sessions) as work:
        work.checkpoints.add(
            Checkpoint(
                pipeline_id=pipeline,
                run_id=run,
                stream="stream-secret",
                cursor="duplicate",
                version=1,
            )
        )


@pytest.mark.parametrize(
    "values",
    [
        {"status": "invalid"},
        {"records_processed": -1},
        {"status": "succeeded"},
        {"finished_at": datetime(2000, 1, 1, tzinfo=UTC)},
        {"status": "failed", "finished_at": datetime(2000, 1, 1, tzinfo=UTC)},
    ],
)
def test_run_constraints(database: Database, values: dict[str, object]) -> None:
    _, _, _, run = seed(database)
    with pytest.raises(IntegrityError), database.engine.begin() as connection:
        connection.execute(update(SyncRun).where(SyncRun.id == run).values(**values))


def test_audit_safety_and_append_only(database: Database, tmp_path: Path) -> None:
    logger = configure_logging(Settings(log_to_file=False))
    stream = io.StringIO()
    handler = logger.handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    handler.setStream(stream)
    request_id, actor_id = uuid4(), uuid4()
    with UnitOfWork(database.sessions) as work:
        service = MetadataService(work, request_id=request_id, actor_id=actor_id)
        source = service.create_connection("password=name-secret", "source")
        assert stream.getvalue() == ""  # no successful event before commit
    rows = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert len(rows) == 1
    assert rows[0]["event"] == "audit_event" and rows[0]["action"] == "connection_created"
    assert rows[0]["entity_id"] == str(source.id)
    assert rows[0]["request_id"] == str(request_id)
    assert "secret" not in stream.getvalue() and str(actor_id) not in stream.getvalue()
    with UnitOfWork(database.sessions) as work:
        service = MetadataService(work, request_id=request_id)
        destination = service.create_connection("destination-secret", "destination")
        pipeline = service.create_pipeline("pipeline-secret", source.id, destination.id)
        run = service.start_run(pipeline.id)
        advance(work, pipeline.id, run.id, 0)
    assert "secret" not in stream.getvalue()
    assert all(
        row["request_id"] in (None, str(request_id))
        for row in map(json.loads, stream.getvalue().splitlines())
    )
    with UnitOfWork(database.sessions) as work:
        audit = next(item for item in work.audits.list() if item.entity_id == source.id)
        assert audit.actor_id == actor_id and audit.request_id == request_id
        assert set(inspect(AuditEvent).columns.keys()) == {
            "id",
            "created_at",
            "action",
            "entity_id",
            "actor_id",
            "request_id",
        }
    statements: list[Executable] = [
        update(AuditEvent).values(action="run_started"),
        delete(AuditEvent),
        text("TRUNCATE audit_events"),
    ]
    for statement in statements:
        with pytest.raises(IntegrityError), database.engine.begin() as connection:
            connection.execute(statement)
    with pytest.raises(IntegrityError), UnitOfWork(database.sessions) as work:
        work.audit(AuditEvent(action="token=secret", entity_id=source.id))
    count = len(stream.getvalue().splitlines())
    with pytest.raises(RuntimeError), UnitOfWork(database.sessions) as work:
        MetadataService(work).create_connection("rolled-back-secret", "source")
        raise RuntimeError("abort")
    assert len(stream.getvalue().splitlines()) == count
    record = logging.LogRecord("databridge", logging.INFO, "", 0, "audit_event", (), None)
    record.request_id = "secret"
    record.audit_action = "secret"
    record.audit_id = "secret"
    record.entity_id = "secret"
    assert "secret" not in JsonFormatter().format(record)


def test_audit_and_checkpoint_commit_together(database: Database) -> None:
    _, _, pipeline, run = seed(database)
    # A DB trigger rejects the audit insert after the cursor write.
    with database.engine.begin() as connection:
        connection.execute(
            text("""
            CREATE FUNCTION fail_checkpoint_audit() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN IF NEW.action = 'checkpoint_advanced' THEN
            RAISE EXCEPTION 'audit unavailable' USING ERRCODE = '23000';
            END IF; RETURN NEW; END $$
        """)
        )
        connection.execute(
            text("""
            CREATE TRIGGER fail_audit BEFORE INSERT ON audit_events
            FOR EACH ROW EXECUTE FUNCTION fail_checkpoint_audit()
        """)
        )
    with UnitOfWork(database.sessions) as work:
        with pytest.raises(IntegrityError):
            advance(work, pipeline, run, 0)
    with UnitOfWork(database.sessions) as work:
        saved_run = work.runs.get(run)
        assert saved_run is not None and saved_run.records_processed == 0
        assert work.checkpoints.list() == []
        assert len(work.audits.list()) == 4


def test_migrations_round_trip_and_model_parity(database: Database) -> None:
    with database.engine.begin() as connection:
        config = migration_config()
        config.attributes["connection"] = connection
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0001"
        command.check(config)
        command.upgrade(config, "head")
        tables = inspect(connection).get_table_names()
        assert set(tables) == {
            "alembic_version",
            "connections",
            "pipelines",
            "sync_runs",
            "checkpoints",
            "audit_events",
        }
        indexes = inspect(connection).get_indexes("sync_runs")
        assert any(
            index["name"] == "uq_sync_runs_running_pipeline" and index["unique"]
            for index in indexes
        )
    seed(database)
    with database.engine.begin() as connection:
        config = migration_config()
        config.attributes["connection"] = connection
        command.downgrade(config, "base")
        assert inspect(connection).get_table_names() == ["alembic_version"]
        command.upgrade(config, "head")
        command.check(config)
        assert connection.scalar(select(Connection.id)) is None


def test_dependency_injection_and_cleanup(database: Database) -> None:
    app = create_app(Settings(log_to_file=False))

    @app.post("/test-metadata")
    def create(work: TransactionDependency) -> dict[str, str]:
        record = MetadataService(work).create_connection("injected", "source")
        return {"id": str(record.id)}

    @app.get("/test-failure")
    def fail(work: TransactionDependency) -> None:
        MetadataService(work).create_connection("secret", "source")
        raise RuntimeError("database-password-secret")

    @app.get("/test-db-failure")
    def db_fail(work: TransactionDependency) -> None:
        work.connections.add(Connection(name="credential-secret", kind="invalid"))

    @app.get("/test-deferred-failure")
    def deferred_fail(work: TransactionDependency) -> dict[str, bool]:
        work.session.add(Connection(name="credential-secret", kind="invalid"))
        return {"ok": True}

    with TestClient(app) as client:
        assert client.post("/test-metadata").status_code == 503
        app.state.database = database
        created_response = client.post("/test-metadata")
        assert created_response.status_code == 200
        response = client.get("/test-failure")
        assert response.status_code == 500 and "secret" not in response.text
        assert "x-request-id" in response.headers
        db_response = client.get("/test-db-failure")
        assert db_response.status_code == 500
        assert db_response.json() == {"detail": "Internal server error"}
        assert "x-request-id" in db_response.headers
        deferred_response = client.get("/test-deferred-failure")
        assert deferred_response.status_code == 500
        assert deferred_response.json() == {"detail": "Internal server error"}
        assert "x-request-id" in deferred_response.headers
    pool = database.engine.pool
    assert isinstance(pool, QueuePool) and pool.checkedout() == 0
    with UnitOfWork(database.sessions) as work:
        assert [record.name for record in work.connections.list()] == ["injected"]
        assert str(work.audits.list()[0].request_id) == created_response.headers["x-request-id"]


def test_database_configuration_and_app_lifecycle(database: Database) -> None:
    for url in [
        "sqlite:///test.db",
        "postgresql://host/db",
        "not-a-url",
        "postgresql+psycopg://host",
    ]:
        with pytest.raises(ValidationError) as error:
            Settings(database_url=SecretStr(url))
        assert url not in str(error.value)
    with pytest.raises(ValueError, match="required"):
        Database(Settings(database_url=None))
    assert database.engine.hide_parameters and not database.engine.echo
    settings = Settings(
        database_url=SecretStr(database.engine.url.render_as_string(hide_password=False))
    )
    assert settings.database_url is not None
    assert "databridge" not in repr(settings.database_url)
    app = create_app(settings)
    disposed: list[bool] = []
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        app_database = app.state.database
        assert isinstance(app_database, Database)
        event.listen(app_database.engine, "engine_disposed", lambda _: disposed.append(True))
        with app_database.sessions() as session:
            assert session.scalar(text("SELECT 1")) == 1
    assert disposed == [True]
    pool = app_database.engine.pool
    assert isinstance(pool, QueuePool) and pool.checkedout() == 0


def test_audit_batch_is_bounded(database: Database) -> None:
    with UnitOfWork(database.sessions) as work:
        # A bounded transaction also bounds audit events retained for post-commit logging.
        for _ in range(100):
            work.audit(AuditEvent(action="connection_created", entity_id=uuid4()))
        with pytest.raises(ValueError, match="limit"):
            work.audit(AuditEvent(action="connection_created", entity_id=uuid4()))


def test_ci_requires_postgres_and_migrations() -> None:
    import yaml

    workflow = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / ".github/workflows/ci.yml").read_text()
    )
    backend = workflow["jobs"]["backend"]
    postgres = backend["services"]["postgres"]
    assert postgres["image"] == "postgres:17"
    assert "pg_isready" in postgres["options"]
    assert backend["env"]["DATABRIDGE_TEST_DATABASE_URL"].endswith("/databridge_test")
    commands = [step.get("run", "") for step in backend["steps"]]
    assert "python -m alembic upgrade head" in commands
    assert "python -m alembic check" in commands
    assert "pytest" in commands
