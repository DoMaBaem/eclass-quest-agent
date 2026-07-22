"""사용자 요청과 시스템 이벤트를 Manager 중심 실행으로 처리하는 Runtime."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from collections.abc import Awaitable, Callable
from uuid import UUID, uuid4

from agents import trace

from app.agent.errors import OpenAiApiKeyRequiredError
from app.agent.eclass_mcp_handler import EclassMcpSpecialistHandler
from app.agent.document_handler import DocumentSpecialistHandler
from app.agent.manager_agent import TextDeltaHandler, create_plan
from app.agent.mission_handler import MissionServiceHandler
from app.config import Settings
from app.guardrails import GuardrailViolation, guard_user_input, sanitize_output
from app.runtime.event_queue import RuntimeEventQueue
from app.runtime.events import RuntimeProgressEvent
from app.schemas.manager import (
    ManagerAction,
    ManagerEntityKind,
    ManagerPlan,
    ManagerPriority,
    ManagerResult,
    ManagerStatus,
    ManagerTask,
    ExecutionTargetName,
    SpecialistResult,
    SpecialistStatus,
    VerifiedAnnouncementTarget,
    VerifiedAssignmentTarget,
    VerifiedAttachmentTarget,
    VerifiedLectureTarget,
    parse_verified_download_ref,
)
from app.schemas.runtime import (
    AssistantContext,
    ConversationTurn,
    RuntimeEvent,
    RuntimeEventType,
    VerifiedEntityKind,
    VerifiedEntityReference,
    VerifiedEntitySnapshot,
)
from app.schemas.workflow import CapabilityCode, ErrorCode, InteractionMode
from app.storage.workflow_audit import WorkflowAuditService

ProgressHandler = Callable[[RuntimeProgressEvent, str | None], Awaitable[None] | None]
ExecutionHandler = Callable[[ManagerTask], Awaitable[SpecialistResult]]


class ProactiveAssistantRuntime:
    """Manager가 계획하고 Runtime이 전문 Agent 순서·한도·결과 통합을 강제한다."""

    MAX_RECENT_TURNS = 12

    def __init__(
        self,
        settings: Settings,
        *,
        specialist_handlers: dict[ExecutionTargetName, ExecutionHandler] | None = None,
        max_agent_steps: int = 4,
    ) -> None:
        if max_agent_steps < 1:
            raise ValueError("max_agent_steps는 1 이상이어야 합니다.")
        self.settings = settings
        self.context = AssistantContext()
        self.event_queue = RuntimeEventQueue()
        self.max_agent_steps = max_agent_steps
        self._closed = False
        # 재생 Tool이 실제로 반환한 ID만 별도로 보관한다. 영상 중지 단축키는 이 집합에
        # 있는 값만 사용할 수 있으므로 사용자 문자열이나 Manager의 ID 생성을 신뢰하지 않는다.
        self._active_playback_refs: set[str] = set()
        self._audit = WorkflowAuditService(settings)
        self._specialist_handlers: dict[ExecutionTargetName, ExecutionHandler] = {
            ExecutionTargetName.ECLASS: EclassMcpSpecialistHandler(settings),
            ExecutionTargetName.DOCUMENT: DocumentSpecialistHandler(settings),
            ExecutionTargetName.MISSION_SERVICE: MissionServiceHandler(settings),
        }
        if specialist_handlers is not None:
            self._specialist_handlers.update(specialist_handlers)

    async def handle_user_request(
        self,
        message: str,
        *,
        on_progress: ProgressHandler | None = None,
        on_manager_delta: TextDeltaHandler | None = None,
    ) -> ManagerResult:
        """사용자 원문을 한 이벤트로 한정해 Manager 계획과 전문 Agent 실행을 처리한다."""

        if self._closed:
            raise RuntimeError("종료된 Runtime은 요청을 처리할 수 없습니다.")
        request_id = str(uuid4())
        self.context.last_request_id = request_id
        await self._audit_safely("start", request_id, trigger_type="USER_REQUEST")
        try:
            guarded = guard_user_input(message, self.settings)
        except GuardrailViolation as exc:
            await self._audit_safely(
                "step", request_id, component="InputGuardrail", state="BLOCKED", code=exc.code
            )
            await self._audit_safely(
                "finish", request_id, status="FAILED", error_code=exc.code
            )
            return ManagerResult(
                status=ManagerStatus.FAILED,
                message=str(exc),
                should_notify=True,
                priority=ManagerPriority.NORMAL,
                error_code=(
                    ErrorCode.INVALID_REQUEST
                    if exc.code in {"INVALID_REQUEST", "INPUT_TOO_LONG"}
                    else ErrorCode.POLICY_BLOCKED
                ),
            )
        normalized = guarded.text
        await self._audit_safely(
            "step", request_id, component="InputGuardrail", state="PASSED"
        )
        event = RuntimeEvent(
            event_type=RuntimeEventType.USER_REQUEST,
            payload={
                "user_message": normalized,
                "explicit_playback_request": guarded.explicit_playback_request,
            },
        )
        with trace(
            "E-Class Quest Request",
            group_id=self.context.conversation_id,
            metadata={"request_id": request_id, "trigger_type": "USER_REQUEST"},
            disabled=self._trace_disabled(),
        ):
            result = await self._handle_event(
                event,
                on_progress=on_progress,
                on_manager_delta=on_manager_delta,
            )
        safe_message = sanitize_output(
            result.message,
            self.settings,
            download_root=self.settings.download_root,
        )
        result = result.model_copy(update={"message": safe_message or "안전하게 표시할 결과가 없습니다."})
        await self._audit_safely(
            "step", request_id, component="OutputGuardrail", state="PASSED"
        )
        await self._audit_safely(
            "finish",
            request_id,
            status=result.status.value,
            error_code=result.error_code.value if result.error_code else None,
        )
        self._remember_exchange(normalized, result.message)
        return result

    async def handle_system_event(
        self,
        event: RuntimeEvent,
        *,
        on_progress: ProgressHandler | None = None,
    ) -> ManagerResult:
        """구조화 시스템 이벤트만 받고 사용자 원문 대화가 섞이면 계약에서 거부한다."""

        if event.event_type is RuntimeEventType.USER_REQUEST:
            raise ValueError("USER_REQUEST는 handle_user_request()로 처리해야 합니다.")
        if self._closed:
            raise RuntimeError("종료된 Runtime은 이벤트를 처리할 수 없습니다.")
        self.context.last_event_id = event.event_id
        request_id = str(uuid4())
        self.context.last_request_id = request_id
        await self._audit_safely(
            "start", request_id, trigger_type=event.event_type.value, event_id=event.event_id
        )
        with trace(
            "E-Class Quest System Event",
            group_id=self.context.conversation_id,
            metadata={"request_id": request_id, "trigger_type": event.event_type.value},
            disabled=self._trace_disabled(),
        ):
            result = await self._handle_event(event, on_progress=on_progress, on_manager_delta=None)
        safe_message = sanitize_output(
            result.message,
            self.settings,
            download_root=self.settings.download_root,
        )
        result = result.model_copy(update={"message": safe_message or "안전하게 표시할 결과가 없습니다."})
        await self._audit_safely(
            "finish",
            request_id,
            status=result.status.value,
            error_code=result.error_code.value if result.error_code else None,
        )
        if result.should_notify and result.message:
            self._remember_turn("assistant", result.message)
        return result

    async def stop_verified_playback(
        self,
        playback_id: str,
        *,
        on_progress: ProgressHandler | None = None,
    ) -> ManagerResult:
        """현재 Runtime이 발급받은 재생 ID 하나를 LLM 없이 E-Class MCP에 중지 요청한다.

        F2 단축키는 자연어 작업 계획이 아니다. 이미 성공한 재생 Tool 결과를 되돌리는 제어이므로
        Manager와 E-Class Agent에게 UUID를 다시 복사시키지 않고, Runtime의 검증 집합과 대조한 뒤
        같은 장수명 MCP 프로세스의 ``stop_lecture``를 직접 호출한다.
        """

        if self._closed:
            raise RuntimeError("종료된 Runtime은 요청을 처리할 수 없습니다.")
        try:
            normalized_id = str(UUID(playback_id))
        except (TypeError, ValueError, AttributeError):
            return self._failure(
                "검증된 영상 재생 ID가 올바르지 않습니다.",
                ErrorCode.INVALID_REQUEST,
            )

        playback_ref = f"playback:{normalized_id}"
        if playback_ref not in self._active_playback_refs:
            return self._failure(
                "현재 Runtime이 시작한 영상 재생을 찾을 수 없습니다.",
                ErrorCode.INVALID_REQUEST,
            )

        request_id = str(uuid4())
        self.context.last_request_id = request_id
        await self._audit_safely(
            "start",
            request_id,
            trigger_type="USER_PLAYBACK_STOP_SHORTCUT",
        )
        await self._emit(on_progress, RuntimeProgressEvent.RUNTIME_STARTED, None)
        await self._emit(
            on_progress,
            RuntimeProgressEvent.AGENT_DELEGATED,
            ExecutionTargetName.ECLASS.value,
        )

        handler = self._specialist_handlers.get(ExecutionTargetName.ECLASS)
        stop = getattr(handler, "stop_verified_playback", None)
        if stop is None:
            result = ManagerResult(
                status=ManagerStatus.CAPABILITY_NOT_READY,
                message="현재 E-Class 실행 경로는 검증된 영상 중지를 지원하지 않습니다.",
                should_notify=True,
                priority=ManagerPriority.NORMAL,
                delegated_agents=[ExecutionTargetName.ECLASS],
            )
            await self._emit(
                on_progress,
                RuntimeProgressEvent.CAPABILITY_NOT_READY,
                ExecutionTargetName.ECLASS.value,
            )
        else:
            try:
                specialist = stop(normalized_id)
                if inspect.isawaitable(specialist):
                    specialist = await specialist
                if not isinstance(specialist, SpecialistResult):
                    raise TypeError("영상 중지 handler가 SpecialistResult를 반환하지 않았습니다.")
            except Exception:
                specialist = SpecialistResult(
                    status=SpecialistStatus.FAILED,
                    summary="E-Class 영상 중지 요청을 완료하지 못했습니다.",
                    error_code=ErrorCode.TEMPORARY_FAILURE,
                )

            if specialist.status is SpecialistStatus.COMPLETED:
                self._active_playback_refs.discard(playback_ref)
                result = ManagerResult(
                    status=ManagerStatus.COMPLETED,
                    message=specialist.verified_display_text or specialist.summary,
                    should_notify=True,
                    priority=ManagerPriority.NORMAL,
                    delegated_agents=[ExecutionTargetName.ECLASS],
                    evidence_refs=list(dict.fromkeys(specialist.evidence_refs)),
                )
            elif specialist.status is SpecialistStatus.AUTH_REQUIRED:
                await self._emit(
                    on_progress,
                    RuntimeProgressEvent.AUTH_REQUIRED,
                    ExecutionTargetName.ECLASS.value,
                )
                result = ManagerResult(
                    status=ManagerStatus.AUTH_REQUIRED,
                    message=specialist.summary,
                    should_notify=True,
                    priority=ManagerPriority.HIGH,
                    delegated_agents=[ExecutionTargetName.ECLASS],
                    error_code=ErrorCode.AUTH_REQUIRED,
                )
            else:
                await self._emit(
                    on_progress,
                    RuntimeProgressEvent.ERROR,
                    ExecutionTargetName.ECLASS.value,
                )
                result = self._failure(
                    specialist.summary,
                    specialist.error_code or ErrorCode.TEMPORARY_FAILURE,
                    delegated=[ExecutionTargetName.ECLASS],
                )

        safe_message = sanitize_output(
            result.message,
            self.settings,
            download_root=self.settings.download_root,
        )
        result = result.model_copy(
            update={"message": safe_message or "안전하게 표시할 결과가 없습니다."}
        )
        await self._audit_safely(
            "finish",
            request_id,
            status=result.status.value,
            error_code=result.error_code.value if result.error_code else None,
        )
        self._remember_exchange("[F2] 재생 중인 강의 영상 중지", result.message)
        return result

    async def _handle_event(
        self,
        event: RuntimeEvent,
        *,
        on_progress: ProgressHandler | None,
        on_manager_delta: TextDeltaHandler | None,
    ) -> ManagerResult:
        await self._emit(on_progress, RuntimeProgressEvent.RUNTIME_STARTED, None)
        await self._emit(on_progress, RuntimeProgressEvent.MANAGER_STARTED, None)
        try:
            plan = await self._create_plan_with_retry(
                event,
                on_manager_delta=on_manager_delta,
            )
        except OpenAiApiKeyRequiredError:
            return self._failure(
                "OpenAI API 키가 필요합니다. ./run.sh --setup에서 설정하세요.",
                ErrorCode.OPENAI_API_KEY_REQUIRED,
            )
        except Exception:
            await self._emit(on_progress, RuntimeProgressEvent.ERROR, None)
            return self._failure("LMS Manager 응답에 실패했습니다.", ErrorCode.MANAGER_FAILED)

        # Manager 출력에 실행용 참조가 포함되어도 신뢰하지 않는다. 이후 각 단계 직전에 Runtime이
        # 실제 전문 Tool 결과에서 얻은 참조만 다시 채운다.
        plan = plan.model_copy(
            update={
                "tasks": [
                    task.model_copy(
                        update={
                            "verified_input_refs": [],
                            "verified_attachment_id": None,
                            "verified_attachment_ids": [],
                            "verified_attachment_targets": [],
                            "reuse_latest_verified_download": False,
                        }
                    )
                    for task in plan.tasks
                ]
            }
        )
        plan = self._attach_verified_announcement_target(plan, event)
        plan = self._attach_verified_assignment_target(plan, event)
        plan = self._attach_verified_lecture_target(plan, event)
        plan = self._attach_verified_attachment_target(plan, event)
        if self.context.last_request_id:
            await self._audit_safely(
                "step", self.context.last_request_id, component="LMS Manager Agent", state="PLANNED"
            )
        if any(task.capability.value == "VIDEO_PLAY" for task in plan.tasks) and not bool(
            event.payload.get("explicit_playback_request", False)
        ):
            return self._failure(
                "영상 제어는 사용자가 재생 또는 중지를 명시적으로 요청한 경우에만 실행할 수 있습니다.",
                ErrorCode.POLICY_BLOCKED,
            )
        self._update_context(plan, event)
        if plan.mode is InteractionMode.CHAT:
            if event.event_type is not RuntimeEventType.USER_REQUEST:
                if self._system_event_should_notify(event):
                    return ManagerResult(
                        status=ManagerStatus.COMPLETED,
                        message=plan.reply,
                        should_notify=True,
                        priority=self._system_event_priority(event),
                    )
                await self._emit(on_progress, RuntimeProgressEvent.NO_ACTION, None)
                return ManagerResult(
                    status=ManagerStatus.NO_ACTION,
                    message=plan.reply,
                    should_notify=False,
                    priority=ManagerPriority.NONE,
                )
            return ManagerResult(
                status=ManagerStatus.COMPLETED,
                message=plan.reply,
                should_notify=True,
                priority=ManagerPriority.NORMAL,
            )

        return await self._execute_plan(plan, on_progress=on_progress)

    def _verified_candidate_payload(
        self,
        kind: VerifiedEntityKind,
        legacy_kind: str,
    ) -> dict[str, object] | None:
        """종류별 typed snapshot을 우선하고 이전 JSON 필드는 호환용으로만 읽는다."""

        snapshot = next(
            (
                item
                for item in reversed(self.context.verified_entity_snapshots)
                if item.kind is kind
            ),
            None,
        )
        if snapshot is not None:
            return {
                "kind": legacy_kind,
                "selected_term": {
                    "year": snapshot.year,
                    "semester": snapshot.semester,
                },
                "items": [
                    item.model_dump(mode="json", exclude_none=True, exclude={"kind"})
                    for item in snapshot.items
                ],
            }

        try:
            legacy = json.loads(self.context.last_verified_result_summary)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(legacy, dict) or legacy.get("kind") != legacy_kind:
            return None
        return legacy

    def _remember_verified_snapshots(self, results: list[SpecialistResult]) -> None:
        """MCP 후속 JSON을 검증된 종류별 참조로 바꾸고 기존 종류만 교체한다."""

        kind_map = {
            "verified_course_candidates": VerifiedEntityKind.COURSE,
            "verified_announcement_candidates": VerifiedEntityKind.ANNOUNCEMENT,
            "verified_assignment_candidates": VerifiedEntityKind.ASSIGNMENT,
            "verified_lecture_candidates": VerifiedEntityKind.LECTURE,
            "verified_attachment_candidates": VerifiedEntityKind.ATTACHMENT,
        }
        allowed_fields = {
            "id",
            "number",
            "title",
            "name",
            "url",
            "course_id",
            "course_name",
            "parent_id",
            "week",
            "professor",
            "mime_type",
            "posted_at",
        }
        for result in results:
            encoded = result.verified_followup_context
            if not encoded:
                continue
            try:
                payload = json.loads(encoded)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(payload, dict):
                continue
            kind = kind_map.get(str(payload.get("kind") or ""))
            if kind is None:
                continue
            references: list[VerifiedEntityReference] = []
            for item in payload.get("items", []):
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                normalized = {key: item.get(key) for key in allowed_fields if key in item}
                normalized["kind"] = kind
                try:
                    references.append(VerifiedEntityReference.model_validate(normalized))
                except (TypeError, ValueError):
                    continue
            selected_term = payload.get("selected_term")
            term = selected_term if isinstance(selected_term, dict) else {}
            try:
                snapshot = VerifiedEntitySnapshot(
                    kind=kind,
                    items=references,
                    year=term.get("year"),
                    semester=term.get("semester"),
                )
            except (TypeError, ValueError):
                continue
            retained = [
                existing
                for existing in self.context.verified_entity_snapshots
                if existing.kind is not kind
            ]
            self.context.verified_entity_snapshots = (retained + [snapshot])[-8:]

    @staticmethod
    def _attachment_targets_from_results(
        results: list[SpecialistResult],
        download_refs: list[str],
    ) -> list[VerifiedAttachmentTarget]:
        """현재 실행에서 생성된 첨부 Snapshot을 다운로드 참조의 파일명에 다시 연결한다."""

        ordered_ids = [
            parsed[1]
            for ref in download_refs
            if (parsed := parse_verified_download_ref(ref)) is not None
        ]
        if not ordered_ids:
            return []
        for result in reversed(results):
            encoded = result.verified_followup_context
            if not encoded:
                continue
            try:
                payload = json.loads(encoded)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(payload, dict) or payload.get("kind") != "verified_attachment_candidates":
                continue
            items_by_id = {
                str(item.get("id")): item
                for item in payload.get("items", [])
                if isinstance(item, dict) and item.get("id")
            }
            if any(attachment_id not in items_by_id for attachment_id in ordered_ids):
                continue
            try:
                targets = [
                    VerifiedAttachmentTarget(
                        id=attachment_id,
                        name=str(items_by_id[attachment_id]["name"]),
                        url=str(items_by_id[attachment_id]["url"]),
                        parent_id=str(items_by_id[attachment_id]["parent_id"]),
                    )
                    for attachment_id in ordered_ids
                ]
            except (KeyError, TypeError, ValueError):
                continue
            if len({target.parent_id for target in targets}) == 1:
                return targets
        return []

    @staticmethod
    def _task_matches_intent(
        task: ManagerTask,
        *,
        entity: ManagerEntityKind,
        actions: set[ManagerAction],
        capability: CapabilityCode,
    ) -> bool:
        """검증 대상을 붙여도 되는 typed task인지 자연어와 무관하게 판정한다."""

        return (
            task.agent is ExecutionTargetName.ECLASS
            and task.capability is capability
            and task.entity is entity
            and task.action in actions
        )

    @staticmethod
    def _normalized_lookup_text(value: object) -> str:
        """공백·구두점을 제외해 검증된 이름의 보수적인 포함 비교에 사용한다."""

        return re.sub(r"[\W_]+", "", str(value or "").casefold())

    @classmethod
    def _text_matches(cls, query: object, *values: object) -> bool:
        """typed query가 검증 후보의 ID·제목에 포함되는 경우만 참으로 본다."""

        normalized_query = cls._normalized_lookup_text(query)
        if not normalized_query:
            return False
        return any(
            normalized_query in normalized_value or normalized_value in normalized_query
            for value in values
            if (normalized_value := cls._normalized_lookup_text(value))
        )

    @classmethod
    def _candidate_matches_task_scope(
        cls,
        task: ManagerTask,
        item: dict[str, object],
        selected_term: object,
    ) -> bool:
        """다른 학기·강좌의 동일 번호 후보를 현재 task에 잘못 연결하지 않는다."""

        term = selected_term if isinstance(selected_term, dict) else {}
        if task.slots.year is not None and term.get("year") != task.slots.year:
            return False
        if task.slots.semester is not None and term.get("semester") != task.slots.semester:
            return False
        if task.slots.course_query is not None:
            if not cls._text_matches(
                task.slots.course_query,
                item.get("course_name"),
                item.get("course_id"),
            ):
                return False
        return True

    def _attach_verified_announcement_target(
        self,
        plan: ManagerPlan,
        event: RuntimeEvent,
    ) -> ManagerPlan:
        """직전 MCP 목록과 현재 표현이 정확히 일치할 때만 상세 대상 URL을 task에 붙인다."""

        # Manager가 verified 필드를 스스로 만들었더라도 먼저 모두 폐기한다.
        tasks = [task.model_copy(update={"verified_announcement_target": None}) for task in plan.tasks]
        if event.event_type is not RuntimeEventType.USER_REQUEST:
            return plan.model_copy(update={"tasks": tasks})
        candidates = self._verified_candidate_payload(
            VerifiedEntityKind.ANNOUNCEMENT,
            "verified_announcement_candidates",
        )
        if candidates is None:
            return plan.model_copy(update={"tasks": tasks})
        items = candidates.get("items")
        if not isinstance(items, list) or not items:
            return plan.model_copy(update={"tasks": tasks})

        user_message = str(event.payload.get("user_message", ""))
        selected_term = candidates.get("selected_term") or {}
        enriched: list[ManagerTask] = []
        for task in tasks:
            if not self._task_matches_intent(
                task,
                entity=ManagerEntityKind.ANNOUNCEMENT,
                actions={ManagerAction.DETAIL},
                capability=CapabilityCode.ECLASS_QUERY,
            ):
                enriched.append(task)
                continue
            combined = f"{user_message}\n{task.instruction}"
            compact = "".join(combined.casefold().split())
            valid_items = [
                item
                for item in items
                if isinstance(item, dict)
                and self._candidate_matches_task_scope(task, item, selected_term)
            ]
            matched_items: list[dict[str, object]] = []
            number = task.slots.ordinal
            if number is None:
                number_match = re.search(r"(?<!\d)(\d+)\s*번", combined)
                number = int(number_match.group(1)) if number_match else None
            if number is not None:
                matched_items = [item for item in valid_items if item.get("number") == number]
            if task.slots.query:
                matched_items = [
                    item
                    for item in (matched_items or valid_items)
                    if self._text_matches(task.slots.query, item.get("title"), item.get("id"))
                ]
            elif not matched_items:
                for item in valid_items:
                    item_id = str(item.get("id") or "")
                    title = str(item.get("title") or "")
                    if (item_id and item_id in combined) or (title and title in combined):
                        matched_items.append(item)
            if not matched_items and len(valid_items) == 1 and any(
                phrase in compact for phrase in ("그공지", "해당공지", "이공지")
            ):
                matched_items = [valid_items[0]]
            # 여러 후보 또는 무일치 상태에서는 임의로 선택하지 않는다.
            if len(matched_items) != 1:
                enriched.append(task)
                continue
            item = matched_items[0]
            try:
                target = VerifiedAnnouncementTarget(
                    id=str(item["id"]),
                    title=str(item["title"]),
                    url=str(item["url"]),
                    course_id=str(item["course_id"]) if item.get("course_id") is not None else None,
                    year=(selected_term.get("year") if isinstance(selected_term, dict) else None),
                    semester=(
                        selected_term.get("semester")
                        if isinstance(selected_term, dict)
                        else None
                    ),
                )
            except (KeyError, TypeError, ValueError):
                enriched.append(task)
                continue
            enriched.append(task.model_copy(update={"verified_announcement_target": target}))
        return plan.model_copy(update={"tasks": enriched})

    def _attach_verified_assignment_target(
        self,
        plan: ManagerPlan,
        event: RuntimeEvent,
    ) -> ManagerPlan:
        """직전 과제 목록의 번호·서수·제목을 실제 assignment_id에 연결한다."""

        tasks = [task.model_copy(update={"verified_assignment_target": None}) for task in plan.tasks]
        if event.event_type is not RuntimeEventType.USER_REQUEST:
            return plan.model_copy(update={"tasks": tasks})
        candidates = self._verified_candidate_payload(
            VerifiedEntityKind.ASSIGNMENT,
            "verified_assignment_candidates",
        )
        if candidates is None:
            return plan.model_copy(update={"tasks": tasks})
        items = [item for item in candidates.get("items", []) if isinstance(item, dict)]
        if not items:
            return plan.model_copy(update={"tasks": tasks})

        user_message = str(event.payload.get("user_message", ""))
        compact = "".join(user_message.casefold().split())
        selected_term = candidates.get("selected_term") or {}
        enriched: list[ManagerTask] = []
        for task in tasks:
            is_assignment_detail = self._task_matches_intent(
                task,
                entity=ManagerEntityKind.ASSIGNMENT,
                actions={ManagerAction.DETAIL},
                capability=CapabilityCode.ECLASS_QUERY,
            )
            # "첫 번째 과제 파일들"은 첨부 Tool에 과제 ID가 필요하다. Manager가 ID를
            # 자연어로 복사하게 하지 않고, 직전 과제 목록에서 선택한 부모 과제를 Runtime이
            # LIST/DETAIL task에 직접 결박한다.
            is_attachment_listing = self._task_matches_intent(
                task,
                entity=ManagerEntityKind.ATTACHMENT,
                actions={ManagerAction.LIST, ManagerAction.DETAIL, ManagerAction.DOWNLOAD},
                capability=CapabilityCode.ECLASS_QUERY,
            )
            if not (is_assignment_detail or is_attachment_listing):
                enriched.append(task)
                continue
            scoped_items = [
                item
                for item in items
                if self._candidate_matches_task_scope(task, item, selected_term)
            ]
            number = task.slots.ordinal
            if number is None:
                number_match = re.search(r"(?<!\d)(\d+)\s*번", user_message)
                number = int(number_match.group(1)) if number_match else None
            if number is None:
                ordinals = {
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
                number = next(
                    (value for word, value in ordinals.items() if word in compact),
                    None,
                )

            matched = (
                [item for item in scoped_items if item.get("number") == number]
                if number is not None
                else []
            )
            if task.slots.query:
                matched = [
                    item
                    for item in (matched or scoped_items)
                    if self._text_matches(task.slots.query, item.get("title"), item.get("id"))
                ]
            elif not matched:
                matched = [
                    item
                    for item in scoped_items
                    if str(item.get("title") or "")
                    and str(item.get("title")) in f"{user_message}\n{task.instruction}"
                ]
            if not matched and len(scoped_items) == 1 and any(
                phrase in compact for phrase in ("그과제", "해당과제", "이과제")
            ):
                matched = [scoped_items[0]]
            if len(matched) == 1:
                item = matched[0]
                try:
                    target = VerifiedAssignmentTarget(
                        id=str(item["id"]),
                        title=str(item["title"]),
                        course_id=str(item["course_id"]),
                        course_name=(
                            str(item["course_name"]) if item.get("course_name") else None
                        ),
                        year=(
                            selected_term.get("year")
                            if isinstance(selected_term, dict)
                            else None
                        ),
                        semester=(
                            selected_term.get("semester")
                            if isinstance(selected_term, dict)
                            else None
                        ),
                    )
                except (KeyError, TypeError, ValueError):
                    target = None
                if target is not None:
                    task = task.model_copy(update={"verified_assignment_target": target})
            enriched.append(task)
        return plan.model_copy(update={"tasks": enriched})

    def _attach_verified_lecture_target(
        self,
        plan: ManagerPlan,
        event: RuntimeEvent,
    ) -> ManagerPlan:
        """직전 강의 목록의 주차·번호·제목을 실제 lecture_id에 연결한다."""

        tasks = [task.model_copy(update={"verified_lecture_target": None}) for task in plan.tasks]
        if event.event_type is not RuntimeEventType.USER_REQUEST:
            return plan.model_copy(update={"tasks": tasks})
        candidates = self._verified_candidate_payload(
            VerifiedEntityKind.LECTURE,
            "verified_lecture_candidates",
        )
        if candidates is None:
            return plan.model_copy(update={"tasks": tasks})
        items = [item for item in candidates.get("items", []) if isinstance(item, dict)]
        if not items:
            return plan.model_copy(update={"tasks": tasks})

        user_message = str(event.payload.get("user_message", ""))
        compact = "".join(user_message.casefold().split())
        selected_term = candidates.get("selected_term") or {}
        enriched: list[ManagerTask] = []
        for task in tasks:
            if not self._task_matches_intent(
                task,
                entity=ManagerEntityKind.LECTURE,
                actions={ManagerAction.PLAY, ManagerAction.PREVIEW},
                capability=CapabilityCode.VIDEO_PLAY,
            ):
                enriched.append(task)
                continue
            scoped_items = [
                item
                for item in items
                if self._candidate_matches_task_scope(task, item, selected_term)
            ]
            matched: list[dict[str, object]] = []
            number = task.slots.ordinal
            if number is None:
                number_match = re.search(r"(?<!\d)(\d+)\s*번", user_message)
                number = int(number_match.group(1)) if number_match else None
            if number is not None:
                matched = [item for item in scoped_items if item.get("number") == number]
            if task.slots.query:
                matched = [
                    item
                    for item in (matched or scoped_items)
                    if self._text_matches(task.slots.query, item.get("title"), item.get("id"))
                ]
            elif not matched:
                matched = [
                    item
                    for item in scoped_items
                    if str(item.get("title") or "")
                    and str(item.get("title")) in f"{user_message}\n{task.instruction}"
                ]
            if not matched:
                week = task.slots.week
                if week is None:
                    week_match = re.search(r"(?<!\d)(\d{1,2})\s*주차", user_message)
                    week = int(week_match.group(1)) if week_match else None
                if week is not None:
                    matched = [item for item in scoped_items if item.get("week") == week]
            if not matched and len(scoped_items) == 1 and any(
                phrase in compact
                for phrase in ("그거", "그영상", "해당영상", "이영상", "재생", "틀어", "봐")
            ):
                matched = [scoped_items[0]]
            # 한 주차에 영상이 여러 개면 임의로 하나를 재생하지 않는다.
            if len(matched) == 1:
                item = matched[0]
                try:
                    target = VerifiedLectureTarget(
                        id=str(item["id"]),
                        course_id=str(item["course_id"]),
                        course_name=(
                            str(item["course_name"]) if item.get("course_name") else None
                        ),
                        title=str(item["title"]),
                        url=str(item["url"]),
                        week=item.get("week"),
                        year=(
                            selected_term.get("year")
                            if isinstance(selected_term, dict)
                            else None
                        ),
                        semester=(
                            selected_term.get("semester")
                            if isinstance(selected_term, dict)
                            else None
                        ),
                    )
                except (KeyError, TypeError, ValueError):
                    target = None
                if target is not None:
                    task = task.model_copy(update={"verified_lecture_target": target})
            enriched.append(task)
        return plan.model_copy(update={"tasks": enriched})

    def _attach_verified_attachment_target(
        self,
        plan: ManagerPlan,
        event: RuntimeEvent,
    ) -> ManagerPlan:
        """직전 첨부 목록에서 사용자가 지목한 파일을 다운로드 단계에 안전하게 연결한다."""

        # 이 값들은 모두 Runtime 전용이다. Manager가 출력한 대상·binding·재사용 권한은
        # 그럴듯한 형식이어도 신뢰하지 않고 현재 요청과 typed snapshot으로 다시 만든다.
        tasks = [
            task.model_copy(
                update={
                    "verified_attachment_target": None,
                    "verified_attachment_targets": [],
                    "verified_attachment_id": None,
                    "verified_attachment_ids": [],
                    "reuse_latest_verified_download": False,
                }
            )
            for task in plan.tasks
        ]
        if event.event_type is not RuntimeEventType.USER_REQUEST:
            return plan.model_copy(update={"tasks": tasks})
        user_message = str(event.payload.get("user_message", ""))
        compact = "".join(user_message.casefold().split())
        plural_intent = any(
            phrase in compact
            for phrase in (
                "파일들",
                "문서들",
                "첨부들",
                "첨부파일들",
                "둘다",
                "모두",
                "전부",
                "각각",
            )
        )

        def allow_anaphoric_reuse(source_tasks: list[ManagerTask]) -> list[ManagerTask]:
            """`그 파일`처럼 명시적인 대용 표현에만 과거 다운로드 재사용을 허용한다."""

            # 복수형은 어느 과제의 어느 batch인지 검증할 Snapshot이 없으면 전역 과거 참조로
            # 복원하지 않는다. 과거 여러 요청의 파일이 섞일 수 있으므로 안전하게 다시 목록을 묻는다.
            if plural_intent:
                return source_tasks
            anaphoric = any(
                phrase in compact
                for phrase in (
                    "그파일",
                    "그문서",
                    "그첨부",
                    "방금파일",
                    "방금문서",
                    "직전파일",
                    "직전문서",
                )
            )
            if not anaphoric:
                return source_tasks
            return [
                task.model_copy(update={"reuse_latest_verified_download": True})
                if task.agent is ExecutionTargetName.DOCUMENT
                and task.capability is CapabilityCode.DOCUMENT_ANALYSIS
                else task
                for task in source_tasks
            ]

        candidates = self._verified_candidate_payload(
            VerifiedEntityKind.ATTACHMENT,
            "verified_attachment_candidates",
        )
        if candidates is None:
            return plan.model_copy(update={"tasks": allow_anaphoric_reuse(tasks)})
        items = [item for item in candidates.get("items", []) if isinstance(item, dict)]
        if not items:
            return plan.model_copy(update={"tasks": allow_anaphoric_reuse(tasks)})

        download_tasks = [
            task
            for task in tasks
            if self._task_matches_intent(
                task,
                entity=ManagerEntityKind.ATTACHMENT,
                actions={ManagerAction.DOWNLOAD},
                capability=CapabilityCode.ECLASS_QUERY,
            )
        ]
        document_tasks = [
            task
            for task in tasks
            if task.agent is ExecutionTargetName.DOCUMENT
            and task.capability is CapabilityCode.DOCUMENT_ANALYSIS
            and task.entity is ManagerEntityKind.DOCUMENT
            and task.action is ManagerAction.ANALYZE
        ]
        # 첨부 다운로드나 문서 분석 작업이 아닌 task에는 파일 대상을 연결하지 않는다.
        if not download_tasks and not document_tasks:
            return plan.model_copy(update={"tasks": tasks})
        selector_task = download_tasks[0] if download_tasks else document_tasks[0]
        matched: list[dict[str, object]] = []
        number = selector_task.slots.ordinal
        if number is None:
            number_match = re.search(r"(?<!\d)(\d+)\s*번", user_message)
            number = int(number_match.group(1)) if number_match else None
        if number is not None:
            matched = [item for item in items if item.get("number") == number]

        # Manager가 query를 빠뜨렸더라도 사용자가 실제 파일명을 쓴 경우에는 그 파일명을
        # 강제 선택자로 취급한다. `missing.pdf`가 없을 때 다른 PDF 하나로 대체하면 안 된다.
        filename_match = re.search(
            # 변환 지원 확장자를 하드코딩하지 않는다. txt/md/csv/json/py 등도 명시 파일명이며,
            # 새 형식이 추가되어도 `없는 파일 → 같은 확장자의 다른 파일`로 완화되면 안 된다.
            r"([^\s/\\]+\.[A-Za-z0-9]{1,10})(?=$|\s|[을를은는이가의와과도만])",
            user_message,
            re.IGNORECASE,
        )
        # 사용자 원문에 파일명이 있으면 그것이 유일한 권위 있는 선택자다. Manager가
        # ``slots.query``에 현재 후보의 다른 파일명을 생성했더라도 그 값을 우선하면
        # ``missing.pdf`` 요청이 ``real.pdf`` 분석으로 바뀔 수 있으므로 절대 대체하지 않는다.
        explicit_query = filename_match.group(1) if filename_match is not None else selector_task.slots.query
        has_explicit_selector = number is not None or explicit_query is not None
        if explicit_query:
            query_pool = matched if number is not None else items
            query_matches = [
                item
                for item in query_pool
                if self._text_matches(
                    explicit_query,
                    item.get("name"),
                    item.get("id"),
                )
            ]
            # 복수 대용 표현에서 Manager가 query에 "파일들" 같은 일반어를 넣은 것은
            # 파일 선택자가 아니다. 사용자 원문에 실제 파일명이 있으면 기존처럼 엄격하게
            # 실패시키고, 일반 복수 요청일 때만 전체 후보 선택으로 이어간다.
            if query_matches:
                matched = query_matches
            elif plural_intent and filename_match is None and number is None:
                explicit_query = None
                has_explicit_selector = False
                matched = []
            else:
                matched = []
        elif number is None and not matched:
            matched = [item for item in items if str(item.get("name") or "") in user_message]
        # 번호·파일명·query를 명시했는데 일치하지 않으면 확장자나 단일 후보로 완화하지 않는다.
        # 이 분기 덕분에 과거에 받은 다른 파일의 download ref도 이후 Document 단계에 갈 수 없다.
        if has_explicit_selector and not matched:
            return plan.model_copy(update={"tasks": tasks})
        if not matched:
            extension_words = {
                "pdf": ".pdf",
                "워드": ".docx",
                "docx": ".docx",
                "한글": ".hwp",
                "hwp": ".hwp",
                "압축": ".zip",
                "zip": ".zip",
            }
            requested_extensions = {
                extension for word, extension in extension_words.items() if word in compact
            }
            if requested_extensions:
                matched = [
                    item
                    for item in items
                    if any(str(item.get("name") or "").casefold().endswith(ext) for ext in requested_extensions)
                ]
        if not matched and plural_intent:
            matched = items
        if not matched and len(items) == 1 and any(
            word in compact for word in ("파일", "첨부", "문서", "내용", "분석")
        ):
            matched = [items[0]]
        if len(matched) > 1 and not plural_intent:
            return plan.model_copy(update={"tasks": tasks})
        if not matched or len(matched) > 5:
            return plan.model_copy(update={"tasks": tasks})

        try:
            targets = [
                VerifiedAttachmentTarget(
                    id=str(item["id"]),
                    name=str(item["name"]),
                    url=str(item["url"]),
                    parent_id=str(item["parent_id"]),
                )
                for item in matched
            ]
        except (KeyError, TypeError, ValueError):
            return plan.model_copy(update={"tasks": tasks})
        # 하나의 복수 요청이 서로 다른 과제 파일을 합치지 못하게 한다. 정상적인 첨부 목록
        # Snapshot은 한 과제에서 생성되므로 부모가 섞였다면 오래되었거나 손상된 문맥이다.
        if len({target.parent_id for target in targets}) != 1:
            return plan.model_copy(update={"tasks": tasks})
        target = targets[0]
        is_batch = len(targets) > 1

        enriched: list[ManagerTask] = []
        attached = False
        for task in tasks:
            if download_tasks and task is download_tasks[0]:
                task = task.model_copy(
                    update=(
                        {"verified_attachment_targets": targets}
                        if is_batch
                        else {"verified_attachment_target": target}
                    )
                )
                attached = True
            if task in document_tasks:
                task = task.model_copy(
                    update=(
                        {
                            "verified_attachment_targets": targets,
                            "verified_attachment_ids": [item.id for item in targets],
                        }
                        if is_batch
                        else {
                            "verified_attachment_target": target,
                            "verified_attachment_id": target.id,
                        }
                    )
                )
            enriched.append(task)
        # 모델이 Document 단계만 만들었어도 검증된 파일이 하나라면 다운로드 단계를 앞에 보충한다.
        if not attached and any(task.agent is ExecutionTargetName.DOCUMENT for task in tasks):
            download_task = ManagerTask(
                agent=ExecutionTargetName.ECLASS,
                capability=CapabilityCode.ECLASS_QUERY,
                entity=ManagerEntityKind.ATTACHMENT,
                action=ManagerAction.DOWNLOAD,
                slots=selector_task.slots,
                instruction=(
                    f"같은 과제의 검증된 첨부파일 {len(targets)}개를 다운로드한다."
                    if is_batch
                    else f"검증된 첨부파일 '{target.name}'을 다운로드한다."
                ),
                verified_attachment_target=None if is_batch else target,
                verified_attachment_targets=targets if is_batch else [],
            )
            enriched.insert(0, download_task)
        return plan.model_copy(update={"tasks": enriched[:4]})

    async def _create_plan_with_retry(
        self,
        event: RuntimeEvent,
        *,
        on_manager_delta: TextDeltaHandler | None,
    ) -> ManagerPlan:
        """일시적인 API·구조화 출력 실패만 스트리밍 없이 정확히 한 번 재시도한다."""

        try:
            return await create_plan(
                event,
                self.context.model_copy(deep=True),
                self.settings,
                on_text_delta=on_manager_delta,
            )
        except OpenAiApiKeyRequiredError:
            raise
        except Exception:
            # 첫 시도에서 일부 delta가 표시됐을 수 있어 재시도 응답은 스트리밍하지 않는다.
            return await create_plan(
                event,
                self.context.model_copy(deep=True),
                self.settings,
                on_text_delta=None,
            )

    @staticmethod
    def _system_event_should_notify(event: RuntimeEvent) -> bool:
        """검증된 변경·마감 이벤트만 Manager의 선제 알림을 허용한다."""

        if event.event_type in {
            RuntimeEventType.LMS_CHANGED,
            RuntimeEventType.DEADLINE_WARNING,
            RuntimeEventType.ATTENDANCE_WARNING,
        }:
            return True
        if event.event_type is RuntimeEventType.STARTUP_BRIEFING:
            return any(
                int(event.payload.get(key, 0) or 0) > 0
                for key in ("incomplete_assignment_count", "unwatched_lecture_count", "change_count")
            )
        return False

    @staticmethod
    def _system_event_priority(event: RuntimeEvent) -> ManagerPriority:
        if event.event_type in {RuntimeEventType.DEADLINE_WARNING, RuntimeEventType.ATTENDANCE_WARNING}:
            return ManagerPriority.HIGH
        return ManagerPriority.NORMAL

    async def _execute_plan(
        self,
        plan: ManagerPlan,
        *,
        on_progress: ProgressHandler | None,
    ) -> ManagerResult:
        if len(plan.tasks) > self.max_agent_steps:
            return self._failure(
                "한 요청에서 허용된 Agent 실행 단계 수를 초과했습니다.",
                ErrorCode.WORKFLOW_LIMIT_REACHED,
            )
        dependency_error = self._validate_plan_dependencies(plan)
        if dependency_error is not None:
            return self._failure(dependency_error, ErrorCode.MANAGER_FAILED)

        seen_steps: set[tuple[ExecutionTargetName, str, str, str, str]] = set()
        delegated: list[ExecutionTargetName] = []
        evidence_refs: list[str] = []
        suggested_actions: list[str] = []
        specialist_results: list[SpecialistResult] = []

        for task in plan.tasks:
            step_key = (
                task.agent,
                task.capability.value,
                task.entity.value,
                task.action.value,
                task.slots.model_dump_json(exclude_none=True),
            )
            if step_key in seen_steps:
                return self._failure(
                    "같은 실행 대상과 기능이 반복되어 작업을 중단했습니다.",
                    ErrorCode.WORKFLOW_LIMIT_REACHED,
                    delegated=delegated,
                )
            seen_steps.add(step_key)
            delegated.append(task.agent)
            await self._emit(on_progress, RuntimeProgressEvent.AGENT_DELEGATED, task.agent.value)

            handler = self._specialist_handlers.get(task.agent, self._capability_not_ready)
            # Specialist가 자연어 instruction에서 evidence 문자열을 재파싱하지 않도록 한다.
            # Document 단계는 선택된 attachment ID와 같은 download ref만 받는다. 결박이 없는
            # 일반 요청은 이번 실행에서 새로 내려받은 ref만, `그 파일` 요청만 과거 ref를 받는다.
            effective_task = task.model_copy(update={"verified_input_refs": []})
            if task.agent is ExecutionTargetName.DOCUMENT:
                historical_refs = self.context.last_verified_entity_refs
                expected_attachment_ids = (
                    task.verified_attachment_ids
                    if task.verified_attachment_ids
                    else (
                        [task.verified_attachment_id]
                        if task.verified_attachment_id is not None
                        else []
                    )
                )
                ref_pool = (
                    [*historical_refs, *evidence_refs]
                    if expected_attachment_ids
                    or task.reuse_latest_verified_download
                    else evidence_refs
                )
                latest_ref_by_attachment: dict[str, str] = {}
                for ref in ref_pool:
                    parsed = parse_verified_download_ref(ref)
                    if parsed is None:
                        continue
                    if expected_attachment_ids and parsed[1] not in expected_attachment_ids:
                        continue
                    latest_ref_by_attachment[parsed[1]] = ref
                if expected_attachment_ids:
                    verified_download_refs = [
                        latest_ref_by_attachment[attachment_id]
                        for attachment_id in expected_attachment_ids
                        if attachment_id in latest_ref_by_attachment
                    ]
                    all_expected_downloaded = len(verified_download_refs) == len(
                        expected_attachment_ids
                    )
                else:
                    verified_download_refs = list(latest_ref_by_attachment.values())
                    # 결박 없는 Document task는 이번 실행에서 막 내려받은 결과만 사용할 수
                    # 있다. 과거 전역 참조를 묶음으로 재사용해 다른 과제 파일을 섞지 않는다.
                    all_expected_downloaded = bool(verified_download_refs)
                if all_expected_downloaded:
                    runtime_targets = self._attachment_targets_from_results(
                        specialist_results,
                        verified_download_refs,
                    )
                    runtime_update: dict[str, object] = {
                        "verified_input_refs": (
                            [verified_download_refs[-1]]
                            if task.reuse_latest_verified_download
                            and not expected_attachment_ids
                            else verified_download_refs
                        )
                    }
                    if runtime_targets and len(runtime_targets) == len(verified_download_refs):
                        runtime_update.update(
                            {
                                "verified_attachment_target": None,
                                "verified_attachment_targets": runtime_targets,
                                "verified_attachment_id": None,
                                "verified_attachment_ids": [
                                    target.id for target in runtime_targets
                                ],
                            }
                        )
                    effective_task = task.model_copy(
                        update=runtime_update
                    )
                else:
                    return self._failure(
                        "선택한 첨부파일의 검증된 다운로드 결과가 없습니다. 첨부파일을 다시 선택해 주세요.",
                        ErrorCode.INVALID_REQUEST,
                        delegated=delegated,
                    )
            result = await handler(effective_task)
            consume_trace = getattr(handler, "consume_trace_events", None)
            if consume_trace is not None and self.context.last_request_id:
                trace_events = consume_trace()
                if inspect.isawaitable(trace_events):
                    trace_events = await trace_events
                if not isinstance(trace_events, (list, tuple)):
                    trace_events = []
                for component, state in trace_events:
                    await self._audit_safely(
                        "step",
                        self.context.last_request_id,
                        component=component,
                        state=state,
                    )
            if self.context.last_request_id:
                await self._audit_safely(
                    "step",
                    self.context.last_request_id,
                    component=task.agent.value,
                    state=result.status.value,
                    code=result.error_code.value if result.error_code else None,
                )
            specialist_results.append(result)
            evidence_refs.extend(result.evidence_refs)
            suggested_actions.extend(result.suggested_actions)
            playback_refs = {
                ref for ref in result.evidence_refs if ref.startswith("playback:")
            }
            if result.status is SpecialistStatus.COMPLETED:
                if task.action in {ManagerAction.PLAY, ManagerAction.PREVIEW}:
                    self._active_playback_refs.update(playback_refs)
                elif task.action is ManagerAction.STOP:
                    self._active_playback_refs.difference_update(playback_refs)

            if result.status is SpecialistStatus.CAPABILITY_NOT_READY:
                await self._emit(on_progress, RuntimeProgressEvent.CAPABILITY_NOT_READY, task.agent.value)
                return ManagerResult(
                    status=ManagerStatus.CAPABILITY_NOT_READY,
                    message=result.summary,
                    should_notify=True,
                    priority=ManagerPriority.NORMAL,
                    suggested_actions=result.suggested_actions,
                    delegated_agents=delegated,
                    evidence_refs=evidence_refs,
                    error_code=result.error_code,
                )
            if result.status is SpecialistStatus.AUTH_REQUIRED:
                await self._emit(on_progress, RuntimeProgressEvent.AUTH_REQUIRED, task.agent.value)
                return ManagerResult(
                    status=ManagerStatus.AUTH_REQUIRED,
                    message=result.summary,
                    should_notify=True,
                    priority=ManagerPriority.HIGH,
                    suggested_actions=result.suggested_actions,
                    delegated_agents=delegated,
                    error_code=ErrorCode.AUTH_REQUIRED,
                )
            if result.status is SpecialistStatus.FAILED:
                await self._emit(on_progress, RuntimeProgressEvent.ERROR, task.agent.value)
                return self._failure(
                    result.summary,
                    result.error_code or ErrorCode.TEMPORARY_FAILURE,
                    delegated=delegated,
                )

        # 새 단계가 document:* 참조만 반환해도 직전의 download:* 검증 참조를 잃지 않는다.
        # 후속 "그 파일 다시 분석" 의존 검사는 누적된 최근 참조에서 안전하게 이어진다.
        self.context.last_verified_entity_refs = list(
            dict.fromkeys([*self.context.last_verified_entity_refs, *evidence_refs])
        )[-100:]
        # 다음 입력이 "날짜순으로"처럼 대상을 생략해도 직전의 검증된 LMS 작업을 이어갈 수 있도록
        # 사용자 원문 전체 대신 Manager 작업 범위와 전문 Agent 결과 요약만 제한적으로 보존한다.
        self.context.last_specialist_scope = "\n".join(
            (
                f"{task.agent.value}/{task.capability.value}/"
                f"{task.entity.value}/{task.action.value} "
                f"slots={task.slots.model_dump(mode='json', exclude_none=True)}: "
                f"{task.instruction}"
            )
            for task in plan.tasks
        )[-2_000:]
        self._remember_verified_snapshots(specialist_results)
        # 호환 문자열에는 마지막의 유효한 JSON 하나만 둔다. 여러 JSON을 개행으로 합쳐 파싱할 수
        # 없게 만들지 않으며, 상세 조회처럼 새 후보가 없는 작업은 이전 목록 문맥을 보존한다.
        followup_contexts = [
            result.verified_followup_context
            for result in specialist_results
            if result.verified_followup_context
            and str(result.verified_followup_context).lstrip().startswith("{")
        ]
        if followup_contexts:
            self.context.last_verified_result_summary = followup_contexts[-1][-12_000:]
        self.context.last_specialist_agents = [agent.value for agent in delegated]
        # 별도 synthesis Agent를 호출하지 않는다. 검증 Tool이 만든 표시 문자열을 우선해 문자와
        # 식별자를 보존하고, 여러 단계일 때만 실행 대상 제목을 결정론적으로 붙인다.
        display_blocks = [
            result.verified_display_text or result.summary
            for result in specialist_results
        ]
        message = display_blocks[0] if len(display_blocks) == 1 else "\n\n".join(
            f"[{task.agent.value}]\n{text}"
            for task, text in zip(plan.tasks, display_blocks, strict=True)
        )
        return ManagerResult(
            status=ManagerStatus.COMPLETED,
            message=message,
            should_notify=True,
            priority=ManagerPriority.NORMAL,
            suggested_actions=list(dict.fromkeys(suggested_actions)),
            delegated_agents=delegated,
            evidence_refs=self.context.last_verified_entity_refs,
        )

    def _validate_plan_dependencies(self, plan: ManagerPlan) -> str | None:
        """Agent와 결정론적 Service의 데이터 의존 순서를 실행 전에 검사한다."""

        agents = [task.agent for task in plan.tasks]
        for document_index, document_task in enumerate(plan.tasks):
            if document_task.agent is not ExecutionTargetName.DOCUMENT:
                continue
            has_eclass_before = ExecutionTargetName.ECLASS in agents[:document_index]
            historical_downloads = [
                parsed
                for ref in self.context.last_verified_entity_refs
                if (parsed := parse_verified_download_ref(ref)) is not None
            ]
            expected_attachment_ids = (
                document_task.verified_attachment_ids
                if document_task.verified_attachment_ids
                else (
                    [document_task.verified_attachment_id]
                    if document_task.verified_attachment_id is not None
                    else []
                )
            )
            if expected_attachment_ids:
                downloaded_ids = {attachment_id for _, attachment_id in historical_downloads}
                has_verified_input = all(
                    attachment_id in downloaded_ids for attachment_id in expected_attachment_ids
                )
            elif document_task.reuse_latest_verified_download:
                has_verified_input = bool(historical_downloads)
            else:
                # 명시 파일 선택에 실패한 요청은 다른 과거 파일로 조용히 대체하지 않는다.
                has_verified_input = False
            if not has_verified_input and not has_eclass_before:
                return "검증된 첨부 문서 없이 Document Analysis Agent를 실행할 수 없습니다."
        if ExecutionTargetName.DOCUMENT in agents and ExecutionTargetName.MISSION_SERVICE in agents:
            if agents.index(ExecutionTargetName.MISSION_SERVICE) < agents.index(ExecutionTargetName.DOCUMENT):
                return "문서 분석보다 먼저 Mission Service를 실행할 수 없습니다."
        return None

    @staticmethod
    async def _capability_not_ready(task: ManagerTask) -> SpecialistResult:
        """MCP 연결 전 가짜 결과 대신 명시적인 미지원 상태를 반환한다."""

        return SpecialistResult(
            status=SpecialistStatus.CAPABILITY_NOT_READY,
            summary=f"{task.agent.value}의 실제 Tool 연결이 아직 준비되지 않았습니다.",
            suggested_actions=["E-Class MCP 읽기 Tool을 연결한 뒤 다시 실행하세요."],
        )

    def _update_context(self, plan: ManagerPlan, event: RuntimeEvent) -> None:
        self.context.safe_summary = plan.conversation_summary
        self.context.turn_count += 1
        self.context.last_event_id = event.event_id

    def _remember_exchange(self, user_message: str, assistant_message: str) -> None:
        """이번 요청과 최종 답변을 다음 후속 요청을 위한 세션 문맥에 추가한다."""

        self._remember_turn("user", user_message)
        self._remember_turn("assistant", assistant_message)

    def _remember_turn(self, role: str, content: str) -> None:
        """설정된 인증값과 흔한 비밀값 표기를 지운 뒤 최근 대화만 제한적으로 보존한다."""

        safe_content = self._redact_secrets(content).strip()
        if not safe_content:
            return
        self.context.recent_turns.append(
            ConversationTurn(role=role, content=safe_content[-4_000:])
        )
        self.context.recent_turns = self.context.recent_turns[-self.MAX_RECENT_TURNS :]

    def _redact_secrets(self, text: str) -> str:
        """Agent 문맥에 계정·비밀번호·토큰 값이 재전달되지 않도록 평문을 마스킹한다."""

        redacted = text
        for secret in (self.settings.eclass_username, self.settings.eclass_password):
            if secret is None:
                continue
            value = secret.get_secret_value()
            if value:
                redacted = redacted.replace(value, "[REDACTED]")
        patterns = (
            r"(?i)\b(password|passwd|pwd|token|api[_ -]?key|cookie)\b\s*[:=]\s*\S+",
            r"(비밀번호|암호|토큰)\s*(?:[:=]\s*|\s+)\S+",
        )
        for pattern in patterns:
            redacted = re.sub(pattern, lambda match: f"{match.group(1)}=[REDACTED]", redacted)
        return redacted

    def _trace_disabled(self) -> bool:
        """테스트용 키나 키 미설정 환경에서는 SDK trace 전송을 비활성화한다."""

        api_key = (self.settings.openai_api_key or "").strip()
        return not api_key or api_key == "..." or api_key.startswith("test-")

    @staticmethod
    async def _emit(
        handler: ProgressHandler | None,
        event: RuntimeProgressEvent,
        agent: str | None,
    ) -> None:
        if handler is None:
            return
        result = handler(event, agent)
        if inspect.isawaitable(result):
            await result

    @staticmethod
    def _failure(
        message: str,
        error_code: ErrorCode,
        *,
        delegated: list[ExecutionTargetName] | None = None,
    ) -> ManagerResult:
        return ManagerResult(
            status=ManagerStatus.FAILED,
            message=message,
            should_notify=True,
            priority=ManagerPriority.HIGH,
            delegated_agents=delegated or [],
            error_code=error_code,
        )

    async def shutdown(self) -> None:
        """TUI 종료 시 큐를 닫고 이후 이벤트 수신을 막는다."""

        if self._closed:
            return
        self._closed = True
        await self.event_queue.close()
        for handler in self._specialist_handlers.values():
            close = getattr(handler, "close", None)
            if close is not None:
                result = close()
                if inspect.isawaitable(result):
                    await result

    async def _audit_safely(self, method: str, request_id: str, **kwargs) -> None:
        """관측 DB 장애 때문에 LMS 기능이 실패하지 않도록 감사 저장 오류만 격리한다."""

        try:
            # 관측 저장소가 느리거나 꺼져 있어도 사용자 요청의 지연 원인이 되지 않는다.
            await asyncio.wait_for(
                getattr(self._audit, method)(request_id, **kwargs),
                timeout=0.25,
            )
        except Exception:
            return
