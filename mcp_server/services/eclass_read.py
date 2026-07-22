"""Playwright Adapter를 안전한 MCP 응답 계약으로 감싸는 서비스."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import TypeVar
from uuid import uuid4

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from pydantic import ValidationError

from app.config import Settings
from app.schemas.domain import Course, Lecture, utc_now
from mcp_server.adapters.base import EclassAdapter, TermScopedData
from mcp_server.adapters.hansung_playwright import HansungPlaywrightAdapter
from mcp_server.browser.credential_login import automatic_login_available
from mcp_server.browser.session import AuthRequiredError
from mcp_server.errors import EclassNotFoundError, EclassParserChangedError, EclassTemporaryError
from mcp_server.schemas import (
    AnnouncementDetailsResult,
    AnnouncementListResult,
    AssignmentDetailsResult,
    AssignmentListResult,
    AttachmentListResult,
    CourseAnnouncementData,
    CourseAnnouncementResult,
    CourseAssignmentData,
    CourseAssignmentResult,
    CourseLectureData,
    CourseLectureResult,
    CourseListResult,
    CourseResolutionResult,
    DashboardSnapshotData,
    DashboardSnapshotResult,
    GradeListResult,
    LectureListResult,
    LectureResolutionResult,
    LectureStatusResult,
    McpErrorCode,
    McpOutcomeStatus,
    McpResponse,
    McpToolError,
    SemanticMcpResponse,
    SelectedTerm,
    SessionCheckResult,
    SessionInfo,
    VerifiedCourseReference,
    VerifiedLectureTarget,
)
from mcp_server.services.locks import UserBrowserLockRegistry
from mcp_server.services.course_resolution import (
    filter_assignments_by_query,
    filter_lectures_by_query,
    resolve_course_query,
)


ResponseT = TypeVar("ResponseT", bound=McpResponse)
DataT = TypeVar("DataT")


class EclassReadService:
    """한 사용자의 브라우저 작업을 직렬화하고 오류를 표준화한다."""

    def __init__(
        self,
        settings: Settings,
        *,
        adapter: EclassAdapter | None = None,
        locks: UserBrowserLockRegistry | None = None,
        user_id: str = "local-user",
    ) -> None:
        self.settings = settings
        self.adapter = adapter or HansungPlaywrightAdapter(settings)
        self.locks = locks or UserBrowserLockRegistry()
        self.user_id = user_id
        # Agent가 ``list_courses(year, semester)`` 다음 Tool에서 학기를 빠뜨려도 같은 강좌 조회가
        # E-Class 기본 학기로 되돌아가지 않도록 마지막으로 검증한 강좌 목록 학기를 기억한다.
        self._last_course_lookup_term: SelectedTerm | None = None
        # 재생 Tool에 원시 lecture_id를 넘기지 않도록, resolve_lecture가 검증한 대상을
        # 짧은 시간 동안 불투명 reference_id로 보관한다.
        self._verified_lecture_targets: dict[str, VerifiedLectureTarget] = {}
        self._verified_target_ttl = timedelta(minutes=15)

    async def check_session(self) -> SessionCheckResult:
        return await self._execute(
            SessionCheckResult,
            self.adapter.ensure_login,
            transform=lambda _: SessionInfo(
                authenticated=True,
                auto_login_enabled=automatic_login_available(self.settings),
            ),
        )

    async def list_courses(
        self, year: int | None, semester: int | None
    ) -> CourseListResult:
        invalid = self._validate_term(year, semester)
        if invalid:
            return self._bad_request(CourseListResult, invalid)
        result = await self._execute_term(
            CourseListResult,
            lambda: self.adapter.list_courses(year=year, semester=semester),
        )
        if result.ok and result.selected_term is not None:
            self._last_course_lookup_term = result.selected_term
        return result

    async def get_dashboard_snapshot(self) -> DashboardSnapshotResult:
        """E-Class가 기본으로 선택한 현재 학기의 동기화 원본을 일괄 수집한다.

        첫 강좌 조회에서 학기를 한 번 확정한 뒤 나머지 조회에는 그 연도·학기를
        명시한다. 다섯 영역 중 하나라도 실패하거나 서로 다른 학기를 반환하면 부분
        Snapshot을 저장하지 않도록 전체 호출을 실패시킨다.
        """

        async def collect() -> TermScopedData[DashboardSnapshotData]:
            courses = await self.adapter.list_courses(year=None, semester=None)
            selected_term = courses.selected_term
            year = selected_term.year
            semester = selected_term.semester

            announcements = await self.adapter.list_announcements(
                course_id=None,
                limit=50,
                year=year,
                semester=semester,
            )
            assignments = await self.adapter.list_assignments(
                course_id=None,
                days=None,
                only_incomplete=False,
                year=year,
                semester=semester,
            )
            lectures = await self.adapter.list_lectures(
                course_id=None,
                only_unwatched=False,
                year=year,
                semester=semester,
            )
            grades = await self.adapter.get_grades(
                course_id=None,
                year=year,
                semester=semester,
            )

            for scoped in (announcements, assignments, lectures, grades):
                if (
                    scoped.selected_term.year != year
                    or scoped.selected_term.semester != semester
                ):
                    raise EclassTemporaryError(
                        "Dashboard 수집 중 E-Class 선택 학기가 변경되었습니다."
                    )

            return TermScopedData(
                data=DashboardSnapshotData(
                    courses=courses.data,
                    announcements=announcements.data,
                    assignments=assignments.data,
                    lectures=lectures.data,
                    grades=grades.data,
                ),
                # 후속 명시 조회의 selection_source가 user_request여도 Snapshot 자체는
                # E-Class가 최초에 기본 선택한 학기라는 출처를 보존한다.
                selected_term=selected_term,
            )

        result = await self._execute_term(DashboardSnapshotResult, collect)
        if result.ok and result.selected_term is not None:
            self._last_course_lookup_term = result.selected_term
        return result

    async def resolve_course(
        self,
        query: str,
        year: int | None,
        semester: int | None,
    ) -> CourseResolutionResult:
        """사람이 입력한 강좌명·약칭을 선택 학기의 실제 강좌와 연결한다."""

        query = query.strip()
        if not query or len(query) > 300:
            return self._bad_request(CourseResolutionResult, "강좌 검색어는 1~300자로 입력해 주세요.")
        courses = await self.list_courses(year, semester)
        if not courses.ok:
            return CourseResolutionResult(
                ok=False,
                selected_term=courses.selected_term,
                error=courses.error,
            )
        return CourseResolutionResult(
            ok=True,
            selected_term=courses.selected_term,
            data=resolve_course_query(query, courses.data),
        )

    async def list_announcements(
        self,
        course_id: str | None,
        limit: int,
        year: int | None,
        semester: int | None,
    ) -> AnnouncementListResult:
        if not 1 <= limit <= 100:
            return self._bad_request(AnnouncementListResult, "limit은 1~100으로 입력해 주세요.")
        invalid = self._validate_term(year, semester)
        if invalid:
            return self._bad_request(AnnouncementListResult, invalid)
        return await self._execute_term(
            AnnouncementListResult,
            lambda: self.adapter.list_announcements(
                course_id=course_id,
                limit=limit,
                year=year,
                semester=semester,
            ),
        )

    async def list_course_announcements(
        self,
        course_query: str,
        limit: int,
        year: int | None,
        semester: int | None,
    ) -> CourseAnnouncementResult:
        """강좌명을 서버 내부에서 ID로 해석한 뒤 그 강좌 공지만 조회한다."""

        resolution = await self.resolve_course(course_query, year, semester)
        failure = self._course_scoped_failure(CourseAnnouncementResult, resolution)
        if failure is not None:
            return failure
        assert resolution.data is not None and resolution.data.course is not None
        course = resolution.data.course
        selected_year, selected_semester = self._selected_term_values(
            resolution.selected_term,
            course,
        )
        announcements = await self.list_announcements(
            course.id,
            limit,
            selected_year,
            selected_semester,
        )
        if not announcements.ok:
            return CourseAnnouncementResult(
                ok=False,
                status=self._outcome_from_error(announcements.error),
                selected_term=announcements.selected_term or resolution.selected_term,
                error=announcements.error,
            )
        return CourseAnnouncementResult(
            ok=True,
            status=McpOutcomeStatus.FOUND,
            selected_term=announcements.selected_term,
            data=CourseAnnouncementData(
                course=self._verified_course(course),
                announcements=announcements.data,
            ),
        )

    async def get_announcement_details(
        self,
        announcement_url: str,
        course_id: str | None,
        year: int | None,
        semester: int | None,
    ) -> AnnouncementDetailsResult:
        invalid = self._validate_term(year, semester)
        if invalid:
            return self._bad_request(AnnouncementDetailsResult, invalid)
        return await self._execute_term(
            AnnouncementDetailsResult,
            lambda: self.adapter.get_announcement_details(
                announcement_url,
                course_id=course_id,
                year=year,
                semester=semester,
            ),
        )

    async def list_assignments(
        self,
        days: int | None,
        only_incomplete: bool,
        year: int | None,
        semester: int | None,
        course_id: str | None = None,
        course_query: str | None = None,
        assignment_query: str | None = None,
    ) -> AssignmentListResult:
        if course_id is not None and course_query is not None:
            return self._bad_request(
                AssignmentListResult,
                "course_id와 course_query는 동시에 지정할 수 없습니다.",
            )
        if course_query is not None:
            resolution = await self.resolve_course(course_query, year, semester)
            if not resolution.ok or resolution.data is None:
                return AssignmentListResult(
                    ok=False,
                    selected_term=resolution.selected_term,
                    error=resolution.error,
                )
            if resolution.data.status == "AMBIGUOUS":
                return self._failure(
                    AssignmentListResult,
                    McpErrorCode.AMBIGUOUS_MATCH,
                    "강좌 검색 결과가 여러 개입니다. resolve_course의 후보 중 하나를 지정해 주세요.",
                    retryable=False,
                )
            if resolution.data.status != "MATCHED" or resolution.data.course is None:
                return self._failure(
                    AssignmentListResult,
                    McpErrorCode.NOT_FOUND,
                    "선택 학기에서 일치하는 강좌를 찾을 수 없습니다.",
                    retryable=False,
                )
            course_id = resolution.data.course.id
            if resolution.selected_term is not None:
                year = resolution.selected_term.year
                semester = resolution.selected_term.semester
        year, semester = self._inherit_course_lookup_term(course_id, year, semester)
        if days is not None and not 0 <= days <= 365:
            return self._bad_request(AssignmentListResult, "days는 0~365로 입력해 주세요.")
        invalid = self._validate_term(year, semester)
        if invalid:
            return self._bad_request(AssignmentListResult, invalid)
        result = await self._execute_term(
            AssignmentListResult,
            lambda: self.adapter.list_assignments(
                course_id=course_id,
                days=days,
                only_incomplete=only_incomplete,
                year=year,
                semester=semester,
            ),
        )
        if result.ok and assignment_query is not None:
            query = assignment_query.strip()
            if not query or len(query) > 500:
                return self._bad_request(AssignmentListResult, "과제 검색어는 1~500자로 입력해 주세요.")
            result = result.model_copy(update={"data": filter_assignments_by_query(query, result.data)})
        return result

    async def list_course_assignments(
        self,
        course_query: str,
        days: int | None,
        only_incomplete: bool,
        assignment_query: str | None,
        year: int | None,
        semester: int | None,
    ) -> CourseAssignmentResult:
        """과목 ID 전달 없이 과목명·약칭만으로 검증된 과제 목록을 조회한다."""

        resolution = await self.resolve_course(course_query, year, semester)
        failure = self._course_scoped_failure(CourseAssignmentResult, resolution)
        if failure is not None:
            return failure
        assert resolution.data is not None and resolution.data.course is not None
        course = resolution.data.course
        selected_year, selected_semester = self._selected_term_values(
            resolution.selected_term,
            course,
        )
        assignments = await self.list_assignments(
            days,
            only_incomplete,
            selected_year,
            selected_semester,
            course.id,
            None,
            assignment_query,
        )
        if not assignments.ok:
            return CourseAssignmentResult(
                ok=False,
                status=self._outcome_from_error(assignments.error),
                selected_term=assignments.selected_term or resolution.selected_term,
                error=assignments.error,
            )
        if assignment_query is not None and not assignments.data:
            return CourseAssignmentResult(
                ok=False,
                status=McpOutcomeStatus.NOT_FOUND,
                selected_term=assignments.selected_term,
                error=McpToolError(
                    code=McpErrorCode.NOT_FOUND,
                    message="해당 강좌에서 검색어와 일치하는 과제를 찾을 수 없습니다.",
                    retryable=False,
                ),
                data=CourseAssignmentData(
                    course=self._verified_course(course),
                    assignments=[],
                ),
            )
        return CourseAssignmentResult(
            ok=True,
            status=McpOutcomeStatus.FOUND,
            selected_term=assignments.selected_term,
            data=CourseAssignmentData(
                course=self._verified_course(course),
                assignments=assignments.data,
            ),
        )

    def _inherit_course_lookup_term(
        self,
        course_id: str | None,
        year: int | None,
        semester: int | None,
    ) -> tuple[int | None, int | None]:
        """강좌 ID 후속 호출이 학기를 누락했을 때 직전 검증 학기를 이어 쓴다.

        전체 과제 요청처럼 ``course_id``가 없는 호출은 사용자가 의도한 E-Class 기본 학기를 그대로
        사용한다. 연도·학기 중 하나만 들어온 잘못된 입력도 상속으로 감추지 않고 기존 검증에서 거부한다.
        """

        if (
            course_id is not None
            and year is None
            and semester is None
            and self._last_course_lookup_term is not None
        ):
            return (
                self._last_course_lookup_term.year,
                self._last_course_lookup_term.semester,
            )
        return year, semester

    async def get_assignment_details(
        self, assignment_id: str, year: int | None, semester: int | None
    ) -> AssignmentDetailsResult:
        invalid = self._validate_term(year, semester)
        if invalid:
            return self._bad_request(AssignmentDetailsResult, invalid)
        return await self._execute_term(
            AssignmentDetailsResult,
            lambda: self.adapter.get_assignment_details(
                assignment_id,
                year=year,
                semester=semester,
            ),
        )

    async def list_assignment_attachments(
        self, assignment_id: str, year: int | None, semester: int | None
    ) -> AttachmentListResult:
        invalid = self._validate_term(year, semester)
        if invalid:
            return self._bad_request(AttachmentListResult, invalid)
        return await self._execute_term(
            AttachmentListResult,
            lambda: self.adapter.list_assignment_attachments(
                assignment_id,
                year=year,
                semester=semester,
            ),
        )

    async def list_lectures(
        self,
        course_id: str | None,
        only_unwatched: bool,
        year: int | None,
        semester: int | None,
    ) -> LectureListResult:
        invalid = self._validate_term(year, semester)
        if invalid:
            return self._bad_request(LectureListResult, invalid)
        return await self._execute_term(
            LectureListResult,
            lambda: self.adapter.list_lectures(
                course_id=course_id,
                only_unwatched=only_unwatched,
                year=year,
                semester=semester,
            ),
        )

    async def list_course_lectures(
        self,
        course_query: str,
        week: int | None,
        only_unwatched: bool,
        year: int | None,
        semester: int | None,
    ) -> CourseLectureResult:
        """강좌명·주차를 한 번에 해석하고 그 범위에 속한 강의만 반환한다."""

        if week is not None and not 1 <= week <= 99:
            return CourseLectureResult(
                ok=False,
                status=McpOutcomeStatus.INVALID_REQUEST,
                error=McpToolError(
                    code=McpErrorCode.INVALID_REQUEST,
                    message="주차는 1~99 사이로 입력해 주세요.",
                    retryable=False,
                ),
            )
        resolution = await self.resolve_course(course_query, year, semester)
        failure = self._course_scoped_failure(CourseLectureResult, resolution)
        if failure is not None:
            return failure
        assert resolution.data is not None and resolution.data.course is not None
        course = resolution.data.course
        selected_year, selected_semester = self._selected_term_values(
            resolution.selected_term,
            course,
        )
        lectures = await self.list_lectures(
            course.id,
            only_unwatched,
            selected_year,
            selected_semester,
        )
        if not lectures.ok:
            return CourseLectureResult(
                ok=False,
                status=self._outcome_from_error(lectures.error),
                selected_term=lectures.selected_term or resolution.selected_term,
                error=lectures.error,
            )
        items = [lecture for lecture in lectures.data if week is None or lecture.week == week]
        return CourseLectureResult(
            ok=True,
            status=McpOutcomeStatus.FOUND,
            selected_term=lectures.selected_term,
            data=CourseLectureData(
                course=self._verified_course(course),
                requested_week=week,
                lectures=items,
            ),
        )

    async def resolve_lecture(
        self,
        course_query: str,
        week: int | None,
        title_query: str | None,
        only_unwatched: bool,
        year: int | None,
        semester: int | None,
    ) -> LectureResolutionResult:
        """자연어 강좌·주차·제목 조건을 단 하나의 재생 가능한 대상으로 확정한다."""

        if title_query is not None:
            title_query = title_query.strip()
            if not title_query or len(title_query) > 500:
                return LectureResolutionResult(
                    ok=False,
                    status=McpOutcomeStatus.INVALID_REQUEST,
                    error=McpToolError(
                        code=McpErrorCode.INVALID_REQUEST,
                        message="강의 제목 검색어는 1~500자로 입력해 주세요.",
                        retryable=False,
                    ),
                )
        listed = await self.list_course_lectures(
            course_query,
            week,
            only_unwatched,
            year,
            semester,
        )
        if not listed.ok or listed.data is None:
            return LectureResolutionResult(
                ok=False,
                status=listed.status,
                selected_term=listed.selected_term,
                error=listed.error,
                course_candidates=listed.candidates,
            )

        candidates = listed.data.lectures
        if title_query is not None:
            candidates = filter_lectures_by_query(title_query, candidates)
        if not candidates:
            return LectureResolutionResult(
                ok=False,
                status=McpOutcomeStatus.NOT_FOUND,
                selected_term=listed.selected_term,
                error=McpToolError(
                    code=McpErrorCode.NOT_FOUND,
                    message="조건과 일치하는 강의 영상을 찾을 수 없습니다.",
                    retryable=False,
                ),
            )
        if len(candidates) > 1:
            return LectureResolutionResult(
                ok=False,
                status=McpOutcomeStatus.AMBIGUOUS,
                selected_term=listed.selected_term,
                error=McpToolError(
                    code=McpErrorCode.AMBIGUOUS_MATCH,
                    message="조건과 일치하는 강의가 여러 개입니다. 제목을 더 구체적으로 지정해 주세요.",
                    retryable=False,
                ),
                candidates=candidates[:50],
            )

        lecture = candidates[0]
        verified_at = utc_now()
        target = VerifiedLectureTarget(
            reference_id=str(uuid4()),
            lecture_id=lecture.id,
            course_id=lecture.course_id,
            course_name=listed.data.course.course_name,
            title=lecture.title,
            week=lecture.week,
            year=listed.data.course.year,
            semester=listed.data.course.semester,
            verified_at=verified_at,
            expires_at=verified_at + self._verified_target_ttl,
        )
        self._prune_verified_lecture_targets(verified_at)
        self._verified_lecture_targets[target.reference_id] = target
        return LectureResolutionResult(
            ok=True,
            status=McpOutcomeStatus.FOUND,
            selected_term=listed.selected_term,
            data=target,
        )

    def get_verified_lecture_target(self, reference_id: str) -> VerifiedLectureTarget | None:
        """유효한 resolve_lecture 결과만 반환하고 만료·조작된 참조는 거부한다."""

        now = utc_now()
        self._prune_verified_lecture_targets(now)
        return self._verified_lecture_targets.get(reference_id)

    async def get_lecture_status(
        self, lecture_id: str, year: int | None, semester: int | None
    ) -> LectureStatusResult:
        invalid = self._validate_term(year, semester)
        if invalid:
            return self._bad_request(LectureStatusResult, invalid)
        return await self._execute_term(
            LectureStatusResult,
            lambda: self.adapter.get_lecture_status(
                lecture_id,
                year=year,
                semester=semester,
            ),
        )

    async def get_grades(
        self, course_id: str | None, year: int | None, semester: int | None
    ) -> GradeListResult:
        invalid = self._validate_term(year, semester)
        if invalid:
            return self._bad_request(GradeListResult, invalid)
        return await self._execute_term(
            GradeListResult,
            lambda: self.adapter.get_grades(
                course_id=course_id,
                year=year,
                semester=semester,
            ),
        )

    @staticmethod
    def _verified_course(course: Course) -> VerifiedCourseReference:
        """Adapter가 실제 화면에서 읽은 Course를 고수준 Tool 참조로 좁힌다."""

        return VerifiedCourseReference(
            course_id=course.id,
            course_name=course.name,
            professor=course.professor,
            year=course.year,
            semester=course.semester,
        )

    @staticmethod
    def _selected_term_values(
        selected_term: SelectedTerm | None,
        course: Course,
    ) -> tuple[int, int]:
        """후속 조회가 E-Class 기본 학기로 되돌아가지 않게 검증 학기를 명시한다."""

        if selected_term is not None:
            return selected_term.year, selected_term.semester
        return course.year, course.semester

    @classmethod
    def _course_scoped_failure(
        cls,
        response_type: type[ResponseT],
        resolution: CourseResolutionResult,
    ) -> ResponseT | None:
        """강좌 해석 실패를 모든 고수준 목록 Tool의 동일한 상태 계약으로 바꾼다."""

        if not resolution.ok or resolution.data is None:
            return response_type(
                ok=False,
                status=cls._outcome_from_error(resolution.error),
                selected_term=resolution.selected_term,
                error=resolution.error,
            )
        if resolution.data.status == "AMBIGUOUS":
            return response_type(
                ok=False,
                status=McpOutcomeStatus.AMBIGUOUS,
                selected_term=resolution.selected_term,
                error=McpToolError(
                    code=McpErrorCode.AMBIGUOUS_MATCH,
                    message="강좌 검색 결과가 여러 개입니다. 후보 이름을 더 구체적으로 지정해 주세요.",
                    retryable=False,
                ),
                candidates=resolution.data.candidates,
            )
        if resolution.data.status != "MATCHED" or resolution.data.course is None:
            return response_type(
                ok=False,
                status=McpOutcomeStatus.NOT_FOUND,
                selected_term=resolution.selected_term,
                error=McpToolError(
                    code=McpErrorCode.NOT_FOUND,
                    message="선택 학기에서 일치하는 강좌를 찾을 수 없습니다.",
                    retryable=False,
                ),
            )
        return None

    @staticmethod
    def _outcome_from_error(error: McpToolError | None) -> McpOutcomeStatus:
        """기존 오류 코드를 새 업무 단위 상태로 손실 없이 대응시킨다."""

        if error is None:
            return McpOutcomeStatus.TEMPORARY_FAILURE
        return {
            McpErrorCode.INVALID_REQUEST: McpOutcomeStatus.INVALID_REQUEST,
            McpErrorCode.AUTH_REQUIRED: McpOutcomeStatus.AUTH_REQUIRED,
            McpErrorCode.NOT_FOUND: McpOutcomeStatus.NOT_FOUND,
            McpErrorCode.AMBIGUOUS_MATCH: McpOutcomeStatus.AMBIGUOUS,
            McpErrorCode.PARSER_CHANGED: McpOutcomeStatus.PARSER_CHANGED,
            McpErrorCode.TEMPORARY_FAILURE: McpOutcomeStatus.TEMPORARY_FAILURE,
        }[error.code]

    def _prune_verified_lecture_targets(self, now: datetime | None = None) -> None:
        """만료된 재생 참조가 MCP 서버 메모리에 남지 않게 제거한다."""

        current = now if now is not None else utc_now()
        expired = [
            reference_id
            for reference_id, target in self._verified_lecture_targets.items()
            if target.expires_at <= current
        ]
        for reference_id in expired:
            self._verified_lecture_targets.pop(reference_id, None)

    async def _execute_term(
        self,
        response_type: type[ResponseT],
        operation: Callable[[], Awaitable[TermScopedData[DataT]]],
    ) -> ResponseT:
        """학기 범위 결과의 데이터와 실제 선택 학기를 응답에 보존한다."""

        return await self._execute(
            response_type,
            operation,
            transform=lambda scoped: scoped.data,
            selected_term=lambda scoped: scoped.selected_term,
        )

    async def _execute(
        self,
        response_type: type[ResponseT],
        operation: Callable[[], Awaitable[DataT]],
        *,
        transform: Callable[[DataT], object] | None = None,
        selected_term: Callable[[DataT], object] | None = None,
    ) -> ResponseT:
        """예외 원문을 숨기고 Agent가 판단할 수 있는 네 종류로만 반환한다."""

        async with self.locks.for_user(self.user_id):
            try:
                raw = await operation()
                data = transform(raw) if transform else raw
                term = selected_term(raw) if selected_term else None
                payload: dict[str, object] = {
                    "ok": True,
                    "data": data,
                    "selected_term": term,
                }
                if issubclass(response_type, SemanticMcpResponse):
                    payload["status"] = McpOutcomeStatus.FOUND
                return response_type(**payload)
            except AuthRequiredError:
                return self._failure(
                    response_type,
                    McpErrorCode.AUTH_REQUIRED,
                    "E-Class 로그인이 필요합니다.",
                    retryable=False,
                )
            except EclassNotFoundError:
                return self._failure(
                    response_type,
                    McpErrorCode.NOT_FOUND,
                    "요청한 E-Class 항목을 찾을 수 없습니다.",
                    retryable=False,
                )
            except (EclassParserChangedError, ValidationError, ValueError):
                return self._failure(
                    response_type,
                    McpErrorCode.PARSER_CHANGED,
                    "E-Class 화면 구조를 해석하지 못했습니다.",
                    retryable=False,
                )
            except (EclassTemporaryError, PlaywrightTimeoutError, TimeoutError):
                return self._failure(
                    response_type,
                    McpErrorCode.TEMPORARY_FAILURE,
                    "E-Class 연결이 일시적으로 실패했습니다.",
                    retryable=True,
                )
            except Exception:
                # 비밀번호·쿠키·HTML·Playwright 로그가 MCP 응답에 섹 수 있어
                # 예상하지 못한 예외도 안전한 일시 오류로만 바꿔 반환한다.
                return self._failure(
                    response_type,
                    McpErrorCode.TEMPORARY_FAILURE,
                    "E-Class 작업 중 일시적인 오류가 발생했습니다.",
                    retryable=True,
                )

    @classmethod
    def _failure(
        cls,
        response_type: type[ResponseT],
        code: McpErrorCode,
        message: str,
        *,
        retryable: bool,
    ) -> ResponseT:
        error = McpToolError(code=code, message=message, retryable=retryable)
        payload: dict[str, object] = {"ok": False, "error": error}
        # 새 고수준 응답은 실패 시에도 status가 반드시 오류 코드와 일치해야 한다.
        # 기존 저수준 McpResponse에는 이 필드를 추가하지 않는다.
        if issubclass(response_type, SemanticMcpResponse):
            payload["status"] = cls._outcome_from_error(error)
        return response_type(**payload)

    @classmethod
    def _bad_request(cls, response_type: type[ResponseT], message: str) -> ResponseT:
        return cls._failure(
            response_type,
            McpErrorCode.INVALID_REQUEST,
            message,
            retryable=False,
        )

    @staticmethod
    def _validate_term(year: int | None, semester: int | None) -> str | None:
        if (year is None) != (semester is None):
            return "연도와 학기는 함께 지정해 주세요."
        if year is not None and not 2000 <= year <= 2100:
            return "연도는 2000~2100으로 입력해 주세요."
        if semester is not None and semester not in {1, 2, 3, 4}:
            return "학기는 1(신학기), 2(2학기), 3(여름), 4(겨울) 중 하나입니다."
        return None
