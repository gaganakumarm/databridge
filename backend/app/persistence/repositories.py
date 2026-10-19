from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.persistence.models import Base, Checkpoint, SyncRun


class ConflictError(Exception):
    """A stale version or invalid lifecycle transition; contains no database details."""


class SqlRepository[T: Base]:
    def __init__(self, session: Session, model: type[T]) -> None:
        self.session = session
        self.model = model

    def add(self, record: T) -> T:
        self.session.add(record)
        self.session.flush()
        return record

    def get(self, identifier: UUID) -> T | None:
        return self.session.get(self.model, identifier)

    def list(self, *, limit: int = 50, offset: int = 0) -> list[T]:
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("Invalid pagination")
        # All persistence records expose an id; no eager relationships or N+1 queries.
        return list(
            self.session.scalars(
                select(self.model).order_by(self.model.__table__.c.id).limit(limit).offset(offset)
            )
        )


class SqlRunRepository(SqlRepository[SyncRun]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, SyncRun)

    def add_progress(self, identifier: UUID, count: int) -> None:
        if count < 0:
            raise ValueError("Progress must be nonnegative")
        result = self.session.scalar(
            update(SyncRun)
            .where(
                SyncRun.id == identifier,
                SyncRun.status == "running",
            )
            .values(records_processed=SyncRun.records_processed + count)
            .returning(SyncRun.id)
        )
        if result is None:
            raise ConflictError("Run is not running")

    def finish(self, identifier: UUID, *, succeeded: bool) -> None:
        result = self.session.scalar(
            update(SyncRun)
            .where(
                SyncRun.id == identifier,
                SyncRun.status == "running",
            )
            .values(
                status="succeeded" if succeeded else "failed", finished_at=func.clock_timestamp()
            )
            .returning(SyncRun.id)
        )
        if result is None:
            raise ConflictError("Run is not running")


class SqlCheckpointRepository(SqlRepository[Checkpoint]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, Checkpoint)

    def advance(
        self, *, pipeline_id: UUID, run_id: UUID, stream: str, cursor: str, expected_version: int
    ) -> Checkpoint:
        if expected_version < 0:
            raise ValueError("Version must be nonnegative")
        if expected_version == 0:
            statement = (
                insert(Checkpoint)
                .values(
                    id=uuid4(),
                    pipeline_id=pipeline_id,
                    run_id=run_id,
                    stream=stream,
                    cursor=cursor,
                    version=1,
                )
                .on_conflict_do_nothing(constraint="uq_checkpoints_pipeline_stream")
            )
            result = self.session.scalar(statement.returning(Checkpoint))
        else:
            result = self.session.scalar(
                update(Checkpoint)
                .where(
                    Checkpoint.pipeline_id == pipeline_id,
                    Checkpoint.stream == stream,
                    Checkpoint.version == expected_version,
                )
                .values(
                    run_id=run_id,
                    cursor=cursor,
                    version=expected_version + 1,
                    updated_at=func.clock_timestamp(),
                )
                .returning(Checkpoint),
                execution_options={"populate_existing": True},
            )
        if result is None:
            raise ConflictError("Checkpoint version conflict")
        return result
