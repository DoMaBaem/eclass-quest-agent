"""LMS Manager의 계획, 전문 Agent 실행 결과, 사용자용 최종 결과 계약."""

from __future__ import annotations

import re
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.workflow import CapabilityCode, ErrorCode, InteractionMode


class ManagerStatus(str, Enum):
    """Runtime 한 번의 외부 공개 상태."""

    COMPLETED = "COMPLETED"
    NO_ACTION = "NO_ACTION"
    CAPABILITY_NOT_READY = "CAPABILITY_NOT_READY"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    FAILED = "FAILED"


class ManagerPriority(str, Enum):
    """TUI가 알림 강조 수준을 결정할 때 사용하는 우선순위."""

    NONE = "NONE"
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    URGENT = "URGENT"


class ExecutionTargetName(str, Enum):
    """Manager 계획이 Runtime에 요청할 수 있는 실행 대상.

    E-Class와 Document만 전문 Agent 책임 경계다. Mission은 모델이 아니라 MySQL 규칙
    서비스이며, ``MISSION``은 이전 호출자 호환을 위한 enum 별칭이다.
    """

    ECLASS = "E-Class Agent"
    DOCUMENT = "Document Analysis Agent"
    MISSION_SERVICE = "Mission Service"
    MISSION = "Mission Service"


# 외부 호출자와 기존 테스트의 import를 깨지 않기 위한 호환 별칭이다. 새 코드와 문서에서는
# ``ExecutionTargetName``을 사용해 Mission Service를 Agent로 오해하지 않게 한다.
SpecialistAgentName = ExecutionTargetName


# 문서 분석 실행 권한으로 인정하는 다운로드 참조 형식이다. UUID는 다운로드 저장소가 발급한
# 식별자이고 마지막 값은 E-Class MCP가 검증한 첨부파일 식별자다. 사용자 자연어에서 이 모양의
# 문자열을 발견했다는 사실만으로는 권한이 생기지 않으며 Runtime이 아래 형식으로 전달해야 한다.
_VERIFIED_DOWNLOAD_REF_PATTERN = re.compile(
    r"download:"
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
    r":([A-Za-z0-9._:-]{1,160})"
)


def parse_verified_download_ref(value: str) -> tuple[str, str] | None:
    """정확한 검증 다운로드 참조를 ``(download_id, attachment_id)``로 분해한다."""

    match = _VERIFIED_DOWNLOAD_REF_PATTERN.fullmatch(value)
    if match is None:
        return None
    return match.group(1), match.group(2)


class ManagerEntityKind(str, Enum):
    """Manager 작업이 다루는 업무 엔터티의 고정 종류.

    자연어 ``instruction``만으로 대상을 다시 추측하지 않도록 Manager와 Runtime 사이의
    최소 의미 계약으로 사용한다.
    """

    COURSE = "COURSE"
    ANNOUNCEMENT = "ANNOUNCEMENT"
    ASSIGNMENT = "ASSIGNMENT"
    ATTACHMENT = "ATTACHMENT"
    LECTURE = "LECTURE"
    GRADE = "GRADE"
    DOCUMENT = "DOCUMENT"
    MISSION = "MISSION"


class ManagerAction(str, Enum):
    """Manager 작업이 엔터티에 수행하려는 고정 동작."""

    LIST = "LIST"
    DETAIL = "DETAIL"
    DOWNLOAD = "DOWNLOAD"
    PLAY = "PLAY"
    STOP = "STOP"
    PREVIEW = "PREVIEW"
    ANALYZE = "ANALYZE"
    LIST_MISSIONS = "LIST_MISSIONS"
    COMPLETE = "COMPLETE"
    UPDATE = "UPDATE"


class ManagerTaskSlots(BaseModel):
    """Manager가 사용자 문장에서 보존해야 하는 선택 조건.

    값이 없는 필드는 ``None``으로 두며 Runtime은 자연어보다 이 값을 먼저 사용한다.
    ``filter``는 "이번 주", "미제출만"처럼 아직 별도 필드가 없는 제한 조건을 보존한다.
    """

    model_config = ConfigDict(extra="forbid")

    year: int | None = Field(default=None, ge=2000, le=2100)
    semester: int | None = Field(default=None, ge=1, le=4)
    course_query: str | None = Field(default=None, min_length=1, max_length=300)
    query: str | None = Field(default=None, min_length=1, max_length=500)
    week: int | None = Field(default=None, ge=1, le=99)
    ordinal: int | None = Field(default=None, ge=1, le=100)
    filter: str | None = Field(default=None, min_length=1, max_length=300)
    # Mission 완료·수정은 자연어에서 숫자를 다시 추측하지 않고 이 검증된 ID만 사용한다.
    mission_id: int | None = Field(default=None, ge=1)


