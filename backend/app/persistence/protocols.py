"""Structural contracts consumed by transactional services."""

from contextlib import AbstractContextManager
from typing import Protocol, TypeVar
from uuid import UUID

from app.persistence.models import AuditEvent, Checkpoint, Connection, Pipeline, SyncRun

T = TypeVar("T")


class Repository(Protocol[T]):
    def add(self, record: T) -> T: ...
    def get(self, identifier: UUID) -> T | None: ...
    def list(self, *, limit: int = 50, offset: int = 0) -> list[T]: ...


class RunRepository(Repository[SyncRun], Protocol):
    def add_progress(self, identifier: UUID, count: int) -> None: ...
    def finish(self, identifier: UUID, *, succeeded: bool) -> None: ...


class CheckpointRepository(Repository[Checkpoint], Protocol):
    def advance(
        self, *, pipeline_id: UUID, run_id: UUID, stream: str, cursor: str, expected_version: int
    ) -> Checkpoint: ...


class Transaction(Protocol):
    connections: Repository[Connection]
    pipelines: Repository[Pipeline]
    runs: RunRepository
    checkpoints: CheckpointRepository
    audits: Repository[AuditEvent]

    def audit(self, event: AuditEvent) -> None: ...
    def atomic(self) -> AbstractContextManager[None]: ...
