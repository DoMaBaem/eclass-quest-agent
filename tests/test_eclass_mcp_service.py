"""E-Class MCP 서비스의 응답 계약·오류 격리·동시성 테스트."""

from __future__ import annotations

import asyncio
import unittest
from datetime import timedelta
from unittest.mock import AsyncMock

from app.config import Settings
from app.schemas.domain import (
    Announcement,
    AnnouncementDetails,
    Assignment,
    Course,
    EntityStatus,
    Grade,
    Lecture,
    utc_now,
)
from mcp_server.adapters.base import TermScopedData
from mcp_server.browser.session import AuthRequiredError
from mcp_server.errors import EclassNotFoundError, EclassParserChangedError, EclassTemporaryError
from mcp_server.schemas import (
    DashboardSnapshotData,
    DashboardSnapshotResult,
    McpErrorCode,
    McpOutcomeStatus,
    SelectedTerm,
)
from mcp_server.services.eclass_read import EclassReadService
from mcp_server.services.locks import UserBrowserLockRegistry


class FakeAdapter:
    """서비스만 테스트하기 위해 모든 Adapter 메소드를 AsyncMock으로 대체한다."""

    def __init__(self) -> None:
        self.ensure_login = AsyncMock()
        self.list_courses = AsyncMock(return_value=[])
        self.list_announcements = AsyncMock(return_value=[])
        self.get_announcement_details = AsyncMock()
        self.list_assignments = AsyncMock(return_value=[])
        self.get_assignment_details = AsyncMock()
        self.list_assignment_attachments = AsyncMock(return_value=[])
        self.list_lectures = AsyncMock(return_value=[])
        self.get_lecture_status = AsyncMock()
        self.get_grades = AsyncMock(return_value=[])


class EclassReadServiceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.adapter = FakeAdapter()
        self.service = EclassReadService(
            Settings(_env_file=None),
            adapter=self.adapter,  # type: ignore[arg-type] -- 테스트 대역은 같은 메소드 계약을 지킨다.
        )

    def _course_scope(self, *courses: Course) -> None:
        self.adapter.list_courses.return_value = TermScopedData(
            data=list(courses),
            selected_term=SelectedTerm(
                year=2026,
                semester=1,
                selection_source="user_request",
            ),
        )

    def test_dashboard_snapshot_schema_rejects_contradictory_success_status(self) -> None:
        with self.assertRaises(ValueError):
            DashboardSnapshotResult(
                ok=True,
                status=McpOutcomeStatus.AUTH_REQUIRED,
                selected_term=SelectedTerm(
                    year=2026,
                    semester=1,
                    selection_source="eclass_default",
                ),
                data=DashboardSnapshotData(),
            )

    @staticmethod
    def _course(course_id: str = "46500", name: str = "빅데이터프로그래밍[7,A,N]") -> Course:
        return Course(
            id=course_id,
            name=name,
            url=f"https://learn.hansung.ac.kr/course/view.php?id={course_id}",
            year=2026,
            semester=1,
        )

    async def test_course_result_is_a_validated_live_envelope(self) -> None:
        self.adapter.list_courses.return_value = TermScopedData(
            data=[
                Course(
                    id="10",
                    name="테스트 강좌",
                    url="https://learn.hansung.ac.kr/course/view.php?id=10",
                    year=2026,
                    semester=1,
                )
            ],
            selected_term=SelectedTerm(
                year=2026,
                semester=1,
                selection_source="user_request",
            ),
        )

        result = await self.service.list_courses(2026, 1)

        self.assertTrue(result.ok)
        self.assertEqual(result.source, "live")
        self.assertEqual(result.data[0].id, "10")
        self.assertEqual(result.selected_term.year, 2026)  # type: ignore[union-attr]
        self.assertEqual(result.selected_term.selection_source, "user_request")  # type: ignore[union-attr]

    async def test_dashboard_snapshot_uses_one_default_term_for_every_collection(self) -> None:
        """기본 학기는 한 번만 읽고 나머지 영역은 그 학기를 명시해 수집한다."""

        default_term = SelectedTerm(
            year=2026,
            semester=1,
            selection_source="eclass_default",
        )
        explicit_term = SelectedTerm(
            year=2026,
            semester=1,
            selection_source="user_request",
        )
        course = self._course()
        announcement = Announcement(
            id="notice-1",
            course_id=course.id,
            title="공지",
            url="https://learn.hansung.ac.kr/mod/ubboard/article.php?id=1",
        )
        assignment = Assignment(
            id="assignment-1",
            course_id=course.id,
            title="과제",
            url="https://learn.hansung.ac.kr/mod/assign/view.php?id=1",
        )
        lecture = Lecture(
            id="lecture-1",
            course_id=course.id,
            title="강의",
            url="https://learn.hansung.ac.kr/mod/vod/view.php?id=1",
        )
        grade = Grade(id="grade-1", course_id=course.id, item="중간고사")
        self.adapter.list_courses.return_value = TermScopedData(
            data=[course], selected_term=default_term
        )
        self.adapter.list_announcements.return_value = TermScopedData(
            data=[announcement], selected_term=explicit_term
        )
        self.adapter.list_assignments.return_value = TermScopedData(
            data=[assignment], selected_term=explicit_term
        )
        self.adapter.list_lectures.return_value = TermScopedData(
            data=[lecture], selected_term=explicit_term
        )
        self.adapter.get_grades.return_value = TermScopedData(
            data=[grade], selected_term=explicit_term
        )

        result = await self.service.get_dashboard_snapshot()

        self.assertTrue(result.ok)
        self.assertEqual(result.status, McpOutcomeStatus.FOUND)
        self.assertEqual(result.selected_term, default_term)
        self.assertEqual(result.data.courses, [course])  # type: ignore[union-attr]
        self.assertEqual(result.data.announcements, [announcement])  # type: ignore[union-attr]
        self.assertEqual(result.data.assignments, [assignment])  # type: ignore[union-attr]
        self.assertEqual(result.data.lectures, [lecture])  # type: ignore[union-attr]
        self.assertEqual(result.data.grades, [grade])  # type: ignore[union-attr]
        self.adapter.list_courses.assert_awaited_once_with(year=None, semester=None)
        self.adapter.list_announcements.assert_awaited_once_with(
            course_id=None, limit=50, year=2026, semester=1
        )
        self.adapter.list_assignments.assert_awaited_once_with(
            course_id=None,
            days=None,
            only_incomplete=False,
            year=2026,
            semester=1,
        )
        self.adapter.list_lectures.assert_awaited_once_with(
            course_id=None,
            only_unwatched=False,
            year=2026,
            semester=1,
        )
        self.adapter.get_grades.assert_awaited_once_with(
            course_id=None, year=2026, semester=1
        )

    async def test_dashboard_snapshot_accepts_empty_vacation_term(self) -> None:
        """방학처럼 강좌와 활동이 0개인 기본 학기도 정상 Snapshot이다."""

        default_term = SelectedTerm(
            year=2026,
            semester=3,
            selection_source="eclass_default",
        )
        explicit_term = SelectedTerm(
            year=2026,
            semester=3,
            selection_source="user_request",
        )
        self.adapter.list_courses.return_value = TermScopedData(
            data=[], selected_term=default_term
        )
        for operation in (
            self.adapter.list_announcements,
            self.adapter.list_assignments,
            self.adapter.list_lectures,
            self.adapter.get_grades,
        ):
            operation.return_value = TermScopedData(data=[], selected_term=explicit_term)

        result = await self.service.get_dashboard_snapshot()

        self.assertTrue(result.ok)
        self.assertEqual(result.status, McpOutcomeStatus.FOUND)
        self.assertEqual(result.data.model_dump(), {  # type: ignore[union-attr]
            "courses": [],
            "announcements": [],
            "assignments": [],
            "lectures": [],
            "grades": [],
        })

    async def test_dashboard_snapshot_is_all_or_nothing_on_auth_failure(self) -> None:
        """중간 영역 인증 실패를 빈 데이터로 완화하거나 뒤 조회를 계속하지 않는다."""

        term = SelectedTerm(year=2026, semester=1, selection_source="eclass_default")
        self.adapter.list_courses.return_value = TermScopedData(data=[], selected_term=term)
        self.adapter.list_announcements.side_effect = AuthRequiredError("cookie=secret")

        result = await self.service.get_dashboard_snapshot()

        self.assertFalse(result.ok)
        self.assertEqual(result.status, McpOutcomeStatus.AUTH_REQUIRED)
        self.assertEqual(result.error.code, McpErrorCode.AUTH_REQUIRED)  # type: ignore[union-attr]
        self.assertIsNone(result.data)
        self.adapter.list_assignments.assert_not_awaited()
        self.adapter.list_lectures.assert_not_awaited()
        self.adapter.get_grades.assert_not_awaited()

    async def test_dashboard_snapshot_rejects_term_drift(self) -> None:
        """일부 영역이 다른 학기를 반환하면 부분 Snapshot을 성공시키지 않는다."""

        default_term = SelectedTerm(
            year=2026, semester=1, selection_source="eclass_default"
        )
        expected_term = SelectedTerm(
            year=2026, semester=1, selection_source="user_request"
        )
        drifted_term = SelectedTerm(
            year=2026, semester=2, selection_source="user_request"
        )
        self.adapter.list_courses.return_value = TermScopedData(
            data=[], selected_term=default_term
        )
        self.adapter.list_announcements.return_value = TermScopedData(
            data=[], selected_term=expected_term
        )
        self.adapter.list_assignments.return_value = TermScopedData(
            data=[], selected_term=drifted_term
        )
        self.adapter.list_lectures.return_value = TermScopedData(
            data=[], selected_term=expected_term
        )
        self.adapter.get_grades.return_value = TermScopedData(
            data=[], selected_term=expected_term
        )

        result = await self.service.get_dashboard_snapshot()

        self.assertFalse(result.ok)
        self.assertEqual(result.status, McpOutcomeStatus.TEMPORARY_FAILURE)
        self.assertIsNone(result.data)

    async def test_year_and_semester_must_be_both_present_or_both_omitted(self) -> None:
        result = await self.service.list_courses(2026, None)

        self.assertFalse(result.ok)
        self.adapter.list_courses.assert_not_awaited()

    async def test_assignment_course_filter_is_forwarded_to_adapter(self) -> None:
        self.adapter.list_assignments.return_value = TermScopedData(
            data=[],
            selected_term=SelectedTerm(
                year=2026,
                semester=1,
                selection_source="user_request",
            ),
        )

        result = await self.service.list_assignments(None, False, 2026, 1, "46499")

        self.assertTrue(result.ok)
        self.adapter.list_assignments.assert_awaited_once_with(
            course_id="46499",
            days=None,
            only_incomplete=False,
            year=2026,
            semester=1,
        )

    async def test_assignment_course_query_is_resolved_before_adapter_call(self) -> None:
        self.adapter.list_courses.return_value = TermScopedData(
            data=[
                Course(
                    id="46500",
                    name="빅데이터프로그래밍[7,A,N]",
                    url="https://learn.hansung.ac.kr/course/view.php?id=46500",
                    year=2026,
                    semester=1,
                )
            ],
            selected_term=SelectedTerm(
                year=2026,
                semester=1,
                selection_source="user_request",
            ),
        )
        self.adapter.list_assignments.return_value = TermScopedData(
            data=[],
            selected_term=SelectedTerm(
                year=2026,
                semester=1,
                selection_source="user_request",
            ),
        )

        result = await self.service.list_assignments(
            None,
            False,
            2026,
            1,
            None,
            "빅데프",
        )

        self.assertTrue(result.ok)
        self.adapter.list_assignments.assert_awaited_once_with(
            course_id="46500",
            days=None,
            only_incomplete=False,
            year=2026,
            semester=1,
        )

    async def test_course_assignment_followup_inherits_verified_term(self) -> None:
        """Agent가 두 번째 Tool에서 학기를 빼도 직전 강좌 목록의 학기를 유지한다."""

        self.adapter.list_courses.return_value = TermScopedData(
            data=[
                Course(
                    id="46500",
                    name="빅데이터프로그래밍[7,A,N]",
                    url="https://learn.hansung.ac.kr/course/view.php?id=46500",
                    year=2026,
                    semester=1,
                )
            ],
            selected_term=SelectedTerm(
                year=2026,
                semester=1,
                selection_source="user_request",
            ),
        )
        self.adapter.list_assignments.return_value = TermScopedData(
            data=[],
            selected_term=SelectedTerm(
                year=2026,
                semester=1,
                selection_source="user_request",
            ),
        )

        await self.service.list_courses(2026, 1)
        result = await self.service.list_assignments(None, False, None, None, "46500")

        self.assertTrue(result.ok)
        self.adapter.list_assignments.assert_awaited_once_with(
            course_id="46500",
            days=None,
            only_incomplete=False,
            year=2026,
            semester=1,
        )

    async def test_all_course_assignment_query_does_not_inherit_previous_term(self) -> None:
        """강좌 필터가 없는 요청은 여전히 E-Class 기본 학기를 사용한다."""

        self.service._last_course_lookup_term = SelectedTerm(
            year=2026,
            semester=1,
            selection_source="user_request",
        )
        self.adapter.list_assignments.return_value = TermScopedData(
            data=[],
            selected_term=SelectedTerm(
                year=2026,
                semester=3,
                selection_source="eclass_default",
            ),
        )

        await self.service.list_assignments(None, False, None, None, None)

        self.adapter.list_assignments.assert_awaited_once_with(
            course_id=None,
            days=None,
            only_incomplete=False,
            year=None,
            semester=None,
        )

    async def test_announcement_details_returns_verified_body(self) -> None:
        self.adapter.get_announcement_details.return_value = TermScopedData(
            data=AnnouncementDetails(
                id="532941",
                course_id="46500",
                title="결과보고서 작성 관련 안내사항",
                url="https://learn.hansung.ac.kr/mod/ubboard/article.php?id=1102436&bwid=532941",
                author="담당교수",
                content="결과보고서 작성 시 팀원별 역할과 피드백 반영 내용을 작성합니다.",
            ),
            selected_term=SelectedTerm(
                year=2026,
                semester=1,
                selection_source="user_request",
            ),
        )

        result = await self.service.get_announcement_details(
            "https://learn.hansung.ac.kr/mod/ubboard/article.php?id=1102436&bwid=532941",
            "46500",
            2026,
            1,
        )

        self.assertTrue(result.ok)
        self.assertIn("팀원별 역할", result.data.content)  # type: ignore[union-attr]
        self.assertEqual(result.selected_term.year, 2026)  # type: ignore[union-attr]

    async def test_course_announcement_tool_resolves_name_without_exposing_id_input(self) -> None:
        course = self._course()
        self._course_scope(course)
        self.adapter.list_announcements.return_value = TermScopedData(
            data=[
                Announcement(
                    id="11",
                    course_id=course.id,
                    title="중간고사 안내",
                    url="https://learn.hansung.ac.kr/mod/ubboard/article.php?id=11",
                )
            ],
            selected_term=SelectedTerm(
                year=2026,
                semester=1,
                selection_source="user_request",
            ),
        )

        result = await self.service.list_course_announcements("빅데프", 20, 2026, 1)

        self.assertTrue(result.ok)
        self.assertEqual(result.status, McpOutcomeStatus.FOUND)
        self.assertEqual(result.data.course.course_id, "46500")  # type: ignore[union-attr]
        self.assertEqual(result.data.announcements[0].title, "중간고사 안내")  # type: ignore[union-attr]
        self.adapter.list_announcements.assert_awaited_once_with(
            course_id="46500",
            limit=20,
            year=2026,
            semester=1,
        )

    async def test_course_assignment_tool_resolves_course_and_assignment_typo(self) -> None:
        course = self._course()
        self._course_scope(course)
        self.adapter.list_assignments.return_value = TermScopedData(
            data=[
                Assignment(
                    id="21",
                    course_id=course.id,
                    title="실습과제 제출 가이드라인",
                    url="https://learn.hansung.ac.kr/mod/assign/view.php?id=21",
                )
            ],
            selected_term=SelectedTerm(
                year=2026,
                semester=1,
                selection_source="user_request",
            ),
        )

        result = await self.service.list_course_assignments(
            "빅데프",
            None,
            False,
            "실습과재",
            2026,
            1,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.status, McpOutcomeStatus.FOUND)
        self.assertEqual([item.id for item in result.data.assignments], ["21"])  # type: ignore[union-attr]

    async def test_course_lecture_tool_filters_week_after_verified_course_resolution(self) -> None:
        course = self._course()
        self._course_scope(course)
        self.adapter.list_lectures.return_value = TermScopedData(
            data=[
                Lecture(
                    id="301",
                    course_id=course.id,
                    title="01주차 Python 기초",
                    url="https://learn.hansung.ac.kr/mod/vod/view.php?id=301",
                    week=1,
                ),
                Lecture(
                    id="302",
                    course_id=course.id,
                    title="02주차 가상환경",
                    url="https://learn.hansung.ac.kr/mod/vod/view.php?id=302",
                    week=2,
                ),
            ],
            selected_term=SelectedTerm(
                year=2026,
                semester=1,
                selection_source="user_request",
            ),
        )

        result = await self.service.list_course_lectures("빅데프", 2, False, 2026, 1)

        self.assertTrue(result.ok)
        self.assertEqual(result.status, McpOutcomeStatus.FOUND)
        self.assertEqual([lecture.id for lecture in result.data.lectures], ["302"])  # type: ignore[union-attr]
        self.adapter.list_lectures.assert_awaited_once_with(
            course_id="46500",
            only_unwatched=False,
            year=2026,
            semester=1,
        )

    async def test_resolve_lecture_issues_opaque_reference_for_exactly_one_target(self) -> None:
        course = self._course()
        self._course_scope(course)
        self.adapter.list_lectures.return_value = TermScopedData(
            data=[
                Lecture(
                    id="302",
                    course_id=course.id,
                    title="[동영상] 02주차 Python 개요 및 가상환경 구축",
                    url="https://learn.hansung.ac.kr/mod/vod/view.php?id=302",
                    week=2,
                    attendance_status=EntityStatus.INCOMPLETE,
                )
            ],
            selected_term=SelectedTerm(
                year=2026,
                semester=1,
                selection_source="user_request",
            ),
        )

        result = await self.service.resolve_lecture(
            "빅데프",
            2,
            "가상환경",
            False,
            2026,
            1,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.status, McpOutcomeStatus.FOUND)
        self.assertEqual(len(result.data.reference_id), 36)  # type: ignore[union-attr]
        verified = self.service.get_verified_lecture_target(result.data.reference_id)  # type: ignore[union-attr]
        self.assertIsNotNone(verified)
        self.assertEqual(verified.lecture_id, "302")  # type: ignore[union-attr]

    async def test_resolve_lecture_never_picks_one_of_multiple_candidates(self) -> None:
        course = self._course()
        self._course_scope(course)
        self.adapter.list_lectures.return_value = TermScopedData(
            data=[
                Lecture(
                    id=str(identifier),
                    course_id=course.id,
                    title=f"02주차 Python 실습 {identifier}",
                    url=f"https://learn.hansung.ac.kr/mod/vod/view.php?id={identifier}",
                    week=2,
                )
                for identifier in (301, 302)
            ],
            selected_term=SelectedTerm(
                year=2026,
                semester=1,
                selection_source="user_request",
            ),
        )

        result = await self.service.resolve_lecture(
            "빅데프",
            2,
            None,
            False,
            2026,
            1,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.status, McpOutcomeStatus.AMBIGUOUS)
        self.assertIsNone(result.data)
        self.assertEqual([candidate.id for candidate in result.candidates], ["301", "302"])

    async def test_ambiguous_course_is_returned_as_typed_status_without_content_call(self) -> None:
        self._course_scope(
            self._course("1", "데이터마이닝[A]"),
            self._course("2", "데이터마이닝[B]"),
        )

        result = await self.service.list_course_lectures("데이터마이닝", 2, False, 2026, 1)

        self.assertFalse(result.ok)
        self.assertEqual(result.status, McpOutcomeStatus.AMBIGUOUS)
        self.assertEqual([candidate.id for candidate in result.candidates], ["1", "2"])
        self.adapter.list_lectures.assert_not_awaited()

    async def test_high_level_tool_preserves_auth_required_status(self) -> None:
        self.adapter.list_courses.side_effect = AuthRequiredError("cookie=secret")

        result = await self.service.list_course_announcements("빅데프", 20, 2026, 1)

        self.assertFalse(result.ok)
        self.assertEqual(result.status, McpOutcomeStatus.AUTH_REQUIRED)
        self.assertEqual(result.error.code, McpErrorCode.AUTH_REQUIRED)  # type: ignore[union-attr]

    async def test_expired_or_unknown_lecture_reference_is_rejected(self) -> None:
        course = self._course()
        self._course_scope(course)
        self.adapter.list_lectures.return_value = TermScopedData(
            data=[
                Lecture(
                    id="302",
                    course_id=course.id,
                    title="02주차 가상환경",
                    url="https://learn.hansung.ac.kr/mod/vod/view.php?id=302",
                    week=2,
                )
            ],
            selected_term=SelectedTerm(
                year=2026,
                semester=1,
                selection_source="user_request",
            ),
        )
        result = await self.service.resolve_lecture("빅데프", 2, None, False, 2026, 1)
        reference_id = result.data.reference_id  # type: ignore[union-attr]
        self.service._verified_lecture_targets[reference_id] = result.data.model_copy(  # type: ignore[union-attr]
            update={"expires_at": utc_now() - timedelta(seconds=1)}
        )

        self.assertIsNone(self.service.get_verified_lecture_target(reference_id))
        self.assertIsNone(self.service.get_verified_lecture_target("not-issued-by-server"))

    async def test_internal_errors_are_mapped_without_leaking_exception_text(self) -> None:
        cases = (
            (AuthRequiredError("cookie=secret"), McpErrorCode.AUTH_REQUIRED),
            (EclassNotFoundError("private url"), McpErrorCode.NOT_FOUND),
            (EclassParserChangedError("html body"), McpErrorCode.PARSER_CHANGED),
            (EclassTemporaryError("browser log"), McpErrorCode.TEMPORARY_FAILURE),
            (RuntimeError("password=secret"), McpErrorCode.TEMPORARY_FAILURE),
        )
        for exception, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                self.adapter.list_courses.side_effect = exception
                result = await self.service.list_courses(2026, 1)
                self.assertFalse(result.ok)
                self.assertEqual(result.error.code, expected_code)  # type: ignore[union-attr]
                serialized = result.model_dump_json()
                self.assertNotIn(str(exception), serialized)

    async def test_same_user_browser_operations_are_serialized(self) -> None:
        active = 0
        maximum = 0

        async def slow_courses(**_: object) -> TermScopedData[list[Course]]:
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.01)
            active -= 1
            return TermScopedData(
                data=[],
                selected_term=SelectedTerm(
                    year=2026,
                    semester=1,
                    selection_source="eclass_default",
                ),
            )

        self.adapter.list_courses.side_effect = slow_courses
        await asyncio.gather(
            self.service.list_courses(2026, 1),
            self.service.list_courses(2026, 1),
        )
        self.assertEqual(maximum, 1)


class UserBrowserLockRegistryTest(unittest.TestCase):
    def test_same_user_reuses_lock_but_other_user_does_not(self) -> None:
        locks = UserBrowserLockRegistry()
        self.assertIs(locks.for_user("a"), locks.for_user("a"))
        self.assertIsNot(locks.for_user("a"), locks.for_user("b"))
