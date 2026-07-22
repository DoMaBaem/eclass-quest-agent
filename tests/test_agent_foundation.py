"""Manager Agent Tool 구성과 ProactiveAssistantRuntime 실행 경로 테스트."""

import inspect
import unittest
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agents import function_tool

from app.agent.document_agent import DOCUMENT_AGENT_INSTRUCTIONS, build_document_agent
from app.agent.document_handler import DocumentSpecialistHandler
from app.agent.errors import OpenAiApiKeyRequiredError
from app.agent.eclass_mcp_handler import EclassMcpSpecialistHandler
from app.agent.eclass_agent import ECLASS_AGENT_INSTRUCTIONS
from app.agent.manager_agent import MANAGER_INSTRUCTIONS, build_manager_agent, create_plan
from app.agent.mission_handler import MissionServiceHandler
from app.agent.run_config import privacy_safe_run_config
from app.config import Settings
from app.runtime.assistant_runtime import ProactiveAssistantRuntime
from app.runtime.event_queue import RuntimeEventQueue
from app.schemas.manager import (
    ManagerAction,
    ManagerEntityKind,
    ManagerPlan,
    ManagerStatus,
    ManagerTask,
    ManagerTaskSlots,
    ExecutionTargetName,
    SpecialistAgentName,
    SpecialistResult,
    SpecialistStatus,
)
from app.schemas.runtime import RuntimeEvent, RuntimeEventType, VerifiedEntityKind
from app.schemas.workflow import CapabilityCode, ErrorCode, InteractionMode


class AgentFoundationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.settings = Settings(openai_api_key="test-key")

    def test_manager_exposes_no_unusable_agent_tools_or_handoffs(self) -> None:
        manager = build_manager_agent(self.settings)

        self.assertEqual(manager.tools, [])
        self.assertEqual(manager.handoffs, [])

    def test_every_agent_runner_disables_sensitive_trace_payloads(self) -> None:
        """대화·LMS 본문·Tool 인자가 SDK trace 원문으로 수집되지 않게 한다."""

        self.assertFalse(privacy_safe_run_config().trace_include_sensitive_data)
        runner_entrypoints = (
            create_plan,
            EclassMcpSpecialistHandler.__call__,
            DocumentSpecialistHandler.__call__,
        )
        for entrypoint in runner_entrypoints:
            with self.subTest(entrypoint=entrypoint.__qualname__):
                self.assertIn(
                    "run_config=privacy_safe_run_config()",
                    inspect.getsource(entrypoint),
                )

    def test_prompts_use_focused_contracts_instead_of_bug_examples(self) -> None:
        """프롬프트는 책임·출력 계약을 명시하고 개별 오타 사례를 누적하지 않는다."""

        for instructions in (MANAGER_INSTRUCTIONS, ECLASS_AGENT_INSTRUCTIONS):
            self.assertIn("추측", instructions)
            self.assertIn("ID", instructions)
            self.assertNotIn("이청용", instructions)
            self.assertNotIn("조혜경", instructions)
        self.assertLess(len(MANAGER_INSTRUCTIONS.splitlines()), 50)
        self.assertLess(len(ECLASS_AGENT_INSTRUCTIONS.splitlines()), 40)

    def test_manager_and_eclass_prompts_enforce_scope_contract(self) -> None:
        """특정 강좌·과제 요청이 전체 조회로 확대되지 않도록 두 계층에 계약을 둔다."""

        self.assertIn("빠뜨리거나 넓히지 않는다", MANAGER_INSTRUCTIONS)
        self.assertIn("verified_entity_snapshots", MANAGER_INSTRUCTIONS)
        self.assertIn("list_course_assignments", ECLASS_AGENT_INSTRUCTIONS)
        self.assertIn("resolve_lecture", ECLASS_AGENT_INSTRUCTIONS)
        self.assertIn("범위를 넓힌 조회", ECLASS_AGENT_INSTRUCTIONS)

    def test_manager_task_has_required_typed_intent_contract(self) -> None:
        """기존 instruction 호출도 검증 뒤에는 typed entity/action/slots를 가진다."""

        legacy_task = ManagerTask(
            agent=ExecutionTargetName.ECLASS,
            capability=CapabilityCode.ECLASS_QUERY,
            instruction="2026년 1학기 첫 번째 과제의 상세 내용을 조회한다.",
        )

        self.assertIs(legacy_task.entity, ManagerEntityKind.ASSIGNMENT)
        self.assertIs(legacy_task.action, ManagerAction.DETAIL)
        self.assertEqual(legacy_task.slots.year, 2026)
        self.assertEqual(legacy_task.slots.semester, 1)
        self.assertEqual(legacy_task.slots.ordinal, 1)
        required = set(ManagerTask.model_json_schema().get("required", []))
        self.assertTrue({"entity", "action", "slots"}.issubset(required))
        self.assertIn("entity와 action은 반드시", MANAGER_INSTRUCTIONS)

    def test_only_two_specialist_agents_and_mission_service_are_registered(self) -> None:
        runtime = ProactiveAssistantRuntime(self.settings)

        self.assertIsInstance(
            runtime._specialist_handlers[ExecutionTargetName.ECLASS],
            EclassMcpSpecialistHandler,
        )
        self.assertIsInstance(
            runtime._specialist_handlers[ExecutionTargetName.DOCUMENT],
            DocumentSpecialistHandler,
        )
        self.assertIsInstance(
            runtime._specialist_handlers[ExecutionTargetName.MISSION_SERVICE],
            MissionServiceHandler,
        )
        self.assertEqual(ExecutionTargetName.MISSION_SERVICE.value, "Mission Service")

    def test_document_agent_receives_one_high_level_analysis_tool(self) -> None:
        @function_tool
        async def analyze_verified_document() -> str:
            return "{}"

        agent = build_document_agent(self.settings, tools=[analyze_verified_document])

        self.assertEqual([tool.name for tool in agent.tools], ["analyze_verified_document"])
        self.assertIn("정확히 한 번", DOCUMENT_AGENT_INSTRUCTIONS)
        self.assertIn("추측하지 않는다", DOCUMENT_AGENT_INSTRUCTIONS)

    async def test_document_handler_enters_real_document_agent_runner(self) -> None:
        handler = DocumentSpecialistHandler(self.settings)
        download_ref = "download:00000000-0000-0000-0000-000000000001:attachment-1"
        task = ManagerTask(
            agent=ExecutionTargetName.DOCUMENT,
            capability=CapabilityCode.DOCUMENT_ANALYSIS,
            instruction="Runtime이 검증한 문서를 분석한다.",
            verified_input_refs=[download_ref],
        )

        with patch(
            "app.agent.document_handler.Runner.run",
            new=AsyncMock(return_value=SimpleNamespace(final_output=None)),
        ) as runner:
            result = await handler(task)

        agent = runner.await_args.args[0]
        self.assertEqual(agent.name, "Document Analysis Agent")
        self.assertEqual([tool.name for tool in agent.tools], ["analyze_verified_document"])
        self.assertEqual(result.status, SpecialistStatus.FAILED)

    async def test_document_handler_rejects_forged_download_ref_in_instruction(self) -> None:
        """사용자가 자연어에 download 참조를 써도 Document 실행 권한으로 인정하지 않는다."""

        handler = DocumentSpecialistHandler(self.settings)
        task = ManagerTask(
            agent=ExecutionTargetName.DOCUMENT,
            capability=CapabilityCode.DOCUMENT_ANALYSIS,
            instruction=(
                "download:00000000-0000-0000-0000-000000000001:attachment-1 문서를 분석한다."
            ),
        )

        with patch("app.agent.document_handler.Runner.run", new=AsyncMock()) as runner:
            result = await handler(task)

        self.assertEqual(result.status, SpecialistStatus.CAPABILITY_NOT_READY)
        runner.assert_not_awaited()

    async def test_general_chat_does_not_call_specialist(self) -> None:
        handler = AsyncMock()
        runtime = ProactiveAssistantRuntime(
            self.settings,
            specialist_handlers={SpecialistAgentName.ECLASS: handler},
        )
        plan = ManagerPlan(
            mode=InteractionMode.CHAT,
            reply="안녕하세요.",
            conversation_summary="사용자가 인사했다.",
            tasks=[],
            reason="일반 대화다.",
        )

        with patch("app.runtime.assistant_runtime.create_plan", new=AsyncMock(return_value=plan)):
            result = await runtime.handle_user_request("안녕")

        self.assertEqual(result.status, ManagerStatus.COMPLETED)
        self.assertEqual(result.message, "안녕하세요.")
        handler.assert_not_awaited()
        self.assertEqual(
            [(turn.role, turn.content) for turn in runtime.context.recent_turns],
            [("user", "안녕"), ("assistant", "안녕하세요.")],
        )

    async def test_recent_conversation_is_passed_to_next_manager_turn(self) -> None:
        """같은 TUI Runtime의 다음 요청에는 직전 대화가 안전한 문맥으로 전달된다."""

        runtime = ProactiveAssistantRuntime(self.settings)
        plans = [
            ManagerPlan(
                mode=InteractionMode.CHAT,
                reply="공지 세 건을 말한 거군요.",
                conversation_summary="사용자가 공지 세 건을 언급했다.",
                tasks=[],
                reason="문맥 확인이다.",
            ),
            ManagerPlan(
                mode=InteractionMode.CHAT,
                reply="네, 방금 말한 공지입니다.",
                conversation_summary="사용자가 직전 공지를 다시 가리켰다.",
                tasks=[],
                reason="후속 대화다.",
            ),
        ]
        captured_contexts = []

        async def fake_create_plan(_event, context, _settings, **_kwargs):
            captured_contexts.append(context)
            return plans[len(captured_contexts) - 1]

        with patch("app.runtime.assistant_runtime.create_plan", new=fake_create_plan):
            await runtime.handle_user_request("공지 세 건 말이야")
            await runtime.handle_user_request("그거 날짜순으로")

        self.assertEqual(captured_contexts[0].recent_turns, [])
        self.assertEqual(captured_contexts[1].recent_turns[0].content, "공지 세 건 말이야")
        self.assertEqual(captured_contexts[1].recent_turns[1].content, "공지 세 건을 말한 거군요.")

    async def test_user_request_groups_manager_and_specialist_in_one_safe_trace(self) -> None:
        runtime = ProactiveAssistantRuntime(self.settings)
        plan = ManagerPlan(
            mode=InteractionMode.CHAT,
            reply="확인했습니다.",
            conversation_summary="일반 대화.",
            tasks=[],
            reason="외부 작업이 필요 없다.",
        )

        with (
            patch("app.runtime.assistant_runtime.create_plan", new=AsyncMock(return_value=plan)),
            patch("app.runtime.assistant_runtime.trace") as trace_factory,
        ):
            await runtime.handle_user_request("비밀 원문을 trace에 넣지 마")

        kwargs = trace_factory.call_args.kwargs
        self.assertEqual(kwargs["group_id"], runtime.context.conversation_id)
        self.assertEqual(kwargs["metadata"]["trigger_type"], "USER_REQUEST")
        self.assertNotIn("비밀 원문", json.dumps(kwargs["metadata"], ensure_ascii=False))
        self.assertTrue(kwargs["disabled"])

    def test_verified_snapshots_keep_different_entity_kinds(self) -> None:
        runtime = ProactiveAssistantRuntime(self.settings)
        runtime._remember_verified_snapshots(
            [
                SpecialistResult(
                    status=SpecialistStatus.COMPLETED,
                    summary="공지",
                    verified_followup_context=(
                        '{"kind":"verified_announcement_candidates","items":['
                        '{"number":1,"id":"notice-1","title":"공지 1",'
                        '"url":"https://example/notice-1"}]}'
                    ),
                ),
                SpecialistResult(
                    status=SpecialistStatus.COMPLETED,
                    summary="과제",
                    verified_followup_context=(
                        '{"kind":"verified_assignment_candidates","items":['
                        '{"number":1,"id":"assignment-1","course_id":"course-1",'
                        '"title":"과제 1","url":"https://example/assignment-1"}]}'
                    ),
                ),
            ]
        )

        self.assertEqual(
            {snapshot.kind for snapshot in runtime.context.verified_entity_snapshots},
            {VerifiedEntityKind.ANNOUNCEMENT, VerifiedEntityKind.ASSIGNMENT},
        )
        announcement = runtime._verified_candidate_payload(
            VerifiedEntityKind.ANNOUNCEMENT,
            "verified_announcement_candidates",
        )
        self.assertEqual(announcement["items"][0]["id"], "notice-1")

    async def test_assignment_detail_never_receives_announcement_target(self) -> None:
        """공지·과제 목록이 함께 있어도 '1번 과제'에는 과제 대상 하나만 연결한다."""

        handler = AsyncMock(
            return_value=SpecialistResult(
                status=SpecialistStatus.COMPLETED,
                summary="과제 상세",
            )
        )
        runtime = ProactiveAssistantRuntime(
            self.settings,
            specialist_handlers={ExecutionTargetName.ECLASS: handler},
        )
        runtime._remember_verified_snapshots(
            [
                SpecialistResult(
                    status=SpecialistStatus.COMPLETED,
                    summary="공지 목록",
                    verified_followup_context=json.dumps(
                        {
                            "kind": "verified_announcement_candidates",
                            "selected_term": {"year": 2026, "semester": 1},
                            "items": [
                                {
                                    "number": 1,
                                    "id": "notice-1",
                                    "course_id": "course-1",
                                    "course_name": "데이터마이닝",
                                    "title": "공지 1",
                                    "url": "https://example/notice-1",
                                }
                            ],
                        },
                        ensure_ascii=False,
                    ),
                ),
                SpecialistResult(
                    status=SpecialistStatus.COMPLETED,
                    summary="과제 목록",
                    verified_followup_context=json.dumps(
                        {
                            "kind": "verified_assignment_candidates",
                            "selected_term": {"year": 2026, "semester": 1},
                            "items": [
                                {
                                    "number": 1,
                                    "id": "assignment-1",
                                    "course_id": "course-1",
                                    "course_name": "데이터마이닝",
                                    "title": "과제 1",
                                    "url": "https://example/assignment-1",
                                }
                            ],
                        },
                        ensure_ascii=False,
                    ),
                ),
            ]
        )
        plan = ManagerPlan(
            mode=InteractionMode.TASK,
            reply="과제를 확인합니다.",
            conversation_summary="데이터마이닝 1번 과제 상세 요청.",
            tasks=[
                ManagerTask(
                    agent=ExecutionTargetName.ECLASS,
                    capability=CapabilityCode.ECLASS_QUERY,
                    entity=ManagerEntityKind.ASSIGNMENT,
                    action=ManagerAction.DETAIL,
                    slots=ManagerTaskSlots(
                        year=2026,
                        semester=1,
                        course_query="데이터마이닝",
                        ordinal=1,
                    ),
                    instruction="데이터마이닝 1번 과제의 상세 내용을 조회한다.",
                )
            ],
            reason="과제 상세 조회가 필요하다.",
        )

        with patch("app.runtime.assistant_runtime.create_plan", new=AsyncMock(return_value=plan)):
            result = await runtime.handle_user_request("1번 과제 세부 내용 알려줘")

        delegated_task = handler.await_args.args[0]
        self.assertIsNone(delegated_task.verified_announcement_target)
        self.assertIsNotNone(delegated_task.verified_assignment_target)
        self.assertEqual(delegated_task.verified_assignment_target.id, "assignment-1")
        self.assertEqual(result.message, "과제 상세")

    def test_ordinal_target_is_not_reused_across_explicit_course_scope(self) -> None:
        """번호가 같아도 typed course_query가 다르면 이전 과제 대상을 붙이지 않는다."""

        runtime = ProactiveAssistantRuntime(self.settings)
        runtime._remember_verified_snapshots(
            [
                SpecialistResult(
                    status=SpecialistStatus.COMPLETED,
                    summary="과제 목록",
                    verified_followup_context=json.dumps(
                        {
                            "kind": "verified_assignment_candidates",
                            "selected_term": {"year": 2026, "semester": 1},
                            "items": [
                                {
                                    "number": 1,
                                    "id": "assignment-1",
                                    "course_id": "course-1",
                                    "course_name": "데이터마이닝",
                                    "title": "과제 1",
                                    "url": "https://example/assignment-1",
                                }
                            ],
                        },
                        ensure_ascii=False,
                    ),
                )
            ]
        )
        plan = ManagerPlan(
            mode=InteractionMode.TASK,
            reply="과제를 확인합니다.",
            conversation_summary="인공지능 1번 과제 상세 요청.",
            tasks=[
                ManagerTask(
                    agent=ExecutionTargetName.ECLASS,
                    capability=CapabilityCode.ECLASS_QUERY,
                    entity=ManagerEntityKind.ASSIGNMENT,
                    action=ManagerAction.DETAIL,
                    slots=ManagerTaskSlots(
                        year=2026,
                        semester=1,
                        course_query="인공지능",
                        ordinal=1,
                    ),
                    instruction="인공지능 1번 과제의 상세 내용을 조회한다.",
                )
            ],
            reason="다른 강좌의 과제 상세 조회다.",
        )
        event = RuntimeEvent(
            event_type=RuntimeEventType.USER_REQUEST,
            payload={"user_message": "인공지능 1번 과제 세부 내용 알려줘"},
        )

        enriched = runtime._attach_verified_assignment_target(plan, event)

        self.assertIsNone(enriched.tasks[0].verified_assignment_target)

    async def test_credentials_are_redacted_from_recent_conversation(self) -> None:
        """환경설정의 계정과 비밀번호는 대화 문맥에 평문으로 남지 않는다."""

        settings = Settings(
            openai_api_key="test-key",
            eclass_username="student-number",
            eclass_password="secret-password",
        )
        runtime = ProactiveAssistantRuntime(settings)
        plan = ManagerPlan(
            mode=InteractionMode.CHAT,
            reply="인증정보는 대화에 남기지 않겠습니다.",
            conversation_summary="민감정보가 포함된 요청을 마스킹했다.",
            tasks=[],
            reason="보안 안내다.",
        )

        with patch("app.runtime.assistant_runtime.create_plan", new=AsyncMock(return_value=plan)):
            await runtime.handle_user_request(
                "아이디 student-number 비밀번호=secret-password token=abc123"
            )

        stored = " ".join(turn.content for turn in runtime.context.recent_turns)
        self.assertNotIn("student-number", stored)
        self.assertNotIn("secret-password", stored)
        self.assertNotIn("abc123", stored)
        # 8단계 Input Guardrail은 마스킹 문자열조차 Agent 문맥에 넣지 않고 요청 전체를 차단한다.
        self.assertEqual(stored, "")

    async def test_manager_plan_is_retried_once_after_temporary_failure(self) -> None:
        """일시적인 API·구조화 출력 오류 한 번은 사용자 오류창으로 바로 보내지 않는다."""

        runtime = ProactiveAssistantRuntime(self.settings)
        plan = ManagerPlan(
            mode=InteractionMode.CHAT,
            reply="재시도 후 응답했습니다.",
            conversation_summary="Manager 재시도 성공.",
            tasks=[],
            reason="일반 대화다.",
        )
        planner = AsyncMock(side_effect=[RuntimeError("temporary"), plan])

        with patch("app.runtime.assistant_runtime.create_plan", new=planner):
            result = await runtime.handle_user_request("안녕")

        self.assertEqual(result.status, ManagerStatus.COMPLETED)
        self.assertEqual(result.message, "재시도 후 응답했습니다.")
        self.assertEqual(planner.await_count, 2)
        self.assertIsNone(planner.await_args_list[1].kwargs["on_text_delta"])

    async def test_eclass_request_reaches_eclass_agent_boundary(self) -> None:
        handler = AsyncMock(
            return_value=SpecialistResult(
                status=SpecialistStatus.CAPABILITY_NOT_READY,
                summary="테스트 경계입니다.",
            )
        )
        runtime = ProactiveAssistantRuntime(
            self.settings,
            specialist_handlers={SpecialistAgentName.ECLASS: handler},
        )
        plan = ManagerPlan(
            mode=InteractionMode.TASK,
            reply="과제를 확인하겠습니다.",
            conversation_summary="사용자가 이번 주 과제를 요청했다.",
            tasks=[
                ManagerTask(
                    agent=SpecialistAgentName.ECLASS,
                    capability=CapabilityCode.ECLASS_QUERY,
                    instruction="이번 주 과제를 조회한다.",
                )
            ],
            reason="E-Class 조회가 필요하다.",
        )

        with patch("app.runtime.assistant_runtime.create_plan", new=AsyncMock(return_value=plan)):
            result = await runtime.handle_user_request("이번 주 과제 알려줘")

        self.assertEqual(result.status, ManagerStatus.CAPABILITY_NOT_READY)
        self.assertEqual(result.delegated_agents, [SpecialistAgentName.ECLASS])
        self.assertNotIn("과제가 없습니다", result.message)
        handler.assert_awaited_once()

    def test_default_runtime_connects_real_eclass_mcp_handler(self) -> None:
        runtime = ProactiveAssistantRuntime(self.settings)

        self.assertIsInstance(
            runtime._specialist_handlers[SpecialistAgentName.ECLASS],
            EclassMcpSpecialistHandler,
        )

    async def test_verified_specialist_context_is_kept_for_follow_up(self) -> None:
        """생략형 후속 요청을 위해 직전 작업 범위와 검증 결과를 안전하게 보존한다."""

        handler = AsyncMock(
            return_value=SpecialistResult(
                status=SpecialistStatus.COMPLETED,
                summary="빅데이터프로그래밍 공지 3건과 게시일을 확인했다.",
                evidence_refs=["announcement:1", "announcement:2", "announcement:3"],
                verified_followup_context=(
                    '{"kind":"verified_announcement_candidates","items":['
                    '{"number":1,"id":"1","title":"정확한 공지","url":"https://example/1"}]}'
                ),
            )
        )
        runtime = ProactiveAssistantRuntime(
            self.settings,
            specialist_handlers={SpecialistAgentName.ECLASS: handler},
        )
        plan = ManagerPlan(
            mode=InteractionMode.TASK,
            reply="공지를 확인하겠습니다.",
            conversation_summary="2026년 1학기 빅데이터프로그래밍 공지 조회 요청.",
            tasks=[
                ManagerTask(
                    agent=SpecialistAgentName.ECLASS,
                    capability=CapabilityCode.ECLASS_QUERY,
                    instruction="2026년 1학기 빅데이터프로그래밍 공지를 조회한다.",
                )
            ],
            reason="실제 E-Class 공지 조회가 필요하다.",
        )
        with patch(
            "app.runtime.assistant_runtime.create_plan",
            new=AsyncMock(return_value=plan),
        ):
            await runtime.handle_user_request("2026년 1학기 빅데이터프로그래밍 공지 알려줘")

        self.assertIn("2026년 1학기", runtime.context.last_specialist_scope)
        self.assertIn("verified_announcement_candidates", runtime.context.last_verified_result_summary)
        self.assertIn("정확한 공지", runtime.context.last_verified_result_summary)
        self.assertEqual(runtime.context.last_specialist_agents, [SpecialistAgentName.ECLASS.value])
        self.assertEqual(len(runtime.context.verified_entity_snapshots), 1)
        self.assertIs(
            runtime.context.verified_entity_snapshots[0].kind,
            VerifiedEntityKind.ANNOUNCEMENT,
        )

    async def test_verified_notice_body_bypasses_llm_resynthesis(self) -> None:
        """MCP에서 캡처한 공지 본문은 Manager 모델이 다시 의역하지 않는다."""

        exact_body = "중간 및 최종발표에서 산업체 전문가 또는 교수자가 제시한 피드백"
        handler = AsyncMock(
            return_value=SpecialistResult(
                status=SpecialistStatus.COMPLETED,
                summary="모델이 잘못 바꾼 요약",
                evidence_refs=["announcement:532941"],
                verified_display_text=exact_body,
            )
        )
        runtime = ProactiveAssistantRuntime(
            self.settings,
            specialist_handlers={SpecialistAgentName.ECLASS: handler},
        )
        plan = ManagerPlan(
            mode=InteractionMode.TASK,
            reply="공지 본문을 확인합니다.",
            conversation_summary="공지 상세 본문 요청.",
            tasks=[
                ManagerTask(
                    agent=SpecialistAgentName.ECLASS,
                    capability=CapabilityCode.ECLASS_QUERY,
                    instruction="공지 목록이 아니라 상세 본문을 조회한다.",
                )
            ],
            reason="E-Class 상세 Tool이 필요하다.",
        )

        with patch(
            "app.runtime.assistant_runtime.create_plan",
            new=AsyncMock(return_value=plan),
        ):
            result = await runtime.handle_user_request("공지 글 내용 알려줘")

        self.assertEqual(result.message, exact_body)

    async def test_document_result_keeps_prior_verified_download_reference(self) -> None:
        handler = AsyncMock(
            return_value=SpecialistResult(
                status=SpecialistStatus.COMPLETED,
                summary="문서 분석 완료",
                evidence_refs=["document:attachment-1:sha256"],
                verified_display_text="문서 분석 완료",
            )
        )
        runtime = ProactiveAssistantRuntime(
            self.settings,
            specialist_handlers={ExecutionTargetName.DOCUMENT: handler},
        )
        download_ref = "download:00000000-0000-0000-0000-000000000001:attachment-1"
        runtime.context.last_verified_entity_refs = [download_ref]
        plan = ManagerPlan(
            mode=InteractionMode.TASK,
            reply="문서를 분석합니다.",
            conversation_summary="검증 문서 분석 요청.",
            tasks=[
                ManagerTask(
                    agent=ExecutionTargetName.DOCUMENT,
                    capability=CapabilityCode.DOCUMENT_ANALYSIS,
                    instruction="직전에 검증한 문서를 다시 분석한다.",
                )
            ],
            reason="검증된 다운로드가 있다.",
        )

        with patch(
            "app.runtime.assistant_runtime.create_plan",
            new=AsyncMock(return_value=plan),
        ):
            result = await runtime.handle_user_request("그 파일 다시 분석해줘")

        self.assertEqual(result.status, ManagerStatus.COMPLETED)
        delegated_task = handler.await_args.args[0]
        self.assertEqual(delegated_task.verified_input_refs, [download_ref])
        self.assertTrue(delegated_task.reuse_latest_verified_download)
        self.assertIsNone(delegated_task.verified_attachment_id)
        self.assertNotIn("download:", delegated_task.instruction)
        self.assertIn(download_ref, runtime.context.last_verified_entity_refs)
        self.assertIn("document:attachment-1:sha256", runtime.context.last_verified_entity_refs)

    async def test_explicit_missing_attachment_never_reuses_old_download(self) -> None:
        """없는 파일명을 명시하면 Manager가 심은 binding도 지우고 과거 파일을 분석하지 않는다."""

        old_ref = "download:00000000-0000-0000-0000-000000000001:attachment-old"
        document_handler = AsyncMock()
        runtime = ProactiveAssistantRuntime(
            self.settings,
            specialist_handlers={ExecutionTargetName.DOCUMENT: document_handler},
        )
        runtime.context.last_verified_entity_refs = [old_ref]
        runtime._remember_verified_snapshots(
            [
                SpecialistResult(
                    status=SpecialistStatus.COMPLETED,
                    summary="첨부 목록",
                    verified_followup_context=json.dumps(
                        {
                            "kind": "verified_attachment_candidates",
                            "items": [
                                {
                                    "number": 1,
                                    "id": "attachment-real",
                                    "parent_id": "assignment-1",
                                    "name": "real.pdf",
                                    "url": "https://example/real.pdf",
                                }
                            ],
                        }
                    ),
                )
            ]
        )
        plan = ManagerPlan(
            mode=InteractionMode.TASK,
            reply="문서를 분석합니다.",
            conversation_summary="missing.txt 분석 요청.",
            tasks=[
                ManagerTask(
                    agent=ExecutionTargetName.DOCUMENT,
                    capability=CapabilityCode.DOCUMENT_ANALYSIS,
                    entity=ManagerEntityKind.DOCUMENT,
                    action=ManagerAction.ANALYZE,
                    # Manager가 query를 누락해도 사용자 원문의 일반 확장자 파일명을 강제 선택자로 본다.
                    slots=ManagerTaskSlots(),
                    instruction="missing.txt를 분석한다.",
                    # 모델이 Runtime 전용 필드를 위조해도 요청 시작 시 반드시 폐기되어야 한다.
                    verified_attachment_id="attachment-old",
                    reuse_latest_verified_download=True,
                )
            ],
            reason="명시 파일 분석 요청.",
        )

        with patch("app.runtime.assistant_runtime.create_plan", new=AsyncMock(return_value=plan)):
            result = await runtime.handle_user_request("missing.txt 분석해줘")

        self.assertEqual(result.status, ManagerStatus.FAILED)
        document_handler.assert_not_awaited()

    async def test_user_filename_wins_over_conflicting_manager_query(self) -> None:
        """사용자가 쓴 없는 파일명을 Manager가 현재의 다른 파일로 바꿔도 대체 분석하지 않는다."""

        eclass_handler = AsyncMock()
        document_handler = AsyncMock()
        runtime = ProactiveAssistantRuntime(
            self.settings,
            specialist_handlers={
                ExecutionTargetName.ECLASS: eclass_handler,
                ExecutionTargetName.DOCUMENT: document_handler,
            },
        )
        runtime._remember_verified_snapshots(
            [
                SpecialistResult(
                    status=SpecialistStatus.COMPLETED,
                    summary="첨부 목록",
                    verified_followup_context=json.dumps(
                        {
                            "kind": "verified_attachment_candidates",
                            "items": [
                                {
                                    "number": 1,
                                    "id": "attachment-real",
                                    "parent_id": "assignment-1",
                                    "name": "real.pdf",
                                    "url": "https://example/real.pdf",
                                }
                            ],
                        }
                    ),
                )
            ]
        )
        plan = ManagerPlan(
            mode=InteractionMode.TASK,
            reply="문서를 분석합니다.",
            conversation_summary="명시 파일 분석 요청.",
            tasks=[
                ManagerTask(
                    agent=ExecutionTargetName.DOCUMENT,
                    capability=CapabilityCode.DOCUMENT_ANALYSIS,
                    entity=ManagerEntityKind.DOCUMENT,
                    action=ManagerAction.ANALYZE,
                    # 모델이 현재 후보의 이름을 잘못 넣어도 사용자 원문보다 우선할 수 없다.
                    slots=ManagerTaskSlots(query="real.pdf"),
                    instruction="real.pdf를 분석한다.",
                )
            ],
            reason="Manager query 충돌 회귀 테스트.",
        )

        with patch("app.runtime.assistant_runtime.create_plan", new=AsyncMock(return_value=plan)):
            result = await runtime.handle_user_request("missing.pdf 분석해줘")

        self.assertEqual(result.status, ManagerStatus.FAILED)
        eclass_handler.assert_not_awaited()
        document_handler.assert_not_awaited()

    async def test_selected_attachment_binding_filters_document_download_ref(self) -> None:
        """현재 snapshot에서 고른 첨부 ID가 다운로드와 Document 단계 끝까지 유지된다."""

        old_ref = "download:00000000-0000-0000-0000-000000000001:attachment-old"
        selected_ref = "download:11111111-1111-1111-1111-111111111111:attachment-selected"
        eclass_handler = AsyncMock(
            return_value=SpecialistResult(
                status=SpecialistStatus.COMPLETED,
                summary="선택 첨부 다운로드 완료",
                evidence_refs=[selected_ref],
            )
        )
        document_handler = AsyncMock(
            return_value=SpecialistResult(
                status=SpecialistStatus.COMPLETED,
                summary="선택 문서 분석 완료",
            )
        )
        runtime = ProactiveAssistantRuntime(
            self.settings,
            specialist_handlers={
                ExecutionTargetName.ECLASS: eclass_handler,
                ExecutionTargetName.DOCUMENT: document_handler,
            },
        )
        runtime.context.last_verified_entity_refs = [old_ref]
        runtime._remember_verified_snapshots(
            [
                SpecialistResult(
                    status=SpecialistStatus.COMPLETED,
                    summary="첨부 목록",
                    verified_followup_context=json.dumps(
                        {
                            "kind": "verified_attachment_candidates",
                            "items": [
                                {
                                    "number": 1,
                                    "id": "attachment-selected",
                                    "parent_id": "assignment-1",
                                    "name": "selected.pdf",
                                    "url": "https://example/selected.pdf",
                                },
                                {
                                    "number": 2,
                                    "id": "attachment-other",
                                    "parent_id": "assignment-1",
                                    "name": "other.pdf",
                                    "url": "https://example/other.pdf",
                                },
                            ],
                        }
                    ),
                )
            ]
        )
        plan = ManagerPlan(
            mode=InteractionMode.TASK,
            reply="선택한 문서를 분석합니다.",
            conversation_summary="selected.pdf 분석 요청.",
            tasks=[
                ManagerTask(
                    agent=ExecutionTargetName.DOCUMENT,
                    capability=CapabilityCode.DOCUMENT_ANALYSIS,
                    entity=ManagerEntityKind.DOCUMENT,
                    action=ManagerAction.ANALYZE,
                    slots=ManagerTaskSlots(query="selected.pdf"),
                    instruction="selected.pdf를 분석한다.",
                )
            ],
            reason="현재 첨부 snapshot에서 하나를 선택했다.",
        )

        with patch("app.runtime.assistant_runtime.create_plan", new=AsyncMock(return_value=plan)):
            result = await runtime.handle_user_request("selected.pdf 분석해줘")

        self.assertEqual(result.status, ManagerStatus.COMPLETED)
        download_task = eclass_handler.await_args.args[0]
        delegated_document_task = document_handler.await_args.args[0]
        self.assertEqual(download_task.verified_attachment_target.id, "attachment-selected")
        self.assertEqual(
            delegated_document_task.verified_attachment_id,
            "attachment-selected",
        )
        self.assertEqual(delegated_document_task.verified_input_refs, [selected_ref])
        self.assertNotIn(old_ref, delegated_document_task.verified_input_refs)

    async def test_eclass_download_flows_to_document_as_typed_runtime_ref(self) -> None:
        """복합 계획은 E-Class의 실제 다운로드 결과만 다음 Document 단계에 넘긴다."""

        download_ref = "download:11111111-1111-1111-1111-111111111111:attachment-1"
        model_supplied_ref = "download:99999999-9999-9999-9999-999999999999:forged"
        eclass_handler = AsyncMock(
            return_value=SpecialistResult(
                status=SpecialistStatus.COMPLETED,
                summary="첨부 다운로드 완료",
                evidence_refs=[download_ref],
            )
        )
        document_handler = AsyncMock(
            return_value=SpecialistResult(
                status=SpecialistStatus.COMPLETED,
                summary="문서 분석 완료",
                evidence_refs=["document:attachment-1:sha256"],
            )
        )
        runtime = ProactiveAssistantRuntime(
            self.settings,
            specialist_handlers={
                ExecutionTargetName.ECLASS: eclass_handler,
                ExecutionTargetName.DOCUMENT: document_handler,
            },
        )
        plan = ManagerPlan(
            mode=InteractionMode.TASK,
            reply="첨부를 내려받아 분석합니다.",
            conversation_summary="첨부 다운로드 후 분석 요청.",
            tasks=[
                ManagerTask(
                    agent=ExecutionTargetName.ECLASS,
                    capability=CapabilityCode.ECLASS_QUERY,
                    entity=ManagerEntityKind.ATTACHMENT,
                    action=ManagerAction.DOWNLOAD,
                    slots=ManagerTaskSlots(),
                    instruction="선택된 첨부를 다운로드한다.",
                ),
                ManagerTask(
                    agent=ExecutionTargetName.DOCUMENT,
                    capability=CapabilityCode.DOCUMENT_ANALYSIS,
                    entity=ManagerEntityKind.DOCUMENT,
                    action=ManagerAction.ANALYZE,
                    slots=ManagerTaskSlots(),
                    instruction="방금 다운로드한 문서를 분석한다.",
                    verified_input_refs=[model_supplied_ref],
                ),
            ],
            reason="순차 실행이 필요하다.",
        )

        with patch("app.runtime.assistant_runtime.create_plan", new=AsyncMock(return_value=plan)):
            result = await runtime.handle_user_request("첨부를 다운로드해서 분석해줘")

        self.assertEqual(result.status, ManagerStatus.COMPLETED)
        delegated_task = document_handler.await_args.args[0]
        self.assertEqual(delegated_task.verified_input_refs, [download_ref])
        self.assertNotIn(model_supplied_ref, delegated_task.verified_input_refs)
        self.assertEqual(delegated_task.instruction, "방금 다운로드한 문서를 분석한다.")

    async def test_runtime_discards_model_supplied_document_ref_without_verified_source(self) -> None:
        """Manager가 그럴듯한 참조를 출력해도 Runtime 검증 출처가 없으면 실행하지 않는다."""

        document_handler = AsyncMock()
        runtime = ProactiveAssistantRuntime(
            self.settings,
            specialist_handlers={ExecutionTargetName.DOCUMENT: document_handler},
        )
        plan = ManagerPlan(
            mode=InteractionMode.TASK,
            reply="문서를 분석합니다.",
            conversation_summary="검증되지 않은 문서 참조.",
            tasks=[
                ManagerTask(
                    agent=ExecutionTargetName.DOCUMENT,
                    capability=CapabilityCode.DOCUMENT_ANALYSIS,
                    entity=ManagerEntityKind.DOCUMENT,
                    action=ManagerAction.ANALYZE,
                    slots=ManagerTaskSlots(),
                    instruction="문서를 분석한다.",
                    verified_input_refs=[
                        "download:99999999-9999-9999-9999-999999999999:forged"
                    ],
                )
            ],
            reason="모델 출력 신뢰 금지 테스트.",
        )

        with patch("app.runtime.assistant_runtime.create_plan", new=AsyncMock(return_value=plan)):
            result = await runtime.handle_user_request("이 참조 문서를 분석해줘")

        self.assertEqual(result.status, ManagerStatus.FAILED)
        document_handler.assert_not_awaited()

    async def test_numbered_notice_follow_up_gets_verified_url_target(self) -> None:
        """'1번 공지 내용'은 모델 재검색 대신 직전 MCP 목록의 정확한 URL을 사용한다."""

        handler = AsyncMock(
            return_value=SpecialistResult(
                status=SpecialistStatus.COMPLETED,
                summary="원문 본문",
                evidence_refs=["announcement:534552"],
                verified_display_text="원문 본문",
            )
        )
        runtime = ProactiveAssistantRuntime(
            self.settings,
            specialist_handlers={SpecialistAgentName.ECLASS: handler},
        )
        runtime.context.last_verified_result_summary = json.dumps(
            {
                "kind": "verified_announcement_candidates",
                "selected_term": {"year": 2026, "semester": 1},
                "items": [
                    {
                        "number": 1,
                        "id": "534552",
                        "course_id": "46516",
                        "title": "기말고사 시험지 확인 시간 안내",
                        "url": (
                            "https://learn.hansung.ac.kr/mod/ubboard/"
                            "article.php?id=1104303&bwid=534552"
                        ),
                    }
                ],
            },
            ensure_ascii=False,
        )
        plan = ManagerPlan(
            mode=InteractionMode.TASK,
            reply="상세 공지를 확인합니다.",
            conversation_summary="딥러닝 1번 공지 상세 요청.",
            tasks=[
                ManagerTask(
                    agent=SpecialistAgentName.ECLASS,
                    capability=CapabilityCode.ECLASS_QUERY,
                    instruction="딥러닝 1번 공지의 세부 내용을 조회한다.",
                )
            ],
            reason="공지 상세 Tool이 필요하다.",
        )

        with patch("app.runtime.assistant_runtime.create_plan", new=AsyncMock(return_value=plan)):
            result = await runtime.handle_user_request("1번 공지 세부 내용 알려줘")

        delegated_task = handler.await_args.args[0]
        target = delegated_task.verified_announcement_target
        self.assertIsNotNone(target)
        self.assertEqual(target.id, "534552")
        self.assertEqual(target.course_id, "46516")
        self.assertIn("bwid=534552", target.url)
        self.assertEqual(result.message, "원문 본문")

    async def test_pdf_follow_up_inserts_verified_download_before_document_analysis(self) -> None:
        """첨부 목록 다음의 `pdf 내용` 요청은 다운로드 참조 없이 Document로 건너뛰지 않는다."""

        runtime = ProactiveAssistantRuntime(self.settings)
        runtime.context.last_verified_result_summary = json.dumps(
            {
                "kind": "verified_attachment_candidates",
                "items": [
                    {
                        "number": 1,
                        "id": "attachment-pdf",
                        "parent_id": "1140975",
                        "name": "BPM_2026-1_lab1.pdf",
                        "url": "https://learn.hansung.ac.kr/pluginfile.php/1/BPM_2026-1_lab1.pdf",
                        "mime_type": "application/pdf",
                    },
                    {
                        "number": 2,
                        "id": "attachment-zip",
                        "parent_id": "1140975",
                        "name": "실습1 샘플이미지.zip",
                        "url": "https://learn.hansung.ac.kr/pluginfile.php/1/sample.zip",
                        "mime_type": "application/zip",
                    },
                ],
            },
            ensure_ascii=False,
        )
        plan = ManagerPlan(
            mode=InteractionMode.TASK,
            reply="PDF를 분석합니다.",
            conversation_summary="직전 과제의 PDF 내용 요청.",
            tasks=[
                ManagerTask(
                    agent=SpecialistAgentName.DOCUMENT,
                    capability=CapabilityCode.DOCUMENT_ANALYSIS,
                    instruction="PDF 내용을 분석한다.",
                )
            ],
            reason="문서 분석 요청이다.",
        )
        event = RuntimeEvent(
            event_type=RuntimeEventType.USER_REQUEST,
            payload={"user_message": "pdf 내용 알려줘"},
        )

        enriched = runtime._attach_verified_attachment_target(plan, event)

        self.assertEqual(
            [task.agent for task in enriched.tasks],
            [SpecialistAgentName.ECLASS, SpecialistAgentName.DOCUMENT],
        )
        target = enriched.tasks[0].verified_attachment_target
        self.assertIsNotNone(target)
        self.assertEqual(target.name, "BPM_2026-1_lab1.pdf")

    async def test_first_assignment_follow_up_gets_verified_assignment_id(self) -> None:
        handler = AsyncMock(
            return_value=SpecialistResult(
                status=SpecialistStatus.COMPLETED,
                summary="과제 상세 원문",
                evidence_refs=["assignment:1001"],
                verified_display_text="과제 상세 원문",
            )
        )
        runtime = ProactiveAssistantRuntime(
            self.settings,
            specialist_handlers={SpecialistAgentName.ECLASS: handler},
        )
        runtime.context.last_verified_result_summary = json.dumps(
            {
                "kind": "verified_assignment_candidates",
                "selected_term": {"year": 2026, "semester": 1},
                "items": [
                    {
                        "number": 1,
                        "id": "1001",
                        "course_id": "46499",
                        "course_name": "데이터마이닝[A,B]",
                        "title": "실습 1: KNN 알고리즘",
                        "url": "https://learn.hansung.ac.kr/mod/assign/view.php?id=1001",
                    },
                    {
                        "number": 2,
                        "id": "1002",
                        "course_id": "46499",
                        "course_name": "데이터마이닝[A,B]",
                        "title": "실습 2: 리뷰 유용성 분류",
                        "url": "https://learn.hansung.ac.kr/mod/assign/view.php?id=1002",
                    },
                ],
            },
            ensure_ascii=False,
        )
        plan = ManagerPlan(
            mode=InteractionMode.TASK,
            reply="첫 번째 과제를 확인합니다.",
            conversation_summary="직전 목록 첫 번째 과제 상세 요청.",
            tasks=[
                ManagerTask(
                    agent=SpecialistAgentName.ECLASS,
                    capability=CapabilityCode.ECLASS_QUERY,
                    instruction="첫 번째 과제의 상세 내용을 조회한다.",
                )
            ],
            reason="과제 상세 조회가 필요하다.",
        )

        with patch("app.runtime.assistant_runtime.create_plan", new=AsyncMock(return_value=plan)):
            result = await runtime.handle_user_request("첫번째 과제 자세히 알려줘")

        delegated_task = handler.await_args.args[0]
        target = delegated_task.verified_assignment_target
        self.assertIsNotNone(target)
        self.assertEqual(target.id, "1001")
        self.assertEqual(target.title, "실습 1: KNN 알고리즘")
        self.assertEqual(result.message, "과제 상세 원문")

    async def test_lecture_follow_up_uses_real_lecture_id_not_course_id(self) -> None:
        playback_id = "00000000-0000-0000-0000-000000000070"
        handler = AsyncMock(
            return_value=SpecialistResult(
                status=SpecialistStatus.COMPLETED,
                summary="강의 영상 재생을 시작했습니다.",
                evidence_refs=["lecture:1133557", f"playback:{playback_id}"],
                verified_display_text="강의 영상 재생을 시작했습니다.",
            )
        )
        runtime = ProactiveAssistantRuntime(
            self.settings,
            specialist_handlers={SpecialistAgentName.ECLASS: handler},
        )
        runtime.context.last_verified_result_summary = json.dumps(
            {
                "kind": "verified_lecture_candidates",
                "selected_term": {"year": 2026, "semester": 1},
                "items": [
                    {
                        "number": 1,
                        "id": "1133557",
                        "course_id": "46499",
                        "course_name": "데이터마이닝[A,B]",
                        "title": "[동영상] 02주차_Python 개요 및 가상환경 구축",
                        "url": "https://learn.hansung.ac.kr/mod/vod/view.php?id=1133557",
                        "week": 2,
                    }
                ],
            },
            ensure_ascii=False,
        )
        plan = ManagerPlan(
            mode=InteractionMode.TASK,
            reply="직전 강의를 재생합니다.",
            conversation_summary="직전 2주차 강의 재생 요청.",
            tasks=[
                ManagerTask(
                    agent=SpecialistAgentName.ECLASS,
                    capability=CapabilityCode.VIDEO_PLAY,
                    instruction="직전에 확인한 2주차 강의 영상을 재생한다.",
                )
            ],
            reason="명시적인 영상 재생 요청이다.",
        )

        with patch("app.runtime.assistant_runtime.create_plan", new=AsyncMock(return_value=plan)):
            result = await runtime.handle_user_request("그거 재생해봐")

        delegated_task = handler.await_args.args[0]
        target = delegated_task.verified_lecture_target
        self.assertIsNotNone(target)
        self.assertEqual(target.id, "1133557")
        self.assertNotEqual(target.id, target.course_id)
        self.assertEqual(target.course_id, "46499")
        self.assertEqual(result.message, "강의 영상 재생을 시작했습니다.")
        self.assertIn(f"playback:{playback_id}", runtime._active_playback_refs)

    async def test_lecture_week_with_multiple_videos_is_not_selected_arbitrarily(self) -> None:
        runtime = ProactiveAssistantRuntime(self.settings)
        runtime.context.last_verified_result_summary = json.dumps(
            {
                "kind": "verified_lecture_candidates",
                "selected_term": {"year": 2026, "semester": 1},
                "items": [
                    {
                        "number": 1,
                        "id": "1133557",
                        "course_id": "46499",
                        "title": "2주차 영상 1",
                        "url": "https://learn.hansung.ac.kr/mod/vod/view.php?id=1133557",
                        "week": 2,
                    },
                    {
                        "number": 2,
                        "id": "1133558",
                        "course_id": "46499",
                        "title": "2주차 영상 2",
                        "url": "https://learn.hansung.ac.kr/mod/vod/view.php?id=1133558",
                        "week": 2,
                    },
                ],
            },
            ensure_ascii=False,
        )
        plan = ManagerPlan(
            mode=InteractionMode.TASK,
            reply="2주차 강의를 재생합니다.",
            conversation_summary="2주차 강의 재생 요청.",
            tasks=[
                ManagerTask(
                    agent=SpecialistAgentName.ECLASS,
                    capability=CapabilityCode.VIDEO_PLAY,
                    instruction="2주차 강의 영상을 재생한다.",
                )
            ],
            reason="영상 재생 요청이다.",
        )
        event = RuntimeEvent(
            event_type=RuntimeEventType.USER_REQUEST,
            payload={"user_message": "2주차 영상 재생해봐", "explicit_playback_request": True},
        )

        enriched = runtime._attach_verified_lecture_target(plan, event)

        self.assertIsNone(enriched.tasks[0].verified_lecture_target)

    async def test_compound_request_runs_specialists_in_order(self) -> None:
        calls: list[SpecialistAgentName] = []

        def make_handler(agent: SpecialistAgentName):
            async def handler(_task: ManagerTask) -> SpecialistResult:
                calls.append(agent)
                evidence_refs = (
                    ["download:22222222-2222-2222-2222-222222222222:attachment-compound"]
                    if agent is ExecutionTargetName.ECLASS
                    else [f"verified:{agent.value}"]
                )
                return SpecialistResult(
                    status=SpecialistStatus.COMPLETED,
                    summary=f"{agent.value} 단계 완료",
                    evidence_refs=evidence_refs,
                )

            return handler

        runtime = ProactiveAssistantRuntime(
            self.settings,
            specialist_handlers={agent: make_handler(agent) for agent in SpecialistAgentName},
        )
        plan = ManagerPlan(
            mode=InteractionMode.TASK,
            reply="문서를 확인해 미션으로 정리하겠습니다.",
            conversation_summary="과제 문서 분석과 미션 생성을 요청했다.",
            tasks=[
                ManagerTask(
                    agent=SpecialistAgentName.ECLASS,
                    capability=CapabilityCode.ECLASS_QUERY,
                    instruction="과제와 첨부파일을 확인한다.",
                ),
                ManagerTask(
                    agent=SpecialistAgentName.DOCUMENT,
                    capability=CapabilityCode.DOCUMENT_ANALYSIS,
                    instruction="확인된 첨부 문서를 분석한다.",
                ),
                ManagerTask(
                    agent=ExecutionTargetName.MISSION_SERVICE,
                    capability=CapabilityCode.MISSION_MANAGEMENT,
                    instruction="검증된 결과를 미션으로 정리한다.",
                ),
            ],
            reason="세 전문 단계가 순서대로 필요하다.",
        )

        with patch(
            "app.runtime.assistant_runtime.create_plan",
            new=AsyncMock(return_value=plan),
        ):
            result = await runtime.handle_user_request("과제 문서를 분석해서 미션으로 만들어줘")

        self.assertEqual(calls, list(SpecialistAgentName))
        self.assertEqual(result.status, ManagerStatus.COMPLETED)
        self.assertEqual(result.delegated_agents, list(SpecialistAgentName))
        self.assertIn("[E-Class Agent]", result.message)
        self.assertIn("[Mission Service]", result.message)

    async def test_same_operation_with_different_slots_is_not_treated_as_duplicate(self) -> None:
        """서로 다른 두 강좌 조회는 entity/action이 같아도 각각 실행한다."""

        handler = AsyncMock(
            side_effect=[
                SpecialistResult(status=SpecialistStatus.COMPLETED, summary="A 과제"),
                SpecialistResult(status=SpecialistStatus.COMPLETED, summary="B 과제"),
            ]
        )
        runtime = ProactiveAssistantRuntime(
            self.settings,
            specialist_handlers={ExecutionTargetName.ECLASS: handler},
        )
        plan = ManagerPlan(
            mode=InteractionMode.TASK,
            reply="두 강좌 과제를 확인합니다.",
            conversation_summary="서로 다른 두 강좌의 과제 요청.",
            tasks=[
                ManagerTask(
                    agent=ExecutionTargetName.ECLASS,
                    capability=CapabilityCode.ECLASS_QUERY,
                    entity=ManagerEntityKind.ASSIGNMENT,
                    action=ManagerAction.LIST,
                    slots=ManagerTaskSlots(course_query="강좌 A"),
                    instruction="강좌 A 과제를 조회한다.",
                ),
                ManagerTask(
                    agent=ExecutionTargetName.ECLASS,
                    capability=CapabilityCode.ECLASS_QUERY,
                    entity=ManagerEntityKind.ASSIGNMENT,
                    action=ManagerAction.LIST,
                    slots=ManagerTaskSlots(course_query="강좌 B"),
                    instruction="강좌 B 과제를 조회한다.",
                ),
            ],
            reason="두 범위가 다르므로 두 단계가 필요하다.",
        )

        with patch(
            "app.runtime.assistant_runtime.create_plan",
            new=AsyncMock(return_value=plan),
        ):
            result = await runtime.handle_user_request("강좌 A와 강좌 B 과제를 각각 알려줘")

        self.assertEqual(result.status, ManagerStatus.COMPLETED)
        self.assertEqual(handler.await_count, 2)

    async def test_identical_operation_and_slots_is_still_blocked_as_duplicate(self) -> None:
        """완전히 같은 단계의 반복은 무한 실행 방지 규칙으로 계속 차단한다."""

        handler = AsyncMock(
            return_value=SpecialistResult(
                status=SpecialistStatus.COMPLETED,
                summary="과제 목록",
            )
        )
        runtime = ProactiveAssistantRuntime(
            self.settings,
            specialist_handlers={ExecutionTargetName.ECLASS: handler},
        )
        task = ManagerTask(
            agent=ExecutionTargetName.ECLASS,
            capability=CapabilityCode.ECLASS_QUERY,
            entity=ManagerEntityKind.ASSIGNMENT,
            action=ManagerAction.LIST,
            slots=ManagerTaskSlots(course_query="강좌 A"),
            instruction="강좌 A 과제를 조회한다.",
        )
        plan = ManagerPlan(
            mode=InteractionMode.TASK,
            reply="과제를 확인합니다.",
            conversation_summary="같은 과제 조회가 중복된 계획.",
            tasks=[task, task.model_copy(deep=True)],
            reason="중복 방지 테스트다.",
        )

        with patch(
            "app.runtime.assistant_runtime.create_plan",
            new=AsyncMock(return_value=plan),
        ):
            result = await runtime.handle_user_request("강좌 A 과제를 알려줘")

        self.assertEqual(result.status, ManagerStatus.FAILED)
        self.assertEqual(result.error_code, ErrorCode.WORKFLOW_LIMIT_REACHED)
        self.assertEqual(handler.await_count, 1)

    async def test_system_chat_plan_becomes_silent_no_action(self) -> None:
        runtime = ProactiveAssistantRuntime(self.settings)
        event = RuntimeEvent(event_type=RuntimeEventType.STARTUP_BRIEFING, payload={"change_count": 0})
        plan = ManagerPlan(
            mode=InteractionMode.CHAT,
            reply="새로 알릴 내용이 없습니다.",
            conversation_summary="시작 동기화 변경 없음.",
            tasks=[],
            reason="변경이 없다.",
        )

        with patch("app.runtime.assistant_runtime.create_plan", new=AsyncMock(return_value=plan)):
            result = await runtime.handle_system_event(event)

        self.assertEqual(result.status, ManagerStatus.NO_ACTION)
        self.assertFalse(result.should_notify)

    async def test_verified_lms_change_chat_plan_becomes_proactive_notice(self) -> None:
        runtime = ProactiveAssistantRuntime(self.settings)
        event = RuntimeEvent(event_type=RuntimeEventType.LMS_CHANGED, payload={"change_count": 1})
        plan = ManagerPlan(
            mode=InteractionMode.CHAT,
            reply="새 과제가 확인되었습니다.",
            conversation_summary="E-Class 변경 1건.",
            tasks=[],
            reason="검증된 변경을 알린다.",
        )

        with patch("app.runtime.assistant_runtime.create_plan", new=AsyncMock(return_value=plan)):
            result = await runtime.handle_system_event(event)

        self.assertEqual(result.status, ManagerStatus.COMPLETED)
        self.assertTrue(result.should_notify)

    def test_shared_api_key_error_is_in_common_module(self) -> None:
        self.assertTrue(issubclass(OpenAiApiKeyRequiredError, RuntimeError))

    async def test_verified_playback_stop_bypasses_both_manager_and_agent(self) -> None:
        """F2 중지는 검증 UUID를 자연어 Agent에게 복사시키지 않고 직접 handler에 결박한다."""

        playback_id = "00000000-0000-0000-0000-000000000071"
        playback_ref = f"playback:{playback_id}"
        eclass_handler = AsyncMock()
        eclass_handler.stop_verified_playback = AsyncMock(
            return_value=SpecialistResult(
                status=SpecialistStatus.COMPLETED,
                summary="강의 영상 재생을 중지했습니다.",
                verified_display_text="강의 영상 재생을 중지했습니다.",
                evidence_refs=[playback_ref],
            )
        )
        runtime = ProactiveAssistantRuntime(
            self.settings,
            specialist_handlers={ExecutionTargetName.ECLASS: eclass_handler},
        )
        runtime._active_playback_refs.add(playback_ref)

        with patch(
            "app.runtime.assistant_runtime.create_plan",
            new=AsyncMock(side_effect=AssertionError("Manager를 호출하면 안 됩니다.")),
        ):
            result = await runtime.stop_verified_playback(playback_id)

        self.assertEqual(result.status, ManagerStatus.COMPLETED)
        eclass_handler.stop_verified_playback.assert_awaited_once_with(playback_id)
        eclass_handler.assert_not_awaited()
        self.assertNotIn(playback_ref, runtime._active_playback_refs)

    async def test_playback_stop_rejects_id_not_issued_by_current_runtime(self) -> None:
        """형식이 UUID여도 현재 Runtime의 성공 재생 결과가 아니면 MCP에 전달하지 않는다."""

        eclass_handler = AsyncMock()
        eclass_handler.stop_verified_playback = AsyncMock()
        runtime = ProactiveAssistantRuntime(
            self.settings,
            specialist_handlers={ExecutionTargetName.ECLASS: eclass_handler},
        )

        result = await runtime.stop_verified_playback(
            "00000000-0000-0000-0000-000000000072"
        )

        self.assertEqual(result.status, ManagerStatus.FAILED)
        self.assertEqual(result.error_code, ErrorCode.INVALID_REQUEST)
        eclass_handler.stop_verified_playback.assert_not_awaited()