def _legacy_task_contract(
    capability: CapabilityCode,
    instruction: str,
) -> tuple[ManagerEntityKind, ManagerAction, ManagerTaskSlots]:
    """typed 필드가 없던 기존 호출을 안전하게 새 계약으로 변환한다.

    이 함수는 이전 Python 호출자와 저장된 테스트 데이터의 호환용이다. 새 Manager 출력은
    프롬프트에서 entity/action/slots를 직접 채우도록 요구한다.
    """

    compact = "".join(instruction.casefold().split())
    if capability is CapabilityCode.DOCUMENT_ANALYSIS:
        entity = ManagerEntityKind.DOCUMENT
        action = ManagerAction.ANALYZE
    elif capability is CapabilityCode.MISSION_MANAGEMENT:
        entity = ManagerEntityKind.MISSION
        if any(word in compact for word in ("완료", "끝냄", "끝냈")):
            action = ManagerAction.COMPLETE
        elif any(word in compact for word in ("목록", "조회", "보여", "알려")):
            action = ManagerAction.LIST_MISSIONS
        else:
            action = ManagerAction.UPDATE
    elif capability is CapabilityCode.VIDEO_PLAY:
        entity = ManagerEntityKind.LECTURE
        if any(word in compact for word in ("중지", "정지", "멈춰", "꺼", "닫아")):
            action = ManagerAction.STOP
        elif any(word in compact for word in ("미리보기", "시연", "데모")):
            action = ManagerAction.PREVIEW
        else:
            action = ManagerAction.PLAY
    else:
        if "공지" in compact:
            entity = ManagerEntityKind.ANNOUNCEMENT
        elif "과제" in compact:
            entity = ManagerEntityKind.ASSIGNMENT
        elif any(word in compact for word in ("첨부", "파일", "pdf", "docx", "hwp")):
            entity = ManagerEntityKind.ATTACHMENT
        elif any(word in compact for word in ("강의", "영상", "출석", "수강")):
            entity = ManagerEntityKind.LECTURE
        elif "성적" in compact:
            entity = ManagerEntityKind.GRADE
        else:
            entity = ManagerEntityKind.COURSE

        if any(word in compact for word in ("다운로드", "내려받")):
            action = ManagerAction.DOWNLOAD
        elif any(word in compact for word in ("내용", "본문", "상세", "세부", "자세히")):
            action = ManagerAction.DETAIL
        else:
            action = ManagerAction.LIST

    year_match = re.search(r"(?<!\d)(20\d{2})\s*년?", instruction)
    semester_match = re.search(r"(?<!\d)([1-4])\s*학기", instruction)
    week_match = re.search(r"(?<!\d)(\d{1,2})\s*주차", instruction)
    number_match = re.search(r"(?<!\d)(\d{1,3})\s*번", instruction)
    compact_ordinals = {
        "첫번째": 1,
        "첫째": 1,
        "두번째": 2,
        "둘째": 2,
        "세번째": 3,
        "셋째": 3,
        "네번째": 4,
        "넷째": 4,
        "다섯번째": 5,
    }
    ordinal = (
        int(number_match.group(1))
        if number_match
        else next((value for word, value in compact_ordinals.items() if word in compact), None)
    )
    mission_id_match = re.search(r"(?:미션|mission)\s*#?\s*(\d+)", instruction, re.IGNORECASE)
    mission_filter = None
    mission_update_title = None
    if capability is CapabilityCode.MISSION_MANAGEMENT:
        if "오늘" in compact:
            mission_filter = "오늘"
        elif any(word in compact for word in ("이번주", "주간", "일주일")):
            mission_filter = "이번 주"
        title_match = re.search(r"제목\s*[:=]\s*['\"]?(.+?)['\"]?$", instruction)
        if title_match:
            mission_update_title = title_match.group(1).strip()[:500]
    return (
        entity,
        action,
        ManagerTaskSlots(
            year=int(year_match.group(1)) if year_match else None,
            semester=int(semester_match.group(1)) if semester_match else None,
            week=int(week_match.group(1)) if week_match else None,
            ordinal=ordinal,
            filter=mission_filter,
            mission_id=int(mission_id_match.group(1)) if mission_id_match else None,
            query=mission_update_title,
        ),
    )


