"""add resumable human approval state

Revision ID: 20260721_0005
Revises: 20260720_0004
Create Date: 2026-07-21
"""

from alembic import op
import sqlalchemy as sa

revision = "20260721_0005"
down_revision = "20260720_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pending_approvals",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("request_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("tool_name", sa.String(length=100), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="WAITING_APPROVAL"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["request_id"], ["workflow_runs.request_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pending_approvals_request_id", "pending_approvals", ["request_id"])
    op.create_index(
        "ix_pending_approval_user_status",
        "pending_approvals",
        ["user_id", "status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_pending_approval_user_status", table_name="pending_approvals")
    op.drop_index("ix_pending_approvals_request_id", table_name="pending_approvals")
    op.drop_table("pending_approvals")

