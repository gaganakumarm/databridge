"""Sprint 1 metadata persistence"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=True),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "action IN ('connection_created', 'pipeline_created', 'run_started', "
            "'run_finished', 'checkpoint_advanced')",
            name="ck_audit_events_action",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_events_created", "audit_events", ["created_at"], unique=False)
    op.create_index(
        "ix_audit_events_entity_created", "audit_events", ["entity_id", "created_at"], unique=False
    )
    op.create_table(
        "connections",
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("kind IN ('source', 'destination')", name="ck_connections_kind"),
        sa.CheckConstraint("length(trim(name)) > 0", name="ck_connections_name"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_connections_name"),
    )
    op.create_table(
        "pipelines",
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("destination_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="draft", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("status IN ('draft', 'active', 'paused')", name="ck_pipelines_status"),
        sa.CheckConstraint("length(trim(name)) > 0", name="ck_pipelines_name"),
        sa.CheckConstraint("source_id <> destination_id", name="ck_pipelines_distinct_connections"),
        sa.ForeignKeyConstraint(["destination_id"], ["connections.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_id"], ["connections.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_pipelines_name"),
    )
    op.create_index("ix_pipelines_destination_id", "pipelines", ["destination_id"], unique=False)
    op.create_index("ix_pipelines_source_id", "pipelines", ["source_id"], unique=False)
    op.create_table(
        "sync_runs",
        sa.Column("pipeline_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="running", nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("records_processed", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(status = 'running' AND finished_at IS NULL) OR "
            "(status IN ('succeeded', 'failed') AND finished_at IS NOT NULL "
            "AND finished_at >= started_at)",
            name="ck_sync_runs_finished",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')", name="ck_sync_runs_status"
        ),
        sa.CheckConstraint("records_processed >= 0", name="ck_sync_runs_records"),
        sa.ForeignKeyConstraint(["pipeline_id"], ["pipelines.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "pipeline_id", name="uq_sync_runs_id_pipeline"),
    )
    op.create_index(
        "ix_sync_runs_pipeline_created", "sync_runs", ["pipeline_id", "created_at"], unique=False
    )
    op.create_index(
        "uq_sync_runs_running_pipeline",
        "sync_runs",
        ["pipeline_id"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
    )
    op.create_table(
        "checkpoints",
        sa.Column("pipeline_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("stream", sa.String(length=128), nullable=False),
        sa.Column("cursor", sa.String(length=4096), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("length(trim(stream)) > 0", name="ck_checkpoints_stream"),
        sa.CheckConstraint("version >= 1", name="ck_checkpoints_version"),
        sa.ForeignKeyConstraint(["pipeline_id"], ["pipelines.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["run_id", "pipeline_id"],
            ["sync_runs.id", "sync_runs.pipeline_id"],
            name="fk_checkpoints_run_pipeline",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("pipeline_id", "stream", name="uq_checkpoints_pipeline_stream"),
    )
    op.create_index("ix_checkpoints_run_id", "checkpoints", ["run_id"], unique=False)
    op.execute("""
        CREATE FUNCTION reject_audit_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            RAISE EXCEPTION 'Audit events are append-only' USING ERRCODE = '23000';
        END $$
    """)
    op.execute("""
        CREATE TRIGGER audit_events_immutable BEFORE UPDATE OR DELETE ON audit_events
        FOR EACH ROW EXECUTE FUNCTION reject_audit_mutation()
    """)
    op.execute("""
        CREATE TRIGGER audit_events_no_truncate BEFORE TRUNCATE ON audit_events
        FOR EACH STATEMENT EXECUTE FUNCTION reject_audit_mutation()
    """)


def downgrade() -> None:
    op.drop_index("ix_checkpoints_run_id", table_name="checkpoints")
    op.drop_table("checkpoints")
    op.drop_index(
        "uq_sync_runs_running_pipeline",
        table_name="sync_runs",
        postgresql_where=sa.text("status = 'running'"),
    )
    op.drop_index("ix_sync_runs_pipeline_created", table_name="sync_runs")
    op.drop_table("sync_runs")
    op.drop_index("ix_pipelines_source_id", table_name="pipelines")
    op.drop_index("ix_pipelines_destination_id", table_name="pipelines")
    op.drop_table("pipelines")
    op.drop_table("connections")
    op.drop_index("ix_audit_events_entity_created", table_name="audit_events")
    op.drop_index("ix_audit_events_created", table_name="audit_events")
    op.drop_table("audit_events")
    op.execute("DROP FUNCTION reject_audit_mutation()")