class VerifiedAnnouncementTarget(BaseModel):
    """Runtime이 직전 MCP 공지 목록에서만 만들 수 있는 상세 조회 대상."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=500)
    url: str = Field(min_length=1, max_length=2_000)
    course_id: str | None = Field(default=None, max_length=160)
    year: int | None = Field(default=None, ge=2000, le=2100)
    semester: int | None = Field(default=None, ge=1, le=4)


class VerifiedAttachmentTarget(BaseModel):
    """Runtime이 직전 MCP 첨부 목록에서만 만들 수 있는 다운로드 대상."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=160)
    name: str = Field(min_length=1, max_length=500)
    url: str = Field(min_length=1, max_length=2_000)
    parent_id: str = Field(min_length=1, max_length=160)


class VerifiedAssignmentTarget(BaseModel):
    """Runtime이 직전 MCP 과제 목록에서만 만들 수 있는 상세 조회 대상."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=500)
    course_id: str = Field(min_length=1, max_length=160)
    course_name: str | None = Field(default=None, max_length=300)
    year: int | None = Field(default=None, ge=2000, le=2100)
    semester: int | None = Field(default=None, ge=1, le=4)


class VerifiedLectureTarget(BaseModel):
    """Runtime이 직전 MCP 강의 목록에서만 만들 수 있는 영상 재생 대상."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=160)
    course_id: str = Field(min_length=1, max_length=160)
    course_name: str | None = Field(default=None, max_length=300)
    title: str = Field(min_length=1, max_length=500)
    url: str = Field(min_length=1, max_length=2_000)
    week: int | None = Field(default=None, ge=1, le=99)
    year: int | None = Field(default=None, ge=2000, le=2100)
    semester: int | None = Field(default=None, ge=1, le=4)


CAPABILITY_TARGET_MAP: dict[CapabilityCode, ExecutionTargetName] = {
    CapabilityCode.ECLASS_QUERY: ExecutionTargetName.ECLASS,
    CapabilityCode.VIDEO_PLAY: ExecutionTargetName.ECLASS,
    CapabilityCode.DOCUMENT_ANALYSIS: ExecutionTargetName.DOCUMENT,
    CapabilityCode.MISSION_MANAGEMENT: ExecutionTargetName.MISSION_SERVICE,
}


