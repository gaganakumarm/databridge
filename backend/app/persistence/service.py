"""Metadata operations only. Caller supplies the transaction boundary."""

from uuid import UUID

from app.persistence.models import (
    AuditAction,
    AuditEvent,
    Checkpoint,
    Connection,
    Pipeline,
    SyncRun,
)
from app.persistence.protocols import Transaction
from app.persistence.repositories import ConflictError


class MetadataService:
    def __init__(
        self,
        transaction: Transaction,
        *,
        request_id: UUID | None = None,
        actor_id: UUID | None = None,
    ) -> None:
        self.transaction = transaction
        self.request_id = request_id
        self.actor_id = actor_id

    def _audit(self, action: AuditAction, entity_id: UUID) -> None:
        self.transaction.audit(
            AuditEvent(
                action=action.value,
                entity_id=entity_id,
                request_id=self.request_id,
                actor_id=self.actor_id,
            )
        )

    def create_connection(self, name: str, kind: str) -> Connection:
        with self.transaction.atomic():
            record = self.transaction.connections.add(Connection(name=name, kind=kind))
            self._audit(AuditAction.CONNECTION_CREATED, record.id)
            return record

    def create_pipeline(self, name: str, source_id: UUID, destination_id: UUID) -> Pipeline:
        with self.transaction.atomic():
            source = self.transaction.connections.get(source_id)
            destination = self.transaction.connections.get(destination_id)
            if source is None or destination is None:
                raise ValueError("Connection does not exist")
            if source.kind != "source" or destination.kind != "destination":
                raise ValueError("Connection roles do not match")
            record = self.transaction.pipelines.add(
                Pipeline(
                    name=name,
                    source_id=source_id,
                    destination_id=destination_id,
                )
            )
            self._audit(AuditAction.PIPELINE_CREATED, record.id)
            return record

    def start_run(self, pipeline_id: UUID) -> SyncRun:
        with self.transaction.atomic():
            record = self.transaction.runs.add(SyncRun(pipeline_id=pipeline_id))
            self._audit(AuditAction.RUN_STARTED, record.id)
            return record

    def advance_checkpoint(
        self,
        *,
        pipeline_id: UUID,
        run_id: UUID,
        stream: str,
        cursor: str,
        expected_version: int,
        records_processed: int,
    ) -> Checkpoint:
        with self.transaction.atomic():
            # Lock/update the run before the cursor: progress, cursor and audit commit together.
            run = self.transaction.runs.get(run_id)
            if run is None or run.pipeline_id != pipeline_id:
                raise ConflictError("Run does not belong to pipeline")
            self.transaction.runs.add_progress(run_id, records_processed)
            checkpoint = self.transaction.checkpoints.advance(
                pipeline_id=pipeline_id,
                run_id=run_id,
                stream=stream,
                cursor=cursor,
                expected_version=expected_version,
            )
            self._audit(AuditAction.CHECKPOINT_ADVANCED, checkpoint.id)
            return checkpoint

    def finish_run(self, run_id: UUID, *, succeeded: bool) -> None:
        with self.transaction.atomic():
            self.transaction.runs.finish(run_id, succeeded=succeeded)
            self._audit(AuditAction.RUN_FINISHED, run_id)