class RuntimeContractTest(unittest.IsolatedAsyncioTestCase):
    def test_all_required_system_event_types_exist(self) -> None:
        required = {
            "USER_REQUEST",
            "STARTUP_BRIEFING",
            "LMS_CHANGED",
            "DEADLINE_WARNING",
            "ATTENDANCE_WARNING",
            "SESSION_EXPIRED",
        }
        self.assertTrue(required.issubset({event.value for event in RuntimeEventType}))

    def test_system_event_rejects_user_original_and_secrets(self) -> None:
        with self.assertRaises(ValueError):
            RuntimeEvent(
                event_type=RuntimeEventType.LMS_CHANGED,
                payload={"user_message": "원문"},
            )
        with self.assertRaises(ValueError):
            RuntimeEvent(
                event_type=RuntimeEventType.LMS_CHANGED,
                payload={"nested": {"password": "secret"}},
            )

    async def test_runtime_shutdown_blocks_new_requests(self) -> None:
        runtime = ProactiveAssistantRuntime(Settings(openai_api_key="test-key"))
        await runtime.shutdown()
        with self.assertRaises(RuntimeError):
            await runtime.handle_user_request("안녕")

    async def test_event_queue_rejects_duplicate_event_id(self) -> None:
        queue = RuntimeEventQueue()
        event = RuntimeEvent(
            event_id="same-event",
            event_type=RuntimeEventType.LMS_CHANGED,
            payload={"change_count": 1},
        )

        self.assertTrue(await queue.publish(event))
        self.assertFalse(await queue.publish(event))
        await queue.close()

    async def test_document_step_requires_verified_input_or_eclass_first(self) -> None:
        runtime = ProactiveAssistantRuntime(Settings(openai_api_key="test-key"))
        invalid_plan = ManagerPlan(
            mode=InteractionMode.TASK,
            reply="문서를 분석합니다.",
            conversation_summary="문서 분석 요청.",
            tasks=[
                ManagerTask(
                    agent=SpecialistAgentName.DOCUMENT,
                    capability=CapabilityCode.DOCUMENT_ANALYSIS,
                    instruction="확인되지 않은 첨부를 분석한다.",
                )
            ],
            reason="잘못된 의존 계획 테스트.",
        )

        with patch(
            "app.runtime.assistant_runtime.create_plan",
            new=AsyncMock(return_value=invalid_plan),
        ):
            result = await runtime.handle_user_request("과제 첨부를 분석해줘")

        self.assertEqual(result.status, ManagerStatus.FAILED)


if __name__ == "__main__":
    unittest.main()