class ManagerTask(BaseModel):
    """복합 요청에서 순서대로 실행할 전문 Agent 작업 한 단계."""

    model_config = ConfigDict(extra="forbid")

    # JSON/public API 호환을 위해 필드명은 agent를 유지하지만 값은 Agent와 Service를 모두 포함하는
    # ExecutionTargetName이다.
    agent: ExecutionTargetName
    capability: CapabilityCode
    entity: ManagerEntityKind
    action: ManagerAction
    slots: ManagerTaskSlots
    instruction: str = Field(min_length=1, max_length=1_000)
    # Manager 모델의 값을 신뢰하지 않는다. Runtime이 먼저 None으로 지운 뒤 검증된 이전 목록에서
    # 정확히 하나를 선택할 수 있을 때만 채운다.
    verified_announcement_target: VerifiedAnnouncementTarget | None = None
    verified_attachment_target: VerifiedAttachmentTarget | None = None
    # ``파일들``, ``첨부 전부``처럼 같은 과제의 여러 파일을 요청했을 때 Runtime이
    # 구조화된 첨부 목록에서만 채우는 묶음 대상이다. Manager 모델이 만든 값은 요청 시작 시
    # 모두 폐기하며, 한 번에 과도한 다운로드가 발생하지 않도록 최대 5개로 제한한다.
    verified_attachment_targets: list[VerifiedAttachmentTarget] = Field(
        default_factory=list,
        max_length=5,
    )
    verified_assignment_target: VerifiedAssignmentTarget | None = None
    verified_lecture_target: VerifiedLectureTarget | None = None
    # 아래 두 필드도 Manager가 정하는 값이 아니다. Runtime이 현재 검증된 첨부 스냅샷에서
    # 선택한 첨부 ID를 Document 단계에 결박하고, 명시적인 "그 파일" 후속 요청일 때만
    # 이전 다운로드 참조 재사용을 허용한다. Runtime은 매 요청마다 모델 출력값을 먼저 지운다.
    verified_attachment_id: str | None = Field(default=None, min_length=1, max_length=160)
    verified_attachment_ids: list[str] = Field(default_factory=list, max_length=5)
    reuse_latest_verified_download: bool = False
    # Manager 모델이 채우는 값이 아니다. Runtime이 검증된 E-Class 다운로드 결과 중 현재
    # Document 단계가 사용할 참조만 넣는다. 개수와 문자열 길이를 제한해 Agent 입력도 경계한다.
    verified_input_refs: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("verified_input_refs")
    @classmethod
    def validate_verified_input_refs(cls, values: list[str]) -> list[str]:
        for value in values:
            if len(value) > 220 or parse_verified_download_ref(value) is None:
                raise ValueError("verified_input_refs에는 검증 다운로드 참조만 넣을 수 있습니다.")
        return values

    @field_validator("verified_attachment_ids")
    @classmethod
    def validate_verified_attachment_ids(cls, values: list[str]) -> list[str]:
        """복수 첨부 결박은 비어 있거나, 서로 다른 유효 ID 목록이어야 한다."""

        if any(not value or len(value) > 160 for value in values):
            raise ValueError("verified_attachment_ids에는 유효한 첨부 ID만 넣을 수 있습니다.")
        if len(values) != len(set(values)):
            raise ValueError("verified_attachment_ids에는 중복 ID를 넣을 수 없습니다.")
        return values

    @model_validator(mode="before")
    @classmethod
    def populate_legacy_typed_contract(cls, value: Any) -> Any:
        """typed 필드가 없던 호출도 검증 후에는 항상 완전한 계약을 갖게 한다."""

        if not isinstance(value, dict):
            return value
        if all(key in value for key in ("entity", "action", "slots")):
            return value
        try:
            capability = CapabilityCode(value.get("capability"))
        except (TypeError, ValueError):
            return value
        entity, action, slots = _legacy_task_contract(
            capability,
            str(value.get("instruction") or ""),
        )
        normalized = dict(value)
        normalized.setdefault("entity", entity)
        normalized.setdefault("action", action)
        normalized.setdefault("slots", slots)
        return normalized

    @model_validator(mode="after")
    def validate_agent_capability(self) -> "ManagerTask":
        if CAPABILITY_TARGET_MAP[self.capability] is not self.agent:
            raise ValueError("capability와 실행 대상의 책임 범위가 일치하지 않습니다.")
        allowed_contracts = {
            CapabilityCode.ECLASS_QUERY: (
                {
                    ManagerEntityKind.COURSE,
                    ManagerEntityKind.ANNOUNCEMENT,
                    ManagerEntityKind.ASSIGNMENT,
                    ManagerEntityKind.ATTACHMENT,
                    ManagerEntityKind.LECTURE,
                    ManagerEntityKind.GRADE,
                },
                {ManagerAction.LIST, ManagerAction.DETAIL, ManagerAction.DOWNLOAD},
            ),
            CapabilityCode.VIDEO_PLAY: (
                {ManagerEntityKind.LECTURE},
                {ManagerAction.PLAY, ManagerAction.STOP, ManagerAction.PREVIEW},
            ),
            CapabilityCode.DOCUMENT_ANALYSIS: (
                {ManagerEntityKind.DOCUMENT},
                {ManagerAction.ANALYZE},
            ),
            CapabilityCode.MISSION_MANAGEMENT: (
                {ManagerEntityKind.MISSION},
                {ManagerAction.LIST_MISSIONS, ManagerAction.COMPLETE, ManagerAction.UPDATE},
            ),
        }
        allowed_entities, allowed_actions = allowed_contracts[self.capability]
        if self.entity not in allowed_entities or self.action not in allowed_actions:
            raise ValueError("capability와 entity/action 계약이 일치하지 않습니다.")
        if self.verified_attachment_target is not None and self.verified_attachment_targets:
            raise ValueError("단일 첨부 대상과 복수 첨부 대상을 동시에 연결할 수 없습니다.")
        if self.verified_attachment_id is not None and self.verified_attachment_ids:
            raise ValueError("단일 첨부 ID와 복수 첨부 ID를 동시에 연결할 수 없습니다.")
        if self.verified_attachment_targets:
            parent_ids = {target.parent_id for target in self.verified_attachment_targets}
            if len(parent_ids) != 1:
                raise ValueError("복수 첨부 대상은 같은 과제에 속해야 합니다.")
            target_ids = [target.id for target in self.verified_attachment_targets]
            if len(target_ids) != len(set(target_ids)):
                raise ValueError("복수 첨부 대상에는 중복 파일을 넣을 수 없습니다.")
        if self.verified_attachment_ids and self.verified_attachment_targets:
            if self.verified_attachment_ids != [
                target.id for target in self.verified_attachment_targets
            ]:
                raise ValueError("복수 첨부 대상과 첨부 ID의 순서가 일치해야 합니다.")

        verified_targets = (
            self.verified_announcement_target,
            self.verified_attachment_target or (
                self.verified_attachment_targets if self.verified_attachment_targets else None
            ),
            self.verified_assignment_target,
            self.verified_lecture_target,
        )
        if sum(target is not None for target in verified_targets) > 1:
            raise ValueError("한 ManagerTask에는 검증된 실행 대상을 하나만 연결할 수 있습니다.")
        return self


