"""harden LMS relations, event identity, attachments, and notifications

Revision ID: 20260720_0003
Revises: 20260720_0002
Create Date: 2026-07-20
"""

from alembic import op
import sqlalchemy as sa

revision = "20260720_0003"
down_revision = "20260720_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "eclass_sessions",
        sa.Column("status", sa.String(length=32), server_default="UNKNOWN", nullable=False),
    )
    op.add_column("eclass_sessions", sa.Column("last_error_code", sa.String(length=64), nullable=True))

    op.create_check_constraint("ck_courses_year", "courses", "year BETWEEN 2000 AND 2100")
    op.create_check_constraint("ck_courses_semester", "courses", "semester BETWEEN 1 AND 4")
    op.create_index("ix_courses_user_term", "courses", ["user_id", "year", "semester"], unique=False)

    op.add_column("assignments", sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        "ix_assignments_user_status_due", "assignments", ["user_id", "status", "due_at"], unique=False
    )
    op.create_foreign_key(
        "fk_assignments_course",
        "assignments",
        "courses",
        ["user_id", "course_eclass_id"],
        ["user_id", "eclass_id"],
        ondelete="CASCADE",
    )

    op.add_column(
        "lectures",
        sa.Column("attendance_status", sa.String(length=32), server_default="UNKNOWN", nullable=False),
    )
    op.add_column("lectures", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        "ck_lectures_progress",
        "lectures",
        "progress_percent IS NULL OR (progress_percent >= 0 AND progress_percent <= 100)",
    )
    op.create_index(
        "ix_lectures_user_status_until",
        "lectures",
        ["user_id", "status", "available_until"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_lectures_course",
        "lectures",
        "courses",
        ["user_id", "course_eclass_id"],
        ["user_id", "eclass_id"],
        ondelete="CASCADE",
    )

    op.create_index(
        "ix_announcements_user_posted", "announcements", ["user_id", "posted_at"], unique=False
    )
    op.create_foreign_key(
        "fk_announcements_course",
        "announcements",
        "courses",
        ["user_id", "course_eclass_id"],
        ["user_id", "eclass_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_grades_course",
        "grades",
        "courses",
        ["user_id", "course_eclass_id"],
        ["user_id", "eclass_id"],
        ondelete="CASCADE",
    )

    # 같은 LMS ID가 과제와 강의에 동시에 존재해도 서로 다른 엔터티로 취급한다.
    op.drop_constraint("uq_snapshot_user_entity_fingerprint", "entity_snapshots", type_="unique")
    op.create_unique_constraint(
        "uq_snapshot_user_type_entity_fingerprint",
        "entity_snapshots",
        ["user_id", "entity_type", "entity_id", "fingerprint"],
    )
    op.drop_constraint("uq_change_user_entity_fingerprint", "change_events", type_="unique")
    op.create_unique_constraint(
        "uq_change_user_type_entity_fingerprint",
        "change_events",
        ["user_id", "entity_type", "entity_id", "fingerprint"],
    )

    op.create_table(
        "attachments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("eclass_id", sa.String(length=160), nullable=False),
        sa.Column("parent_type", sa.String(length=64), nullable=False),
        sa.Column("parent_eclass_id", sa.String(length=160), nullable=False),
        sa.Column("name", sa.String(length=500), nullable=False),
        sa.Column("url", sa.String(length=2000), nullable=False),
        sa.Column("mime_type", sa.String(length=160), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("size_bytes IS NULL OR size_bytes >= 0", name="ck_attachments_size"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "eclass_id", name="uq_attachments_user_entity"),
    )
    op.create_index(
        "ix_attachments_user_parent",
        "attachments",
        ["user_id", "parent_type", "parent_eclass_id"],
        unique=False,
    )
    op.add_column("downloaded_files", sa.Column("sha256", sa.String(length=64), nullable=True))
    op.create_foreign_key(
        "fk_downloaded_files_attachment",
        "downloaded_files",
        "attachments",
        ["user_id", "attachment_id"],
        ["user_id", "eclass_id"],
        ondelete="CASCADE",
    )

    op.add_column(
        "conversation_summaries",
        sa.Column("turn_count", sa.Integer(), server_default="0", nullable=False),
    )

    # 기존 알림은 현재 식별 필드로 안전하게 backfill한 뒤 새 dedupe 계약으로 전환한다.
    op.add_column("notification_history", sa.Column("dedupe_key", sa.String(length=128), nullable=True))
    op.execute(
        "UPDATE notification_history "
        "SET dedupe_key = SHA2(CONCAT_WS('|', entity_type, entity_id, notification_type), 256) "
        "WHERE dedupe_key IS NULL"
    )
    op.alter_column(
        "notification_history",
        "dedupe_key",
        existing_type=sa.String(length=128),
        nullable=False,
    )
    op.drop_constraint("uq_notification_user_entity_type", "notification_history", type_="unique")
    op.create_unique_constraint(
        "uq_notification_user_dedupe", "notification_history", ["user_id", "dedupe_key"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_notification_user_dedupe", "notification_history", type_="unique")
    op.create_unique_constraint(
        "uq_notification_user_entity_type",
        "notification_history",
        ["user_id", "entity_type", "entity_id", "notification_type"],
    )
    op.drop_column("notification_history", "dedupe_key")
    op.drop_column("conversation_summaries", "turn_count")

    op.drop_constraint("fk_downloaded_files_attachment", "downloaded_files", type_="foreignkey")
    op.drop_column("downloaded_files", "sha256")
    op.drop_table("attachments")

    op.drop_constraint("uq_change_user_type_entity_fingerprint", "change_events", type_="unique")
    op.create_unique_constraint(
        "uq_change_user_entity_fingerprint",
        "change_events",
        ["user_id", "entity_id", "fingerprint"],
    )
    op.drop_constraint("uq_snapshot_user_type_entity_fingerprint", "entity_snapshots", type_="unique")
    op.create_unique_constraint(
        "uq_snapshot_user_entity_fingerprint",
        "entity_snapshots",
        ["user_id", "entity_id", "fingerprint"],
    )

    op.drop_constraint("fk_grades_course", "grades", type_="foreignkey")
    op.drop_constraint("fk_announcements_course", "announcements", type_="foreignkey")
    op.drop_index("ix_announcements_user_posted", table_name="announcements")

    op.drop_constraint("fk_lectures_course", "lectures", type_="foreignkey")
    op.drop_index("ix_lectures_user_status_until", table_name="lectures")
    op.drop_constraint("ck_lectures_progress", "lectures", type_="check")
    op.drop_column("lectures", "completed_at")
    op.drop_column("lectures", "attendance_status")

    op.drop_constraint("fk_assignments_course", "assignments", type_="foreignkey")
    op.drop_index("ix_assignments_user_status_due", table_name="assignments")
    op.drop_column("assignments", "submitted_at")

    op.drop_index("ix_courses_user_term", table_name="courses")
    op.drop_constraint("ck_courses_semester", "courses", type_="check")
    op.drop_constraint("ck_courses_year", "courses", type_="check")
    op.drop_column("eclass_sessions", "last_error_code")
    op.drop_column("eclass_sessions", "status")
