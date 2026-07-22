"""SQLAlchemy ORM으로 정의한 MySQL 테이블 구조.

Pydantic 모델(``app.schemas``)은 Agent/MCP 사이의 전송 형식이고, 이 파일의 ORM 모델은 DB 저장
형식이다. 쿠키·비밀번호·문서 원문은 저장하지 않고 암호화 파일의 참조와 정규화 결과만 저장한다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, validates


_FORBIDDEN_USER_SETTING_KEYS = {
    "password", "passwd", "pwd", "login_id", "student_id", "cookie", "cookies",
    "token", "access_token", "refresh_token", "storage_state", "session_state",
}


def _contains_forbidden_key(value: object, forbidden: set[str]) -> bool:
    """중첩 JSON 안에 인증정보로 해석되는 키가 있는지 재귀적으로 검사한다."""

    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in forbidden or _contains_forbidden_key(nested, forbidden):
                return True
    elif isinstance(value, list):
        return any(_contains_forbidden_key(item, forbidden) for item in value)
    return False


class Base(DeclarativeBase):
    """모든 ORM 테이블이 공유하는 SQLAlchemy 메타데이터의 기준 클래스."""

    pass


class TimestampedModel:
    """생성·수정 시각이 필요한 테이블에 재사용하는 mixin."""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class UserModel(TimestampedModel, Base):
    """서비스 내부 사용자와 민감하지 않은 개인 설정."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    settings_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    @validates("settings_json")
    def validate_settings_json(self, _key: str, value: dict[str, Any]) -> dict[str, Any]:
        """사용자 설정 JSON을 인증정보의 우회 저장소로 사용하지 못하게 한다."""

        if _contains_forbidden_key(value, _FORBIDDEN_USER_SETTING_KEYS):
            raise ValueError("settings_json에는 로그인 ID·비밀번호·쿠키·토큰·세션을 저장할 수 없습니다.")
        return value


class EclassSessionModel(TimestampedModel, Base):
    """로그인 세션 원문이 아니라 암호화 파일 위치와 검증 시각만 저장."""

    __tablename__ = "eclass_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    encrypted_state_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="UNKNOWN", server_default="UNKNOWN", nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(64))


