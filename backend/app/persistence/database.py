import logging
from collections.abc import Iterator
from contextlib import contextmanager
from types import TracebackType
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, Request
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.persistence.models import AuditAction, AuditEvent, Connection, Pipeline
from app.persistence.protocols import (
    CheckpointRepository,
    Repository,
    RunRepository,
)
from app.persistence.repositories import SqlCheckpointRepository, SqlRepository, SqlRunRepository


class Database:
    def __init__(self, settings: Settings) -> None:
        if settings.database_url is None:
            raise ValueError("Database URL is required for persistence")
        self.engine: Engine = create_engine(
            settings.database_url.get_secret_value(),
            echo=False,
            hide_parameters=True,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
            pool_timeout=10,
            connect_args={"connect_timeout": 5, "options": "-c statement_timeout=10000"},
        )
        self.sessions = sessionmaker(self.engine, expire_on_commit=False, close_resets_only=False)

    def dispose(self) -> None:
        self.engine.dispose()


class UnitOfWork:
    """One transaction per scope; always close, including failed flush/commit paths."""

    def __init__(self, factory: sessionmaker[Session], *, request_id: UUID | None = None) -> None:
        self.session = factory()
        self.request_id = request_id
        self._audit_events: list[AuditEvent] = []
        self.connections: Repository[Connection] = SqlRepository(self.session, Connection)
        self.pipelines: Repository[Pipeline] = SqlRepository(self.session, Pipeline)
        self.runs: RunRepository = SqlRunRepository(self.session)
        self.checkpoints: CheckpointRepository = SqlCheckpointRepository(self.session)
        self.audits: Repository[AuditEvent] = SqlRepository(self.session, AuditEvent)

    def __enter__(self) -> "UnitOfWork":
        self.session.begin()
        return self

    def audit(self, event: AuditEvent) -> None:
        if len(self._audit_events) >= 100:
            raise ValueError("Transaction audit limit exceeded")
        if event.request_id is None:
            event.request_id = self.request_id
        self.audits.add(event)
        self._audit_events.append(event)

    @contextmanager
    def atomic(self) -> Iterator[None]:
        audit_count = len(self._audit_events)
        try:
            with self.session.begin_nested():
                yield
        except BaseException:
            del self._audit_events[audit_count:]
            raise

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            if exc_type is None:
                self.session.commit()
                for event in self._audit_events:
                    logging.getLogger("databridge").info(
                        "audit_event",
                        extra={
                            "audit_id": event.id,
                            "audit_action": AuditAction(event.action),
                            "entity_id": event.entity_id,
                            "request_id": str(event.request_id) if event.request_id else None,
                        },
                    )
            else:
                self.session.rollback()
        except BaseException:
            self.session.rollback()
            raise
        finally:
            self.session.close()


def get_unit_of_work(request: Request) -> Iterator[UnitOfWork]:
    database: Database | None = request.app.state.database
    if database is None:
        raise HTTPException(status_code=503, detail="Persistence is not configured")
    with UnitOfWork(database.sessions, request_id=UUID(request.state.request_id)) as transaction:
        yield transaction


TransactionDependency = Annotated[UnitOfWork, Depends(get_unit_of_work, scope="function")]
