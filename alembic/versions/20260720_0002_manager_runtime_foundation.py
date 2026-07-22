"""manager runtime persistence foundation

Revision ID: 20260720_0002
Revises: 20260717_0001
Create Date: 2026-07-20
"""

from alembic import op
import sqlalchemy as sa

revision = "20260720_0002"
down_revision = "20260717_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 기존 workflow_runs를 삭제하지 않고 사용자 요청과 시스템 이벤트를 함께 기록하도록 확장한다.
    op.add_column(
        "workflow_runs",
        sa.Column("trigger_type", sa.String(length=48), server_default="USER_REQUEST", nullable=False),
    )
    op.add_column("workflow_runs", sa.Column("event_id", sa.String(length=36), nullable=True))
    op.add_column(
        "workflow_runs",
        sa.Column("initiated_by", sa.String(length=32), server_default="USER", nullable=False),
    )
    op.create_index("ix_workflow_runs_event_id", "workflow_runs", ["event_id"], unique=False)

    op.create_table(
        "missions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("source_type", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.String(length=160), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("priority", sa.String(length=24), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "source_type", "source_id", name="uq_missions_user_source"),
    )
    op.create_index(
        "ix_missions_user_status_due", "missions", ["user_id", "status", "due_at"], unique=False
    )

    op.create_table(
        "notification_history",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.String(length=160), nullable=False),
        sa.Column("notification_type", sa.String(length=64), nullable=False),
        sa.Column("notified_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "entity_type",
            "entity_id",
            "notification_type",
            name="uq_notification_user_entity_type",
        ),
    )
    op.create_index(
        "ix_notification_user_notified", "notification_history", ["user_id", "notified_at"], unique=False
    )

    op.create_table(
        "sync_history",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("trigger_type", sa.String(length=48), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("change_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_sync_history_user_started", "sync_history", ["user_id", "started_at"], unique=False
    )


def downgrade() -> None:
    op.drop_table("sync_history")
    op.drop_table("notification_history")
    op.drop_table("missions")
    op.drop_index("ix_workflow_runs_event_id", table_name="workflow_runs")
    op.drop_column("workflow_runs", "initiated_by")
    op.drop_column("workflow_runs", "event_id")
    op.drop_column("workflow_runs", "trigger_type")
