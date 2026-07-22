"""과제 첨부파일 후속 요청의 typed 결박 경계를 검증한다.

이 테스트는 Manager 모델의 자연어 판단을 검증하지 않는다. 이미 생성된 ManagerPlan과
MCP가 반환했다고 가정할 수 있는 typed snapshot을 사용해 Runtime이 검증된 ID만 실행
계약에 연결하는지 확인한다.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.agent.document_handler import DocumentSpecialistHandler
from app.agent.eclass_mcp_handler import EclassMcpSpecialistHandler
from app.config import Settings
from app.runtime.assistant_runtime import ProactiveAssistantRuntime
from app.schemas.manager import (
    ExecutionTargetName,
    ManagerAction,
    ManagerEntityKind,
    ManagerPlan,
    ManagerTask,
    ManagerTaskSlots,
    SpecialistResult,
    SpecialistStatus,
    ManagerStatus,
    VerifiedAssignmentTarget,
    VerifiedAttachmentTarget,
)
from app.schemas.domain import Attachment
from app.schemas.runtime import (
    RuntimeEvent,
    RuntimeEventType,
    VerifiedEntityKind,
    VerifiedEntityReference,
    VerifiedEntitySnapshot,
)
from app.schemas.workflow import CapabilityCode, InteractionMode
from mcp_server.schemas import (
    AttachmentListResult,
    DownloadInfo,
    DownloadResult,
    SelectedTerm,
)


class AttachmentFollowupBindingTest(unittest.TestCase):
    """다운로드 전에 Runtime이 수행하는 첨부 대상 결박 회귀 테스트."""

    def setUp(self) -> None:
        self.runtime = ProactiveAssistantRuntime(
            Settings(_env_file=None, openai_api_key="test-key")
        )

    @staticmethod
    def _task(
        *,
        agent: ExecutionTargetName,
        entity: ManagerEntityKind,
        action: ManagerAction,
        slots: ManagerTaskSlots | None = None,
        instruction: str,
    ) -> ManagerTask:
        capability = (
            CapabilityCode.DOCUMENT_ANALYSIS
            if agent is ExecutionTargetName.DOCUMENT
            else CapabilityCode.ECLASS_QUERY
        )
        return ManagerTask(
            agent=agent,
            capability=capability,
            entity=entity,
            action=action,
            slots=slots or ManagerTaskSlots(),
            instruction=instruction,
        )

    @staticmethod
    def _plan(*tasks: ManagerTask) -> ManagerPlan:
        return ManagerPlan(
            mode=InteractionMode.TASK,
            reply="검증된 첨부 요청을 처리합니다.",
            conversation_summary="과제 첨부파일 후속 요청.",
            tasks=list(tasks),
            reason="검증된 E-Class 첨부 대상이 필요하다.",
        )

    @staticmethod
    def _event(message: str) -> RuntimeEvent:
        return RuntimeEvent(
            event_type=RuntimeEventType.USER_REQUEST,
            payload={"user_message": message},
        )

    def test_verified_assignment_target_is_bound_to_attachment_list(self) -> None:
        """첫 과제의 파일 목록은 같은 과제의 실제 ID를 부모 대상으로 사용한다."""

        self.runtime.context.verified_entity_snapshots = [
            VerifiedEntitySnapshot(
                kind=VerifiedEntityKind.ASSIGNMENT,
                year=2026,
                semester=1,
                items=[
                    VerifiedEntityReference(
                        kind=VerifiedEntityKind.ASSIGNMENT,
                        number=1,
                        id="1140975",
                        course_id="46499",
                        course_name="데이터마이닝[A,B]",
                        title="실습 1",
                        url=(
                            "https://learn.hansung.ac.kr/mod/assign/"
                            "view.php?id=1140975"
                        ),
                    ),
                    VerifiedEntityReference(
                        kind=VerifiedEntityKind.ASSIGNMENT,
                        number=2,
                        id="1140976",
                        course_id="46499",
                        course_name="데이터마이닝[A,B]",
                        title="실습 2",
                        url=(
                            "https://learn.hansung.ac.kr/mod/assign/"
                            "view.php?id=1140976"
                        ),
                    ),
                ],
            )
        ]
        plan = self._plan(
            self._task(
                agent=ExecutionTargetName.ECLASS,
                entity=ManagerEntityKind.ATTACHMENT,
                action=ManagerAction.LIST,
                slots=ManagerTaskSlots(year=2026, semester=1, ordinal=1),
                instruction="첫 번째 과제의 첨부파일 목록을 조회한다.",
            )
        )

        enriched = self.runtime._attach_verified_assignment_target(
            plan,
            self._event("2026년 1학기 데이터마이닝 첫 번째 과제 파일들 알려줘"),
        )

        target = enriched.tasks[0].verified_assignment_target
        self.assertIsNotNone(target)
        assert target is not None
        self.assertEqual(target.id, "1140975")
        self.assertEqual(target.course_id, "46499")
        self.assertEqual(target.year, 2026)
        self.assertEqual(target.semester, 1)

    def test_plural_file_contents_bind_same_parent_targets_in_snapshot_order(self) -> None:
        """`파일들 내용`은 동일 과제의 검증 첨부 두 개를 순서대로 batch에 넣는다."""

        self.runtime.context.verified_entity_snapshots = [
            VerifiedEntitySnapshot(
                kind=VerifiedEntityKind.ATTACHMENT,
                year=2026,
                semester=1,
                items=[
                    VerifiedEntityReference(
                        kind=VerifiedEntityKind.ATTACHMENT,
                        number=1,
                        id="att-pdf",
                        parent_id="1140975",
                        name="DM_2026-1_lab1.pdf",
                        url=(
                            "https://learn.hansung.ac.kr/pluginfile.php/1/"
                            "DM_2026-1_lab1.pdf"
                        ),
                        mime_type="application/pdf",
                    ),
                    VerifiedEntityReference(
                        kind=VerifiedEntityKind.ATTACHMENT,
                        number=2,
                        id="att-docx",
                        parent_id="1140975",
                        name="DM_2026-1_정답지.docx",
                        url=(
                            "https://learn.hansung.ac.kr/pluginfile.php/1/"
                            "DM_2026-1_answer.docx"
                        ),
                        mime_type=(
                            "application/vnd.openxmlformats-officedocument."
                            "wordprocessingml.document"
                        ),
                    ),
                ],
            )
        ]
        plan = self._plan(
            self._task(
                agent=ExecutionTargetName.DOCUMENT,
                entity=ManagerEntityKind.DOCUMENT,
                action=ManagerAction.ANALYZE,
                instruction="직전 과제의 첨부파일들을 분석한다.",
            )
        )

        enriched = self.runtime._attach_verified_attachment_target(
            plan,
            self._event("파일들 내용 알려줘"),
        )

        self.assertEqual(
            [task.agent for task in enriched.tasks],
            [ExecutionTargetName.ECLASS, ExecutionTargetName.DOCUMENT],
        )
        download_task, document_task = enriched.tasks
        self.assertEqual(
            [target.id for target in download_task.verified_attachment_targets],
            ["att-pdf", "att-docx"],
        )
        self.assertEqual(
            [target.parent_id for target in download_task.verified_attachment_targets],
            ["1140975", "1140975"],
        )
        self.assertEqual(
            [target.id for target in document_task.verified_attachment_targets],
            ["att-pdf", "att-docx"],
        )
        self.assertEqual(
            document_task.verified_attachment_ids,
            ["att-pdf", "att-docx"],
        )

    def test_mixed_parent_snapshot_rejects_forged_batch_and_unrequested_old_ref(self) -> None:
        """부모가 섞인 후보와 Manager 위조값은 과거 다운로드로 완화하지 않는다."""

        old_ref = "download:00000000-0000-0000-0000-000000000001:old-attachment"
        self.runtime.context.last_verified_entity_refs = [old_ref]
        self.runtime.context.verified_entity_snapshots = [
            VerifiedEntitySnapshot(
                kind=VerifiedEntityKind.ATTACHMENT,
                year=2026,
                semester=1,
                items=[
                    VerifiedEntityReference(
                        kind=VerifiedEntityKind.ATTACHMENT,
                        number=1,
                        id="parent-a-pdf",
                        parent_id="assignment-a",
                        name="a.pdf",
                        url="https://learn.hansung.ac.kr/pluginfile.php/1/a.pdf",
                        mime_type="application/pdf",
                    ),
                    VerifiedEntityReference(
                        kind=VerifiedEntityKind.ATTACHMENT,
                        number=2,
                        id="parent-b-docx",
                        parent_id="assignment-b",
                        name="b.docx",
                        url="https://learn.hansung.ac.kr/pluginfile.php/2/b.docx",
                        mime_type=(
                            "application/vnd.openxmlformats-officedocument."
                            "wordprocessingml.document"
                        ),
                    ),
                ],
            )
        ]
        forged = VerifiedAttachmentTarget(
            id="old-attachment",
            parent_id="assignment-forged",
            name="old.pdf",
            url="https://learn.hansung.ac.kr/pluginfile.php/9/old.pdf",
        )
        plan = self._plan(
            ManagerTask(
                agent=ExecutionTargetName.DOCUMENT,
                capability=CapabilityCode.DOCUMENT_ANALYSIS,
                entity=ManagerEntityKind.DOCUMENT,
                action=ManagerAction.ANALYZE,
                slots=ManagerTaskSlots(),
                instruction="첨부파일들을 분석한다.",
                # 이 필드들은 Manager가 만들 수 없는 Runtime 전용 값이다. 그럴듯한 값을
                # 심어도 현재 snapshot이 한 부모로 검증되지 않으면 모두 폐기돼야 한다.
                verified_attachment_targets=[forged],
                verified_attachment_ids=["old-attachment"],
                reuse_latest_verified_download=True,
            )
        )

        enriched = self.runtime._attach_verified_attachment_target(
            plan,
            self._event("파일들 내용 알려줘"),
        )

        self.assertEqual([task.agent for task in enriched.tasks], [ExecutionTargetName.DOCUMENT])
        document_task = enriched.tasks[0]
        self.assertEqual(document_task.verified_attachment_targets, [])
        self.assertEqual(document_task.verified_attachment_ids, [])
        self.assertIsNone(document_task.verified_attachment_target)
        self.assertIsNone(document_task.verified_attachment_id)
        self.assertFalse(document_task.reuse_latest_verified_download)
        self.assertEqual(document_task.verified_input_refs, [])
        self.assertIsNotNone(self.runtime._validate_plan_dependencies(enriched))


class AttachmentHandlerBatchTest(unittest.IsolatedAsyncioTestCase):
    """결박 이후 E-Class 다운로드와 Document 결과 집계까지 검증한다."""

    def setUp(self) -> None:
        self.settings = Settings(_env_file=None, openai_api_key="test-key")
        self.targets = [
            VerifiedAttachmentTarget(
                id="att-pdf",
                parent_id="1140975",
                name="DM_2026-1_lab1.pdf",
                url="https://learn.hansung.ac.kr/pluginfile.php/1/lab1.pdf",
            ),
            VerifiedAttachmentTarget(
                id="att-docx",
                parent_id="1140975",
                name="DM_2026-1_정답지.docx",
                url="https://learn.hansung.ac.kr/pluginfile.php/1/answer.docx",
            ),
        ]

    async def test_verified_assignment_attachment_list_returns_typed_snapshot(self) -> None:
        response = AttachmentListResult(
            ok=True,
            selected_term=SelectedTerm(
                year=2026,
                semester=1,
                selection_source="user_request",
            ),
            data=[
                Attachment(
                    id=target.id,
                    parent_type="assignment",
                    parent_id=target.parent_id,
                    name=target.name,
                    url=target.url,
                )
                for target in self.targets
            ],
        )
        server = SimpleNamespace(
            call_tool=AsyncMock(
                return_value=SimpleNamespace(
                    structuredContent=response.model_dump(mode="json")
                )
            )
        )
        handler = EclassMcpSpecialistHandler(self.settings)
        handler._ensure_server = AsyncMock(return_value=server)  # type: ignore[method-assign]
        task = ManagerTask(
            agent=ExecutionTargetName.ECLASS,
            capability=CapabilityCode.ECLASS_QUERY,
            entity=ManagerEntityKind.ATTACHMENT,
            action=ManagerAction.LIST,
            slots=ManagerTaskSlots(year=2026, semester=1, ordinal=1),
            instruction="첫 번째 과제 첨부파일 목록",
            verified_assignment_target=VerifiedAssignmentTarget(
                id="1140975",
                title="실습 1",
                course_id="46499",
                course_name="데이터마이닝[A,B]",
                year=2026,
                semester=1,
            ),
        )

        result = await handler(task)

        self.assertEqual(result.status, SpecialistStatus.COMPLETED)
        self.assertIn("DM_2026-1_lab1.pdf", result.verified_display_text or "")
        self.assertIn('"id":"att-docx"', result.verified_followup_context or "")
        server.call_tool.assert_awaited_once_with(
            "list_assignment_attachments",
            {"assignment_id": "1140975", "year": 2026, "semester": 1},
        )

    async def test_verified_batch_download_preserves_target_order_and_ids(self) -> None:
        responses = [
            DownloadResult(
                ok=True,
                data=DownloadInfo(
                    download_id="11111111-1111-1111-1111-111111111111",
                    attachment_id="att-pdf",
                    filename="DM_2026-1_lab1.pdf",
                    size_bytes=100,
                    sha256="a" * 64,
                    expires_at=datetime.now(timezone.utc),
                ),
            ),
            DownloadResult(
                ok=True,
                data=DownloadInfo(
                    download_id="22222222-2222-2222-2222-222222222222",
                    attachment_id="att-docx",
                    filename="DM_2026-1_정답지.docx",
                    size_bytes=200,
                    sha256="b" * 64,
                    expires_at=datetime.now(timezone.utc),
                ),
            ),
        ]
        server = SimpleNamespace(
            call_tool=AsyncMock(
                side_effect=[
                    SimpleNamespace(structuredContent=result.model_dump(mode="json"))
                    for result in responses
                ]
            )
        )
        handler = EclassMcpSpecialistHandler(self.settings)
        handler._ensure_server = AsyncMock(return_value=server)  # type: ignore[method-assign]
        task = ManagerTask(
            agent=ExecutionTargetName.ECLASS,
            capability=CapabilityCode.ECLASS_QUERY,
            entity=ManagerEntityKind.ATTACHMENT,
            action=ManagerAction.DOWNLOAD,
            slots=ManagerTaskSlots(),
            instruction="검증된 첨부 두 개 다운로드",
            verified_attachment_targets=self.targets,
        )

        result = await handler(task)

        self.assertEqual(result.status, SpecialistStatus.COMPLETED)
        self.assertEqual(
            result.evidence_refs,
            [
                "download:11111111-1111-1111-1111-111111111111:att-pdf",
                "download:22222222-2222-2222-2222-222222222222:att-docx",
            ],
        )
        called_ids = [call.args[1]["attachment_id"] for call in server.call_tool.await_args_list]
        self.assertEqual(called_ids, ["att-pdf", "att-docx"])

    async def test_document_batch_labels_each_analysis_with_original_filename(self) -> None:
        handler = DocumentSpecialistHandler(self.settings)
        handler._run_verified_pipeline = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                SpecialistResult(
                    status=SpecialistStatus.COMPLETED,
                    summary="PDF 분석 결과",
                    verified_display_text="PDF 분석 결과",
                    evidence_refs=["document:att-pdf:a"],
                ),
                SpecialistResult(
                    status=SpecialistStatus.COMPLETED,
                    summary="DOCX 분석 결과",
                    verified_display_text="DOCX 분석 결과",
                    evidence_refs=["document:att-docx:b"],
                ),
            ]
        )

        result = await handler._run_verified_batch(
            [
                ("11111111-1111-1111-1111-111111111111", "att-pdf"),
                ("22222222-2222-2222-2222-222222222222", "att-docx"),
            ],
            names_by_id={target.id: target.name for target in self.targets},
        )

        self.assertEqual(result.status, SpecialistStatus.COMPLETED)
        display = result.verified_display_text or ""
        self.assertLess(display.index("DM_2026-1_lab1.pdf"), display.index("DM_2026-1_정답지.docx"))
        self.assertIn("PDF 분석 결과", display)
        self.assertIn("DOCX 분석 결과", display)

    async def test_runtime_passes_both_current_download_refs_to_document(self) -> None:
        pdf_ref = "download:11111111-1111-1111-1111-111111111111:att-pdf"
        docx_ref = "download:22222222-2222-2222-2222-222222222222:att-docx"
        eclass = AsyncMock(
            return_value=SpecialistResult(
                status=SpecialistStatus.COMPLETED,
                summary="두 파일 다운로드 완료",
                evidence_refs=[pdf_ref, docx_ref],
            )
        )
        document = AsyncMock(
            return_value=SpecialistResult(
                status=SpecialistStatus.COMPLETED,
                summary="두 파일 분석 완료",
            )
        )
        runtime = ProactiveAssistantRuntime(
            self.settings,
            specialist_handlers={
                ExecutionTargetName.ECLASS: eclass,
                ExecutionTargetName.DOCUMENT: document,
            },
        )
        runtime.context.verified_entity_snapshots = [
            VerifiedEntitySnapshot(
                kind=VerifiedEntityKind.ATTACHMENT,
                year=2026,
                semester=1,
                items=[
                    VerifiedEntityReference(
                        kind=VerifiedEntityKind.ATTACHMENT,
                        number=index,
                        id=target.id,
                        parent_id=target.parent_id,
                        name=target.name,
                        url=target.url,
                    )
                    for index, target in enumerate(self.targets, start=1)
                ],
            )
        ]
        plan = ManagerPlan(
            mode=InteractionMode.TASK,
            reply="첨부파일들을 분석합니다.",
            conversation_summary="복수 첨부 분석 요청.",
            tasks=[
                ManagerTask(
                    agent=ExecutionTargetName.DOCUMENT,
                    capability=CapabilityCode.DOCUMENT_ANALYSIS,
                    entity=ManagerEntityKind.DOCUMENT,
                    action=ManagerAction.ANALYZE,
                    slots=ManagerTaskSlots(),
                    instruction="직전 과제 파일들을 분석한다.",
                )
            ],
            reason="검증 첨부 분석",
        )

        with patch(
            "app.runtime.assistant_runtime.create_plan",
            new=AsyncMock(return_value=plan),
        ):
            result = await runtime.handle_user_request("파일들 내용 알려줘")

        self.assertEqual(result.status, ManagerStatus.COMPLETED)
        download_task = eclass.await_args.args[0]
        document_task = document.await_args.args[0]
        self.assertEqual(
            [target.id for target in download_task.verified_attachment_targets],
            ["att-pdf", "att-docx"],
        )
        self.assertEqual(document_task.verified_input_refs, [pdf_ref, docx_ref])
        self.assertEqual(document_task.verified_attachment_ids, ["att-pdf", "att-docx"])

    async def test_combined_assignment_files_request_lists_downloads_and_forwards_batch(self) -> None:
        """첫 과제의 파일들 내용 요청은 한 turn에서 목록→두 다운로드→Document로 이어진다."""

        attachment_list = AttachmentListResult(
            ok=True,
            selected_term=SelectedTerm(
                year=2026,
                semester=1,
                selection_source="user_request",
            ),
            data=[
                Attachment(
                    id=target.id,
                    parent_type="assignment",
                    parent_id=target.parent_id,
                    name=target.name,
                    url=target.url,
                )
                for target in self.targets
            ],
        )
        pdf_ref = "download:11111111-1111-1111-1111-111111111111:att-pdf"
        docx_ref = "download:22222222-2222-2222-2222-222222222222:att-docx"
        download_results = [
            DownloadResult(
                ok=True,
                data=DownloadInfo(
                    download_id="11111111-1111-1111-1111-111111111111",
                    attachment_id="att-pdf",
                    filename="DM_2026-1_lab1.pdf",
                    size_bytes=100,
                    sha256="a" * 64,
                    expires_at=datetime.now(timezone.utc),
                ),
            ),
            DownloadResult(
                ok=True,
                data=DownloadInfo(
                    download_id="22222222-2222-2222-2222-222222222222",
                    attachment_id="att-docx",
                    filename="DM_2026-1_정답지.docx",
                    size_bytes=200,
                    sha256="b" * 64,
                    expires_at=datetime.now(timezone.utc),
                ),
            ),
        ]
        server = SimpleNamespace(
            call_tool=AsyncMock(
                side_effect=[
                    SimpleNamespace(
                        structuredContent=attachment_list.model_dump(mode="json")
                    ),
                    *[
                        SimpleNamespace(
                            structuredContent=result.model_dump(mode="json")
                        )
                        for result in download_results
                    ],
                ]
            )
        )
        eclass = EclassMcpSpecialistHandler(self.settings)
        eclass._ensure_server = AsyncMock(return_value=server)  # type: ignore[method-assign]
        document = AsyncMock(
            return_value=SpecialistResult(
                status=SpecialistStatus.COMPLETED,
                summary="두 파일 분석 완료",
                verified_display_text="두 파일 분석 완료",
            )
        )
        runtime = ProactiveAssistantRuntime(
            self.settings,
            specialist_handlers={
                ExecutionTargetName.ECLASS: eclass,
                ExecutionTargetName.DOCUMENT: document,
            },
        )
        runtime.context.verified_entity_snapshots = [
            VerifiedEntitySnapshot(
                kind=VerifiedEntityKind.ASSIGNMENT,
                year=2026,
                semester=1,
                items=[
                    VerifiedEntityReference(
                        kind=VerifiedEntityKind.ASSIGNMENT,
                        number=1,
                        id="1140975",
                        course_id="46499",
                        course_name="데이터마이닝[A,B]",
                        title="실습 1",
                        url=(
                            "https://learn.hansung.ac.kr/mod/assign/"
                            "view.php?id=1140975"
                        ),
                    ),
                    VerifiedEntityReference(
                        kind=VerifiedEntityKind.ASSIGNMENT,
                        number=2,
                        id="1140976",
                        course_id="46499",
                        course_name="데이터마이닝[A,B]",
                        title="실습 2",
                        url=(
                            "https://learn.hansung.ac.kr/mod/assign/"
                            "view.php?id=1140976"
                        ),
                    ),
                ],
            )
        ]
        plan = ManagerPlan(
            mode=InteractionMode.TASK,
            reply="첫 번째 과제의 첨부파일들을 분석합니다.",
            conversation_summary="첫 번째 과제 복수 첨부 분석 요청.",
            tasks=[
                ManagerTask(
                    agent=ExecutionTargetName.ECLASS,
                    capability=CapabilityCode.ECLASS_QUERY,
                    entity=ManagerEntityKind.ATTACHMENT,
                    action=ManagerAction.DOWNLOAD,
                    # 이 ordinal은 첨부 번호가 아니라 ASSIGNMENT snapshot의 첫 과제를
                    # 선택한다. 부모가 결박된 뒤 두 첨부를 다시 1개로 축소하면 안 된다.
                    slots=ManagerTaskSlots(year=2026, semester=1, ordinal=1),
                    instruction="첫 번째 과제의 첨부파일들을 모두 다운로드한다.",
                ),
                ManagerTask(
                    agent=ExecutionTargetName.DOCUMENT,
                    capability=CapabilityCode.DOCUMENT_ANALYSIS,
                    entity=ManagerEntityKind.DOCUMENT,
                    action=ManagerAction.ANALYZE,
                    slots=ManagerTaskSlots(),
                    instruction="방금 검증해 받은 첨부파일들을 분석한다.",
                ),
            ],
            reason="과제 선택, 첨부 다운로드, 문서 분석이 순서대로 필요하다.",
        )

        with patch(
            "app.runtime.assistant_runtime.create_plan",
            new=AsyncMock(return_value=plan),
        ):
            result = await runtime.handle_user_request(
                "2026년 1학기 데이터마이닝 첫 번째 과제 파일들 내용 알려줘"
            )

        self.assertEqual(result.status, ManagerStatus.COMPLETED)
        self.assertEqual(
            [call.args[0] for call in server.call_tool.await_args_list],
            [
                "list_assignment_attachments",
                "download_attachment",
                "download_attachment",
            ],
        )
        self.assertEqual(
            server.call_tool.await_args_list[0].args[1],
            {"assignment_id": "1140975", "year": 2026, "semester": 1},
        )
        self.assertEqual(
            [call.args[1]["attachment_id"] for call in server.call_tool.await_args_list[1:]],
            ["att-pdf", "att-docx"],
        )
        document_task = document.await_args.args[0]
        self.assertEqual(document_task.verified_input_refs, [pdf_ref, docx_ref])
        self.assertEqual(document_task.verified_attachment_ids, ["att-pdf", "att-docx"])
        self.assertEqual(
            [target.name for target in document_task.verified_attachment_targets],
            ["DM_2026-1_lab1.pdf", "DM_2026-1_정답지.docx"],
        )


if __name__ == "__main__":
    unittest.main()