class CourseModel(TimestampedModel, Base):
    """사용자별 최신 수강 강좌."""

    __tablename__ = "courses"
    __table_args__ = (
        UniqueConstraint("user_id", "eclass_id", name="uq_courses_user_entity"),
        CheckConstraint("year BETWEEN 2000 AND 2100", name="ck_courses_year"),
        CheckConstraint("semester BETWEEN 1 AND 4", name="ck_courses_semester"),
        Index("ix_courses_user_term", "user_id", "year", "semester"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    eclass_id: Mapped[str] = mapped_column(String(160), nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    professor: Mapped[str | None] = mapped_column(String(160))
    url: Mapped[str] = mapped_column(String(2_000), nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    semester: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)


class AssignmentModel(TimestampedModel, Base):
    """사용자별 최신 과제와 제출 상태."""

    __tablename__ = "assignments"
    __table_args__ = (
        UniqueConstraint("user_id", "eclass_id", name="uq_assignments_user_entity"),
        ForeignKeyConstraint(
            ["user_id", "course_eclass_id"],
            ["courses.user_id", "courses.eclass_id"],
            name="fk_assignments_course",
            ondelete="CASCADE",
        ),
        Index("ix_assignments_user_status_due", "user_id", "status", "due_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    eclass_id: Mapped[str] = mapped_column(String(160), nullable=False)
    course_eclass_id: Mapped[str] = mapped_column(String(160), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    url: Mapped[str] = mapped_column(String(2_000), nullable=False)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted: Mapped[bool | None] = mapped_column(Boolean)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), nullable=False)


class LectureModel(TimestampedModel, Base):
    """사용자별 온라인 강의와 진도율."""

    __tablename__ = "lectures"
    __table_args__ = (
        UniqueConstraint("user_id", "eclass_id", name="uq_lectures_user_entity"),
        ForeignKeyConstraint(
            ["user_id", "course_eclass_id"],
            ["courses.user_id", "courses.eclass_id"],
            name="fk_lectures_course",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "progress_percent IS NULL OR (progress_percent >= 0 AND progress_percent <= 100)",
            name="ck_lectures_progress",
        ),
        Index("ix_lectures_user_status_until", "user_id", "status", "available_until"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    eclass_id: Mapped[str] = mapped_column(String(160), nullable=False)
    course_eclass_id: Mapped[str] = mapped_column(String(160), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    url: Mapped[str] = mapped_column(String(2_000), nullable=False)
    week: Mapped[int | None] = mapped_column(Integer)
    progress_percent: Mapped[float | None] = mapped_column()
    available_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    available_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attendance_status: Mapped[str] = mapped_column(
        String(32), default="UNKNOWN", server_default="UNKNOWN", nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), nullable=False)


class AnnouncementModel(TimestampedModel, Base):
    """강좌 또는 학교 공지."""

    __tablename__ = "announcements"
    __table_args__ = (
        UniqueConstraint("user_id", "eclass_id", name="uq_announcements_user_entity"),
        ForeignKeyConstraint(
            ["user_id", "course_eclass_id"],
            ["courses.user_id", "courses.eclass_id"],
            name="fk_announcements_course",
            ondelete="CASCADE",
        ),
        Index("ix_announcements_user_posted", "user_id", "posted_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    eclass_id: Mapped[str] = mapped_column(String(160), nullable=False)
    course_eclass_id: Mapped[str | None] = mapped_column(String(160))
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    url: Mapped[str] = mapped_column(String(2_000), nullable=False)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), nullable=False)


class GradeModel(TimestampedModel, Base):
    """공개된 성적 항목. 점수 표현은 LMS 원문 형식을 보존해 문자열로 둔다."""

    __tablename__ = "grades"
    __table_args__ = (
        UniqueConstraint("user_id", "eclass_id", name="uq_grades_user_entity"),
        ForeignKeyConstraint(
            ["user_id", "course_eclass_id"],
            ["courses.user_id", "courses.eclass_id"],
            name="fk_grades_course",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    eclass_id: Mapped[str] = mapped_column(String(160), nullable=False)
    course_eclass_id: Mapped[str] = mapped_column(String(160), nullable=False)
    item: Mapped[str] = mapped_column(String(300), nullable=False)
    score: Mapped[str | None] = mapped_column(String(80))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), nullable=False)


class EntitySnapshotModel(Base):
    """변경 감지를 위해 특정 시점의 정규화 payload와 fingerprint를 보관."""

    __tablename__ = "entity_snapshots"
    # 같은 사용자·엔터티·fingerprint 조합은 한 번만 저장한다.
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "entity_type",
            "entity_id",
            "fingerprint",
            name="uq_snapshot_user_type_entity_fingerprint",
        ),
        Index("ix_snapshot_lookup", "user_id", "entity_type", "entity_id", "observed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(160), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ChangeEventModel(Base):
    """이전 snapshot과 달라졌을 때만 생성되는 사용자 알림 후보."""

    __tablename__ = "change_events"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "entity_type",
            "entity_id",
            "fingerprint",
            name="uq_change_user_type_entity_fingerprint",
        ),
        Index("ix_change_events_user_created", "user_id", "created_at"),
        Index("ix_change_events_user_manager_status", "user_id", "manager_status", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(160), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    runtime_event_id: Mapped[str] = mapped_column(
        String(36), default=lambda: str(uuid4()), unique=True, nullable=False
    )
    manager_status: Mapped[str] = mapped_column(
        String(32), default="PENDING", server_default="PENDING", nullable=False
    )
    manager_request_id: Mapped[str | None] = mapped_column(String(36))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class WorkflowRunModel(TimestampedModel, Base):
    """요청별 Agent 실행 단계와 최종 결과 감사 로그."""

    __tablename__ = "workflow_runs"

    request_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # 사용자 입력과 시스템 능동 이벤트가 같은 실행 테이블을 사용하므로 시작 원인을 함께 기록한다.
    trigger_type: Mapped[str] = mapped_column(String(48), default="USER_REQUEST", nullable=False)
    event_id: Mapped[str | None] = mapped_column(String(36), index=True)
    initiated_by: Mapped[str] = mapped_column(String(32), default="USER", nullable=False)
    status: Mapped[str] = mapped_column(String(48), nullable=False)
    steps_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(64))


class PlaybackRunModel(TimestampedModel, Base):
    """영상 재생 요청의 시작·완료·실패 상태 기록."""

    __tablename__ = "playback_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    lecture_id: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(48), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))


class PendingApprovalModel(Base):
    """향후 상태 변경 Tool을 동일 workflow run에서 안전하게 재개하기 위한 승인 상태."""

    __tablename__ = "pending_approvals"
    __table_args__ = (
        Index("ix_pending_approval_user_status", "user_id", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    request_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_runs.request_id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="WAITING_APPROVAL", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    @validates("payload")
    def validate_payload(self, _key: str, value: dict[str, Any]) -> dict[str, Any]:
        if _contains_forbidden_key(value, _FORBIDDEN_USER_SETTING_KEYS):
            raise ValueError("승인 재개 payload에는 인증정보를 저장할 수 없습니다.")
        return value


class AttachmentModel(TimestampedModel, Base):
    """LMS 첨부파일의 메타데이터. 파일 본문은 downloaded_files가 임시 관리한다."""

    __tablename__ = "attachments"
    __table_args__ = (
        UniqueConstraint("user_id", "eclass_id", name="uq_attachments_user_entity"),
        CheckConstraint("size_bytes IS NULL OR size_bytes >= 0", name="ck_attachments_size"),
        Index("ix_attachments_user_parent", "user_id", "parent_type", "parent_eclass_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    eclass_id: Mapped[str] = mapped_column(String(160), nullable=False)
    parent_type: Mapped[str] = mapped_column(String(64), nullable=False)
    parent_eclass_id: Mapped[str] = mapped_column(String(160), nullable=False)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    url: Mapped[str] = mapped_column(String(2_000), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(160))
    size_bytes: Mapped[int | None] = mapped_column(Integer)


class DownloadedFileModel(TimestampedModel, Base):
    """문서 분석을 위해 일시 다운로드한 파일의 위치와 만료 상태."""

    __tablename__ = "downloaded_files"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "attachment_id"],
            ["attachments.user_id", "attachments.eclass_id"],
            name="fk_downloaded_files_attachment",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    attachment_id: Mapped[str] = mapped_column(String(160), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(1_000), nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ConversationSummaryModel(TimestampedModel, Base):
    """원문 대신 보관하는 사용자별 최신 안전 대화 요약."""

    __tablename__ = "conversation_summaries"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    last_request_id: Mapped[str | None] = mapped_column(String(36))
    turn_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)


class MissionModel(TimestampedModel, Base):
    """검증된 LMS 엔터티에서 생성한 사용자 미션과 완료 상태."""

    __tablename__ = "missions"
    __table_args__ = (
        UniqueConstraint("user_id", "source_type", "source_id", name="uq_missions_user_source"),
        Index("ix_missions_user_status_due", "user_id", "status", "due_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(String(160), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    priority: Mapped[str] = mapped_column(String(24), nullable=False)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NotificationHistoryModel(Base):
    """같은 마감 단계나 변경 알림을 반복 표시하지 않기 위한 기록."""

    __tablename__ = "notification_history"
    __table_args__ = (
        UniqueConstraint("user_id", "dedupe_key", name="uq_notification_user_dedupe"),
        Index("ix_notification_user_notified", "user_id", "notified_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(160), nullable=False)
    notification_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # 같은 종류라도 마감 시각·상태 버전이 달라지면 새 알림을 허용한다.
    dedupe_key: Mapped[str] = mapped_column(String(128), nullable=False)
    notified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SyncHistoryModel(Base):
    """TUI 실행 중 수행한 동기화의 시작 원인과 결과."""

    __tablename__ = "sync_history"
    __table_args__ = (Index("ix_sync_history_user_started", "user_id", "started_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    trigger_type: Mapped[str] = mapped_column(String(48), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    change_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
