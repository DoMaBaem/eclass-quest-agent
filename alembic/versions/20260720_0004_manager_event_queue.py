"""add durable Manager delivery state to LMS change events

Revision ID: 20260720_0004
Revises: 20260720_0003
Create Date: 2026-07-20
"""

from alembic import op
import sqlalchemy as sa

revision = "20260720_0004"
down_revision = "20260720_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("change_events", sa.Column("runtime_event_id", sa.String(length=36), nullable=True))
    op.execute("UPDATE change_events SET runtime_event_id = UUID() WHERE runtime_event_id IS NULL")
    op.alter_column(
        "change_events",
        "runtime_event_id",
        existing_type=sa.String(length=36),
        nullable=False,
    )
    op.create_unique_constraint("uq_change_events_runtime_event_id", "change_events", ["runtime_event_id"])
    op.add_column(
        "change_events",
        sa.Column("manager_status", sa.String(length=32), server_default="PENDING", nullable=False),
    )
    op.add_column("change_events", sa.Column("manager_request_id", sa.String(length=36), nullable=True))
    op.add_column("change_events", sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        "ix_change_events_user_manager_status",
        "change_events",
        ["user_id", "manager_status", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_change_events_user_manager_status", table_name="change_events")
    op.drop_column("change_events", "processed_at")
    op.drop_column("change_events", "manager_request_id")
    op.drop_column("change_events", "manager_status")
    op.drop_constraint("uq_change_events_runtime_event_id", "change_events", type_="unique")
    op.drop_column("change_events", "runtime_event_id")