class ManagerPlan(BaseModel):
    """Manager 모델이 반환하는 대화 응답 또는 제한된 순차 실행 계획.

    실행 소유권은 항상 Manager Runtime에 있으므로 Agent handoff 필드는 두지 않는다.
    """

    model_config = ConfigDict(extra="forbid")

    mode: InteractionMode
    reply: str = Field(min_length=1, max_length=700)
    conversation_summary: str = Field(min_length=1, max_length=500)
    tasks: list[ManagerTask] = Field(default_factory=list, max_length=4)
    reason: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def validate_plan(self) -> "ManagerPlan":
        if self.mode is InteractionMode.CHAT and self.tasks:
            raise ValueError("CHAT 계획에는 전문 작업을 넣을 수 없습니다.")
        if self.mode is InteractionMode.TASK and not self.tasks:
            raise ValueError("TASK 계획에는 하나 이상의 전문 Agent 작업이 필요합니다.")
        return self


class SpecialistStatus(str, Enum):
    """전문 Agent 한 단계가 Runtime에 반환하는 상태."""

    COMPLETED = "COMPLETED"
    CAPABILITY_NOT_READY = "CAPABILITY_NOT_READY"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    FAILED = "FAILED"


class SpecialistResult(BaseModel):
    """전문 Agent가 HTML이나 비밀값 대신 반환하는 최소 구조화 결과."""

    model_config = ConfigDict(extra="forbid")

    status: SpecialistStatus
    summary: str = Field(min_length=1, max_length=2_000)
    evidence_refs: list[str] = Field(default_factory=list, max_length=100)
    suggested_actions: list[str] = Field(default_factory=list, max_length=20)
    error_code: ErrorCode | None = None
    # Agent가 작성하는 필드가 아니다. Runtime handler가 검증된 Tool 결과에서만 덮어쓰며,
    # 공지 본문처럼 의역하면 안 되는 내용을 LLM 재작성 없이 TUI로 전달한다.
    verified_display_text: str | None = Field(default=None, max_length=50_000)
    # 후속 요청에서 "1번", "그 공지"를 복원하기 위한 검증된 엔터티 문맥이다. E-Class handler가
    # MCP 원본에서 생성해 덮어쓰며 모델의 자연어 기억에 의존하지 않는다.
    verified_followup_context: str | None = Field(default=None, max_length=12_000)


class ManagerResult(BaseModel):
    """TUI와 호출자가 받는 Manager Runtime 최종 결과."""

    model_config = ConfigDict(extra="forbid")

    status: ManagerStatus
    message: str = Field(min_length=1, max_length=50_000)
    should_notify: bool
    priority: ManagerPriority = ManagerPriority.NORMAL
    suggested_actions: list[str] = Field(default_factory=list, max_length=20)
    delegated_agents: list[ExecutionTargetName] = Field(default_factory=list, max_length=4)
    evidence_refs: list[str] = Field(default_factory=list, max_length=100)
    error_code: ErrorCode | None = None

    @model_validator(mode="after")
    def validate_notification_contract(self) -> "ManagerResult":
        if self.status is ManagerStatus.NO_ACTION and self.should_notify:
            raise ValueError("NO_ACTION 결과는 사용자에게 알림을 표시할 수 없습니다.")
        if self.status is ManagerStatus.NO_ACTION and self.priority is not ManagerPriority.NONE:
            raise ValueError("NO_ACTION 결과의 우선순위는 NONE이어야 합니다.")
        return self
