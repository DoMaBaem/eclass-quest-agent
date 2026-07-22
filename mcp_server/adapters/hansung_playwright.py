"""한성 e-Class DOM을 Playwright로 조작하는 Live Adapter.

브라우저 수명은 Worker에, 선택자 문자열은 selectors.py에, 반환 형식은 Pydantic Course에 맡긴다.
이 파일은 그 세 요소를 연결해 실제 페이지 이동과 정규화 순서만 담당한다.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Literal, TypeVar
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from app.config import Settings
from app.schemas.domain import (
    Announcement,
    AnnouncementDetails,
    Assignment,
    Attachment,
    Course,
    EntityStatus,
    Grade,
    Lecture,
)
from mcp_server.adapters.base import EclassAdapter, TermScopedData
from mcp_server.browser.selectors import HansungSelectors
from mcp_server.browser.credential_login import automatic_login_available, refresh_encrypted_session
from mcp_server.browser.language import with_eclass_language
from mcp_server.browser.session import AuthRequiredError
from mcp_server.browser.worker import EclassBrowserWorker
from mcp_server.errors import EclassNotFoundError, EclassParserChangedError, EclassTemporaryError
from mcp_server.parsers.common import query_id
from mcp_server.parsers.course_content import (
    current_course_id,
    find_course_notice_board_url,
    merge_attendance_status,
    parse_announcement_details,
    parse_announcements_board,
    parse_assignment_attachments,
    parse_assignment_details,
    parse_assignments_index,
    parse_grades_page,
    parse_lectures_index,
)
from mcp_server.schemas import SelectedTerm


class SelectorChangedError(EclassParserChangedError):
    """LMS 화면 구조가 바뀌어 안전하게 정규화할 수 없는 경우."""


ResultT = TypeVar("ResultT")


class HansungPlaywrightAdapter(EclassAdapter):
    """암호화 세션을 사용해 한성 e-Class에 접근하는 Adapter 구현체."""

    # 한성 e-Class의 학기 query 값은 사용자에게 보이는 1~4와 다르다.
    # 1=1학기, 2=2학기, 3=여름학기, 4=겨울학기로 서비스 내부에서 통일한다.
    SEMESTER_QUERY_VALUES = {1: "10", 2: "20", 3: "15", 4: "25"}
    QUERY_VALUE_SEMESTERS = {value: key for key, value in SEMESTER_QUERY_VALUES.items()}

    def __init__(self, settings: Settings, *, headless: bool = True) -> None:
        self.settings = settings
        self.worker = EclassBrowserWorker(settings, headless=headless)

    async def ensure_login(self) -> None:
        """현재 세션을 확인하고 만료됐다면 자동 로그인 후 한 번 재시도한다."""

        async def check(page: Page) -> None:
            # 순서가 중요하다: 인증 확인 → 메뉴 이동 → 필터 선택 → 데이터 정규화.
            await self._ensure_page_authenticated(page)

        await self._run_with_auto_relogin(check)

    async def list_courses(
        self, *, year: int | None, semester: int | None
    ) -> TermScopedData[list[Course]]:
        """학기를 지정하거나 E-Class의 기본 선택 학기 강좌를 반환한다."""

        async def collect(page: Page) -> TermScopedData[list[Course]]:
            return await self._list_courses_on_page(page, year=year, semester=semester)

        return await self._run_with_auto_relogin(collect)

    async def list_announcements(
        self,
        *,
        course_id: str | None,
        limit: int,
        year: int | None,
        semester: int | None,
    ) -> TermScopedData[list[Announcement]]:
        """학교 공지와 현재 학기 강좌 공지를 최신순으로 반환한다."""

        async def collect(page: Page) -> TermScopedData[list[Announcement]]:
            scoped_courses = await self._resolve_courses(page, course_id, year=year, semester=semester)
            courses = scoped_courses.data
            announcements: list[Announcement] = []
            if course_id is None:
                await self._goto(page, "/mod/ubboard/view.php?id=1")
                announcements.extend(await parse_announcements_board(page, course_id=None, limit=limit))
            for course in courses:
                await self._goto(page, f"/mod/ubboard/index.php?id={course.id}")
                board_url = await find_course_notice_board_url(page)
                if board_url is None:
                    continue
                await self._goto(page, board_url)
                announcements.extend(
                    await parse_announcements_board(page, course_id=course.id, limit=limit)
                )
            unique = {item.id: item for item in announcements}
            return TermScopedData(
                data=sorted(
                    unique.values(),
                    key=lambda item: item.posted_at.timestamp() if item.posted_at else 0,
                    reverse=True,
                )[:limit],
                selected_term=scoped_courses.selected_term,
            )

        return await self._run_with_auto_relogin(collect)

    async def get_announcement_details(
        self,
        announcement_url: str,
        *,
        course_id: str | None,
        year: int | None,
        semester: int | None,
    ) -> TermScopedData[AnnouncementDetails]:
        """목록에서 받은 E-Class URL을 열어 공지 본문을 반환한다."""

        parsed = urlparse(announcement_url)
        base = urlparse(str(self.settings.eclass_base_url))
        if parsed.scheme not in {"http", "https"} or parsed.netloc != base.netloc:
            raise EclassNotFoundError("E-Class 공지 주소가 아닙니다.")
        if parsed.path != "/mod/ubboard/article.php":
            raise EclassNotFoundError("공지 상세 주소가 아닙니다.")
        announcement_id = query_id(announcement_url, "bwid")
        board_id = query_id(announcement_url, "id")
        if announcement_id is None or board_id is None:
            raise EclassNotFoundError("공지 상세 ID를 확인할 수 없습니다.")
        self._validate_numeric_id(announcement_id, "공지")
        self._validate_numeric_id(board_id, "공지 게시판")
        if course_id is not None:
            self._validate_numeric_id(course_id, "강좌")

        async def collect(page: Page) -> TermScopedData[AnnouncementDetails]:
            await self._goto(page, announcement_url)
            details = await parse_announcement_details(page, announcement_id)
            if course_id is not None:
                if details.course_id != course_id:
                    raise EclassNotFoundError("요청한 강좌의 공지가 아닙니다.")
                selected_term = await self._ensure_course_allowed(
                    page, course_id, year=year, semester=semester
                )
            else:
                scoped_courses = await self._list_courses_on_page(
                    page, year=year, semester=semester
                )
                allowed_ids = {course.id for course in scoped_courses.data}
                # id=1은 학교 공지 게시판이다. 그 외 강좌 공지는 선택 학기 수강 강좌만 허용한다.
                if board_id != "1" and details.course_id not in allowed_ids:
                    raise EclassNotFoundError("선택 학기의 수강 강좌 공지가 아닙니다.")
                selected_term = scoped_courses.selected_term
            return TermScopedData(data=details, selected_term=selected_term)

        return await self._run_with_auto_relogin(collect)

    async def list_assignments(
        self,
        *,
        course_id: str | None,
        days: int | None,
        only_incomplete: bool,
        year: int | None,
        semester: int | None,
    ) -> TermScopedData[list[Assignment]]:
        """선택된 학기의 전체 또는 지정 강좌 과제를 제출·마감 필터와 함께 반환한다."""

        async def collect(page: Page) -> TermScopedData[list[Assignment]]:
            scoped_courses = await self._resolve_courses(
                page,
                course_id,
                year=year,
                semester=semester,
            )
            courses = scoped_courses.data
            assignments: list[Assignment] = []
            for course in courses:
                await self._goto(page, f"/mod/assign/index.php?id={course.id}")
                assignments.extend(await parse_assignments_index(page, course))
            if only_incomplete:
                assignments = [item for item in assignments if item.status is not EntityStatus.COMPLETE]
            if days is not None:
                now = datetime.now(timezone.utc)
                cutoff = now + timedelta(days=days)
                assignments = [
                    item for item in assignments if item.due_at is not None and now <= item.due_at <= cutoff
                ]
            return TermScopedData(
                data=sorted(
                    assignments,
                    key=lambda item: item.due_at.timestamp() if item.due_at else float("inf"),
                ),
                selected_term=scoped_courses.selected_term,
            )

        return await self._run_with_auto_relogin(collect)

    async def get_assignment_details(
        self, assignment_id: str, *, year: int | None, semester: int | None
    ) -> TermScopedData[Assignment]:
        """과제 상세 페이지의 제출 여부·마감·최종 제출 시각을 반환한다."""

        self._validate_numeric_id(assignment_id, "과제")

        async def collect(page: Page) -> TermScopedData[Assignment]:
            await self._goto(page, f"/mod/assign/view.php?id={assignment_id}")
            details = await parse_assignment_details(page, assignment_id)
            selected_term = await self._ensure_course_allowed(
                page, details.course_id, year=year, semester=semester
            )
            return TermScopedData(data=details, selected_term=selected_term)

        return await self._run_with_auto_relogin(collect)

    async def list_assignment_attachments(
        self, assignment_id: str, *, year: int | None, semester: int | None
    ) -> TermScopedData[list[Attachment]]:
        """과제 상세 화면에 공개된 첨부파일 메타데이터만 반환한다."""

        self._validate_numeric_id(assignment_id, "과제")

        async def collect(page: Page) -> TermScopedData[list[Attachment]]:
            await self._goto(page, f"/mod/assign/view.php?id={assignment_id}")
            details = await parse_assignment_details(page, assignment_id)
            attachments = await parse_assignment_attachments(page, assignment_id)
            selected_term = await self._ensure_course_allowed(
                page, details.course_id, year=year, semester=semester
            )
            return TermScopedData(data=attachments, selected_term=selected_term)

        return await self._run_with_auto_relogin(collect)

    async def list_lectures(
        self,
        *,
        course_id: str | None,
        only_unwatched: bool,
        year: int | None,
        semester: int | None,
    ) -> TermScopedData[list[Lecture]]:
        """선택된 학기 강의 영상과 온라인출석부의 시청 상태를 결합한다."""

        async def collect(page: Page) -> TermScopedData[list[Lecture]]:
            scoped_courses = await self._resolve_courses(page, course_id, year=year, semester=semester)
            courses = scoped_courses.data
            lectures: list[Lecture] = []
            for course in courses:
                lectures.extend(await self._load_course_lectures(page, course))
            if only_unwatched:
                lectures = [item for item in lectures if item.status is not EntityStatus.COMPLETE]
            return TermScopedData(data=lectures, selected_term=scoped_courses.selected_term)

        return await self._run_with_auto_relogin(collect)

    async def get_lecture_status(
        self, lecture_id: str, *, year: int | None, semester: int | None
    ) -> TermScopedData[Lecture]:
        """강의 ID로 소속 강좌를 확인한 뒤 온라인출석부 상태를 반환한다."""

        self._validate_numeric_id(lecture_id, "강의")

        async def collect(page: Page) -> TermScopedData[Lecture]:
            await self._goto(page, f"/mod/vod/view.php?id={lecture_id}")
            course_id = await current_course_id(page)
            scoped_courses = await self._resolve_courses(
                page, course_id, year=year, semester=semester
            )
            lectures = await self._load_course_lectures(page, scoped_courses.data[0])
            for lecture in lectures:
                if lecture.id == lecture_id:
                    return TermScopedData(data=lecture, selected_term=scoped_courses.selected_term)
            raise EclassNotFoundError("강의 영상을 찾을 수 없습니다.")

        return await self._run_with_auto_relogin(collect)

    async def get_grades(
        self, *, course_id: str | None, year: int | None, semester: int | None
    ) -> TermScopedData[list[Grade]]:
        """공개된 성적만 반환하며 비공개 안내는 정상적인 빈 목록으로 처리한다."""

        async def collect(page: Page) -> TermScopedData[list[Grade]]:
            scoped_courses = await self._resolve_courses(page, course_id, year=year, semester=semester)
            courses = scoped_courses.data
            grades: list[Grade] = []
            for course in courses:
                try:
                    await self._goto(page, f"/grade/report/user/index.php?id={course.id}")
                except EclassNotFoundError:
                    # 강좌 설정에서 성적부를 비활성화하면 Moodle이 404를 반환한다.
                    # 강좌 자체는 위의 allowlist에서 확인했으므로 빈 성적으로 처리한다.
                    continue
                grades.extend(await parse_grades_page(page, course.id))
            return TermScopedData(data=grades, selected_term=scoped_courses.selected_term)

        return await self._run_with_auto_relogin(collect)

    async def _list_courses_on_page(
        self, page: Page, *, year: int | None, semester: int | None
    ) -> TermScopedData[list[Course]]:
        await self._ensure_page_authenticated(page)
        await self._open_my_courses(page)
        if year is None and semester is None:
            selected_term = await self._read_selected_term(page, source="eclass_default")
        else:
            if year is None or semester is None:
                raise ValueError("연도와 학기는 함께 지정해야 합니다.")
            await self._select_term(page, year=year, semester=semester)
            selected_term = SelectedTerm(
                year=year,
                semester=semester,
                selection_source="user_request",
            )
        courses = await self._parse_courses(
            page,
            year=selected_term.year,
            semester=selected_term.semester,
        )
        # 방학·신학기 전환 기간의 0개 강좌는 파서 오류가 아니다.
        return TermScopedData(data=courses, selected_term=selected_term)

    async def _resolve_courses(
        self,
        page: Page,
        course_id: str | None,
        *,
        year: int | None,
        semester: int | None,
    ) -> TermScopedData[list[Course]]:
        scoped_courses = await self._list_courses_on_page(page, year=year, semester=semester)
        courses = scoped_courses.data
        if course_id is None:
            return scoped_courses
        self._validate_numeric_id(course_id, "강좌")
        matched = [course for course in courses if course.id == course_id]
        if not matched:
            raise EclassNotFoundError("현재 학기 수강 강좌를 찾을 수 없습니다.")
        return TermScopedData(data=matched, selected_term=scoped_courses.selected_term)

    async def _ensure_course_allowed(
        self,
        page: Page,
        course_id: str,
        *,
        year: int | None,
        semester: int | None,
    ) -> SelectedTerm:
        scoped = await self._resolve_courses(page, course_id, year=year, semester=semester)
        return scoped.selected_term

    async def _load_course_lectures(self, page: Page, course: Course) -> list[Lecture]:
        await self._goto(page, f"/mod/vod/index.php?id={course.id}")
        lectures = await parse_lectures_index(page, course)
        await self._goto(page, f"/report/ubcompletion/progress.php?id={course.id}")
        return await merge_attendance_status(page, lectures)

    async def _goto(self, page: Page, target: str) -> None:
        url = urljoin(str(self.settings.eclass_base_url), target)
        base = urlparse(str(self.settings.eclass_base_url))
        destination = urlparse(url)
        if destination.scheme not in {"http", "https"} or destination.netloc != base.netloc:
            raise EclassNotFoundError("E-Class 외부 주소로는 이동할 수 없습니다.")
        url = with_eclass_language(url, self.settings.eclass_default_language)
        response = await page.goto(url, wait_until="domcontentloaded")
        if self._looks_like_login_url(page.url):
            raise AuthRequiredError("E-Class 세션이 만료되었습니다.")
        if response is not None and response.status == 404:
            raise EclassNotFoundError("요청한 E-Class 항목을 찾을 수 없습니다.")
        if response is not None and response.status >= 500:
            raise EclassTemporaryError("E-Class 서버가 일시적으로 응답하지 않습니다.")

    @staticmethod
    def _validate_numeric_id(value: str, label: str) -> None:
        if not re.fullmatch(r"\d{1,20}", value):
            raise EclassNotFoundError(f"올바른 {label} ID가 아닙니다.")

    async def _run_with_auto_relogin(
        self,
        operation: Callable[[Page], Awaitable[ResultT]],
    ) -> ResultT:
        """인증 오류일 때만 자격증명으로 세션을 갱신하고 원래 작업을 정확히 한 번 재시도한다."""

        try:
            return await self._run_authenticated_once(operation)
        except AuthRequiredError:
            if not automatic_login_available(self.settings):
                raise
            await refresh_encrypted_session(self.settings)
            # 두 번째 인증 오류는 그대로 올려 무한 로그인·재시도를 막는다.
            return await self._run_authenticated_once(operation)

    async def _run_authenticated_once(
        self,
        operation: Callable[[Page], Awaitable[ResultT]],
    ) -> ResultT:
        """현재 암호화 세션으로 Page 하나를 열어 전달받은 LMS 작업을 실행한다."""

        async with self.worker.authenticated_page() as page:
            return await operation(page)

    async def _ensure_page_authenticated(self, page: Page) -> None:
        """로그인 URL·로그아웃 표식·비밀번호 입력창을 함께 보고 인증 여부를 판정한다."""

        await page.goto(
            with_eclass_language(
                str(self.settings.eclass_base_url), self.settings.eclass_default_language
            ),
            wait_until="domcontentloaded",
        )
        await page.wait_for_timeout(500)
        if self._looks_like_login_url(page.url):
            raise AuthRequiredError("E-Class 세션이 만료되었습니다. 실행 명령에 --setup을 붙여 다시 로그인하세요.")

        # URL만으로 로그인 완료를 단정하지 않는다. 로그아웃 표식을 우선 확인한다.
        if await self._has_any_visible(page, HansungSelectors.LOGIN_SUCCESS):
            return
        # SSO가 로그아웃 텍스트를 숨기는 경우를 위해 로그인 폼이 없는 대시보드만 보조적으로 허용한다.
        login_inputs = page.locator("input[type='password'], input[name*='password' i]")
        if await login_inputs.count() > 0:
            raise AuthRequiredError("E-Class 로그인 화면이 표시되었습니다. 실행 명령에 --setup을 붙여 다시 로그인하세요.")

    async def _open_my_courses(self, page: Page) -> None:
        """언어와 메뉴 표시 상태에 영향받지 않도록 정규 수강 강좌 URL로 이동한다."""

        courses_url = urljoin(str(self.settings.eclass_base_url), "/local/ubion/user/")
        await page.goto(
            with_eclass_language(courses_url, self.settings.eclass_default_language),
            wait_until="domcontentloaded",
        )
        if self._looks_like_login_url(page.url):
            raise AuthRequiredError("E-Class 세션이 만료되었습니다. 실행 명령에 --setup을 붙여 다시 로그인하세요.")

    async def _read_selected_term(
        self,
        page: Page,
        *,
        source: Literal["eclass_default", "user_request"],
    ) -> SelectedTerm:
        """E-Class가 필터에 기본으로 선택한 연도·학기를 읽는다."""

        year_select = page.locator(HansungSelectors.YEAR_SELECT[0]).first
        semester_select = page.locator(HansungSelectors.SEMESTER_SELECT[0]).first
        if not await year_select.count() or not await semester_select.count():
            raise SelectorChangedError("연도·학기 선택자를 찾지 못했습니다.")

        year_value = await year_select.input_value()
        semester_value = await semester_select.input_value()
        if not re.fullmatch(r"\d{4}", year_value):
            raise SelectorChangedError("E-Class 선택 연도를 해석하지 못했습니다.")
        semester = self.QUERY_VALUE_SEMESTERS.get(semester_value)
        if semester is None:
            raise SelectorChangedError("E-Class 선택 학기를 해석하지 못했습니다.")
        return SelectedTerm(
            year=int(year_value),
            semester=semester,
            selection_source=source,
        )

    async def _select_term(self, page: Page, *, year: int, semester: int) -> None:
        """한성 e-Class의 실제 query 값으로 학기 페이지를 열고 필터 적용 여부를 검증한다."""

        semester_value = self.SEMESTER_QUERY_VALUES.get(semester)
        if semester_value is None:
            raise ValueError("semester는 1(1학기), 2(2학기), 3(여름), 4(겨울) 중 하나여야 합니다.")
        query = urlencode({"year": year, "semester": semester_value})
        term_url = urljoin(str(self.settings.eclass_base_url), f"/local/ubion/user/?{query}")
        await page.goto(
            with_eclass_language(term_url, self.settings.eclass_default_language),
            wait_until="domcontentloaded",
        )
        if self._looks_like_login_url(page.url):
            raise AuthRequiredError("E-Class 세션이 만료되었습니다. 실행 명령에 --setup을 붙여 다시 로그인하세요.")

        year_select = page.locator(HansungSelectors.YEAR_SELECT[0]).first
        semester_select = page.locator(HansungSelectors.SEMESTER_SELECT[0]).first
        if not await year_select.count() or not await semester_select.count():
            raise SelectorChangedError("연도·학기 선택자를 찾지 못했습니다. selectors.py를 갱신하세요.")
        if await year_select.input_value() != str(year) or await semester_select.input_value() != semester_value:
            raise SelectorChangedError("요청한 연도·학기 필터가 적용되지 않았습니다.")

    async def _parse_courses(self, page: Page, *, year: int, semester: int) -> list[Course]:
        """강좌 링크를 Course 모델로 바꾸고 같은 ID가 여러 번 보이면 하나로 합친다."""

        courses_by_id: dict[str, Course] = {}
        for selector in HansungSelectors.COURSE_LINKS:
            locator = page.locator(selector)
            for index in range(await locator.count()):
                link = locator.nth(index)
                href = await link.get_attribute("href")
                name = (await link.inner_text()).strip()
                if not href or not name:
                    continue
                course_id = self._course_id_from_url(href)
                if course_id is None:
                    continue
                # 화면의 연속 공백·줄바꿈을 한 칸으로 정규화해 fingerprint 변동을 줄인다.
                row = link.locator("xpath=ancestor::tr[1]")
                professor: str | None = None
                if await row.count():
                    cells = row.locator("td")
                    if await cells.count() >= 3:
                        professor_text = " ".join((await cells.nth(2).inner_text()).split())
                        professor = professor_text or None
                courses_by_id[course_id] = Course(
                    id=course_id,
                    name=" ".join(name.split()),
                    professor=professor,
                    url=urljoin(page.url, href),
                    year=year,
                    semester=semester,
                )
        return list(courses_by_id.values())

    @staticmethod
    def _course_id_from_url(href: str) -> str | None:
        """query string을 우선 보고, 없으면 URL path에서 LMS 강좌 ID를 찾는다."""

        parsed = urlparse(href)
        query = parse_qs(parsed.query)
        for key in ("course_id", "courseId", "id", "course"):
            if query.get(key, [None])[0]:
                return query[key][0]
        matched = re.search(r"/(?:course|courses|lecture|class)/([^/?#]+)", parsed.path, flags=re.IGNORECASE)
        if matched:
            return matched.group(1)
        return None

    @staticmethod
    def _looks_like_login_url(url: str) -> bool:
        """SSO 또는 일반 로그인 URL인지 빠르게 판정한다."""

        return "login" in url.lower() or "sso" in url.lower()

    @staticmethod
    async def _has_any_visible(page: Page, selectors: tuple[str, ...]) -> bool:
        """후보 중 하나라도 존재하고 보이면 True를 반환한다."""

        for selector in selectors:
            locator = page.locator(selector).first
            try:
                if await locator.count() and await locator.is_visible():
                    return True
            except PlaywrightTimeoutError:
                continue
        return False
