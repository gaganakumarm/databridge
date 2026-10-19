"""Persistence records only: no connector configuration or credentials."""

from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class AuditAction(StrEnum):
    CONNECTION_CREATED = "connection_created"
    PIPELINE_CREATED = "pipeline_created"
    RUN_STARTED = "run_started"
    RUN_FINISHED = "run_finished"
    CHECKPOINT_ADVANCED = "checkpoint_advanced"


class Base(DeclarativeBase):
    pass


class Record:
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Connection(Record, Base):
    __tablename__ = "connections"
    __table_args__ = (
        UniqueConstraint("name", name="uq_connections_name"),
        CheckConstraint("length(trim(name)) > 0", name="ck_connections_name"),
        CheckConstraint("kind IN ('source', 'destination')", name="ck_connections_kind"),
    )
    name: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(16))


class Pipeline(Record, Base):
    __tablename__ = "pipelines"
    __table_args__ = (
        UniqueConstraint("name", name="uq_pipelines_name"),
        CheckConstraint("length(trim(name)) > 0", name="ck_pipelines_name"),
        CheckConstraint("source_id <> destination_id", name="ck_pipelines_distinct_connections"),
        CheckConstraint("status IN ('draft', 'active', 'paused')", name="ck_pipelines_status"),
        Index("ix_pipelines_source_id", "source_id"),
        Index("ix_pipelines_destination_id", "destination_id"),
    )
    name: Mapped[str] = mapped_column(String(128))
    source_id: Mapped[UUID] = mapped_column(ForeignKey("connections.id", ondelete="RESTRICT"))
    destination_id: Mapped[UUID] = mapped_column(ForeignKey("connections.id", ondelete="RESTRICT"))
    status: Mapped[str] = mapped_column(String(16), server_default="draft")


class SyncRun(Record, Base):
    __tablename__ = "sync_runs"
    __table_args__ = (
        UniqueConstraint("id", "pipeline_id", name="uq_sync_runs_id_pipeline"),
        CheckConstraint("status IN ('running', 'succeeded', 'failed')", name="ck_sync_runs_status"),
        CheckConstraint("records_processed >= 0", name="ck_sync_runs_records"),
        CheckConstraint(
            "(status = 'running' AND finished_at IS NULL) OR "
            "(status IN ('succeeded', 'failed') AND finished_at IS NOT NULL "
            "AND finished_at >= started_at)",
            name="ck_sync_runs_finished",
        ),
        Index("ix_sync_runs_pipeline_created", "pipeline_id", "created_at"),
        Index(
            "uq_sync_runs_running_pipeline",
            "pipeline_id",
            unique=True,
            postgresql_where=text("status = 'running'"),
        ),
    )
    pipeline_id: Mapped[UUID] = mapped_column(ForeignKey("pipelines.id", ondelete="RESTRICT"))
    status: Mapped[str] = mapped_column(String(16), server_default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    records_processed: Mapped[int] = mapped_column(BigInteger, server_default="0")


class Checkpoint(Record, Base):
    __tablename__ = "checkpoints"
    __table_args__ = (
        UniqueConstraint("pipeline_id", "stream", name="uq_checkpoints_pipeline_stream"),
        ForeignKeyConstraint(
            ["run_id", "pipeline_id"],
            ["sync_runs.id", "sync_runs.pipeline_id"],
            name="fk_checkpoints_run_pipeline",
            ondelete="RESTRICT",
        ),
        CheckConstraint("length(trim(stream)) > 0", name="ck_checkpoints_stream"),
        CheckConstraint("version >= 1", name="ck_checkpoints_version"),
        Index("ix_checkpoints_run_id", "run_id"),
    )
    pipeline_id: Mapped[UUID] = mapped_column(ForeignKey("pipelines.id", ondelete="RESTRICT"))
    run_id: Mapped[UUID] = mapped_column()
    stream: Mapped[str] = mapped_column(String(128))
    cursor: Mapped[str] = mapped_column(String(4096))
    version: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditEvent(Record, Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint(
            "action IN ('connection_created', 'pipeline_created', 'run_started', "
            "'run_finished', 'checkpoint_advanced')",
            name="ck_audit_events_action",
        ),
        Index("ix_audit_events_entity_created", "entity_id", "created_at"),
        Index("ix_audit_events_created", "created_at"),
    )
    action: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[UUID] = mapped_column()
    request_id: Mapped[UUID | None] = mapped_column()
    actor_id: Mapped[UUID | None] = mapped_column()
