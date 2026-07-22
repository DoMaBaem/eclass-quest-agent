"""과제·강의·공지·성적 화면을 도메인 Pydantic 모델로 정규화한다."""

from __future__ import annotations

import hashlib
import html
import mimetypes
import re
import unicodedata
from collections import defaultdict, deque
from datetime import datetime
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from playwright.async_api import Page

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
from mcp_server.errors import EclassNotFoundError, EclassParserChangedError
from mcp_server.parsers.common import (
    duration_seconds,
    normalize_text,
    parse_eclass_datetime,
    parse_week,
    parse_week_window,
    query_id,
)


async def current_course_id(page: Page) -> str:
    """body의 Moodle course context class에서 현재 강좌 ID를 읽는다."""

    body_class = await page.locator("body").get_attribute("class") or ""
    matched = re.search(r"\bcourse-(\d+)\b", body_class)
    if matched:
        return matched.group(1)
    raise EclassParserChangedError("강좌 컨텍스트를 확인할 수 없습니다.")


async def parse_assignments_index(page: Page, course: Course) -> list[Assignment]:
    table = page.locator("table.generaltable").first
    if not await table.count():
        # Moodle은 과제가 0개인 강좌에서 표 자체를 렌더링하지 않는다. 한성 e-Class의
        # 표시 언어에 따라 빈 상태 문구가 한국어 또는 영어로 나오므로 제목 한글만 검사하면
        # 정상적인 빈 강좌를 화면 변경 오류로 잘못 판단하게 된다.
        is_assignment_page = await page.locator("body.path-mod-assign").count()
        assignment_links = page.locator("a[href*='/mod/assign/view.php?id=']")
        main = page.locator("#region-main, main").first
        main_text = normalize_text(await main.inner_text()) if await main.count() else ""
        if _is_empty_assignment_page(
            bool(is_assignment_page),
            await assignment_links.count(),
            main_text,
        ):
            return []
        raise EclassParserChangedError("과제 목록 표를 찾을 수 없습니다.")

    header_nodes = table.locator("thead th")
    if not await header_nodes.count():
        header_nodes = table.locator("tr:first-child th")
    headers = [
        normalize_text(await header_nodes.nth(index).inner_text())
        for index in range(await header_nodes.count())
    ]
    columns = _assignment_header_indexes(headers)

    assignments: list[Assignment] = []
    rows = table.locator("tbody tr")
    expected_link_count = await table.locator("a[href*='/mod/assign/view.php?id=']").count()
    current_week: int | None = None
    for index in range(await rows.count()):
        row = rows.nth(index)
        link = row.locator("a[href*='/mod/assign/view.php?id=']").first
        if not await link.count():
            continue
        href = await link.get_attribute("href")
        if not href:
            continue
        assignment_id = query_id(href, "id")
        if assignment_id is None:
            continue
        cells = row.locator("td")
        texts = [normalize_text(await cells.nth(i).inner_text()) for i in range(await cells.count())]
        if len(texts) <= max(columns.values()):
            raise EclassParserChangedError("과제 목록 열 구조가 변경되었습니다.")
        week_index = columns.get("week")
        if week_index is not None:
            current_week = _assignment_row_week(texts[week_index], current_week)
        submitted_text = texts[columns["submitted"]].lower()
        submitted = _submission_boolean(submitted_text)
        assignments.append(
            Assignment(
                id=assignment_id,
                course_id=course.id,
                course_name=course.name,
                title=normalize_text(await link.inner_text()),
                url=urljoin(page.url, href),
                week=current_week,
                due_at=parse_eclass_datetime(texts[columns["due"]]),
                submitted=submitted,
                status=(
                    EntityStatus.COMPLETE
                    if submitted is True
                    else EntityStatus.INCOMPLETE
                    if submitted is False
                    else EntityStatus.UNKNOWN
                ),
            )
        )
    # 링크는 보였는데 일부 행만 조용히 누락되는 것이 잘못된 "과제 없음"보다 위험하다.
    if len(assignments) != expected_link_count:
        raise EclassParserChangedError("과제 링크 수와 파싱 결과 수가 일치하지 않습니다.")
    return assignments


async def parse_assignment_details(page: Page, assignment_id: str) -> Assignment:
    course_id = await current_course_id(page)
    self_link = page.locator(f"a[href*='/mod/assign/view.php?id={assignment_id}']").first
    if not await self_link.count():
        raise EclassNotFoundError("과제를 찾을 수 없습니다.")
    title = normalize_text(await self_link.inner_text())
    table = page.locator("table.generaltable").first
    if not await table.count():
        raise EclassParserChangedError("과제 상세 상태 표를 찾을 수 없습니다.")

    values: dict[str, str] = {}
    rows = table.locator("tr")
    for index in range(await rows.count()):
        cells = rows.nth(index).locator("th, td")
        if await cells.count() >= 2:
            key = normalize_text(await cells.nth(0).inner_text())
            value = normalize_text(await cells.nth(1).inner_text())
            values[key] = value

    submitted_text = _value_for_labels(values, "제출 여부", "Submission status") or ""
    submitted = _submission_boolean(submitted_text.lower())
    due_text = _value_for_labels(values, "종료 일시", "Due date") or ""
    modified_text = _value_for_labels(values, "최종 수정 일시", "Last modified") or ""
    intro = page.locator("#intro").first
    description: str | None = None
    if await intro.count():
        raw_description = await intro.inner_text()
        preserved = "\n".join(
            line
            for line in (normalize_text(raw_line) for raw_line in raw_description.splitlines())
            if line
        )
        description = preserved[:50_000] or None
    return Assignment(
        id=assignment_id,
        course_id=course_id,
        title=title,
        url=page.url,
        description=description,
        due_at=parse_eclass_datetime(due_text),
        submitted=submitted,
        submitted_at=parse_eclass_datetime(modified_text) if submitted else None,
        status=(
            EntityStatus.COMPLETE
            if submitted is True
            else EntityStatus.INCOMPLETE
            if submitted is False
            else EntityStatus.UNKNOWN
        ),
    )


def _is_assignment_intro_attachment_url(candidate_url: str, page_url: str) -> bool:
    """같은 E-Class의 과제 설명 첨부 URL인지 Moodle filearea까지 확인한다.

    한성 E-Class는 ``/mod_assign/introattachment/``를 URL path에 그대로 넣는 경우뿐 아니라
    ``pluginfile.php?file=%2F...%2Fmod_assign%2Fintroattachment%2F...``처럼 query 안에
    percent-encoding해 넣기도 한다. CSS substring 선택자는 후자를 볼 수 없으므로 URL을
    해석한 뒤 component/filearea를 판정한다.
    """

    try:
        candidate = urlparse(candidate_url)
        page = urlparse(page_url)
        same_origin = (
            candidate.scheme.casefold(),
            (candidate.hostname or "").casefold(),
            candidate.port,
        ) == (
            page.scheme.casefold(),
            (page.hostname or "").casefold(),
            page.port,
        )
    except ValueError:
        return False
    if not same_origin:
        return False

    plugin_path = unquote(candidate.path).casefold()
    if not (plugin_path == "/pluginfile.php" or plugin_path.startswith("/pluginfile.php/")):
        return False

    route_candidates = [plugin_path]
    route_candidates.extend(
        unquote(value).casefold()
        for value in parse_qs(candidate.query).get("file", [])
    )
    return any(
        "/mod_assign/introattachment/" in route
        or "/mod_assign/intro/" in route
        for route in route_candidates
    )


def _pluginfile_filename(candidate_url: str) -> str:
    """path형과 query형 pluginfile URL에서 표시용 파일명 후보를 복원한다."""

    parsed = urlparse(candidate_url)
    file_values = parse_qs(parsed.query).get("file", [])
    source = file_values[0] if file_values else parsed.path
    return unquote(source).rstrip("/").rsplit("/", 1)[-1]


async def parse_assignment_attachments(page: Page, assignment_id: str) -> list[Attachment]:
    # 교수 배포 파일은 실제 한성 DOM의 #intro 안에 있고, 사용자 제출 파일은 밖에 있다.
    # 구조 범위와 Moodle component/filearea를 이중 검증해 query형 URL도 지원하면서 하단의
    # 제출 파일(`assignsubmission_file/submission_files`)은 제외한다.
    links = page.locator("#intro a[href*='pluginfile.php']")
    attachments: list[Attachment] = []
    seen_urls: set[str] = set()
    for index in range(await links.count()):
        link = links.nth(index)
        href = await link.get_attribute("href")
        if not href:
            continue
        absolute_url = urljoin(page.url, html.unescape(href))
        if not _is_assignment_intro_attachment_url(absolute_url, page.url):
            continue
        name = normalize_text(await link.inner_text())
        if not name:
            name = normalize_text(await link.get_attribute("title") or "")
        if not name:
            name = normalize_text(_pluginfile_filename(absolute_url))
        if not name:
            continue
        if absolute_url in seen_urls:
            continue
        seen_urls.add(absolute_url)
        attachment_id = hashlib.sha256(absolute_url.encode("utf-8")).hexdigest()[:32]
        mime_type, _ = mimetypes.guess_type(name)
        attachments.append(
            Attachment(
                id=attachment_id,
                parent_type="assignment",
                parent_id=assignment_id,
                name=name,
                url=absolute_url,
                mime_type=mime_type,
            )
        )
    return attachments


async def parse_lectures_index(page: Page, course: Course) -> list[Lecture]:
    table = page.locator("table.mod_index, table.generaltable").first
    if not await table.count():
        is_vod_page = await page.locator("body.path-mod-vod").count()
        if is_vod_page:
            return []
        raise EclassParserChangedError("강의 영상 목록 표를 찾을 수 없습니다.")
    rows = table.locator("tbody tr")
    lectures: list[Lecture] = []
    current_week: int | None = None
    current_window: tuple[datetime, datetime] | None = None
    for index in range(await rows.count()):
        row = rows.nth(index)
        link = row.locator("a[href*='view.php?id=']").first
        cells = row.locator("td")
        texts = [normalize_text(await cells.nth(i).inner_text()) for i in range(await cells.count())]
        if texts:
            current_week = parse_week(texts[0]) or current_week
            current_window = parse_week_window(texts[0], year=course.year) or current_window
        if not await link.count():
            continue
        href = await link.get_attribute("href")
        lecture_id = query_id(urljoin(page.url, href or ""), "id")
        title = normalize_text(await link.inner_text())
        if not href or not lecture_id or not title:
            continue
        lectures.append(
            Lecture(
                id=lecture_id,
                course_id=course.id,
                title=title,
                url=urljoin(page.url, href),
                week=current_week,
                available_from=current_window[0] if current_window else None,
                available_until=current_window[1] if current_window else None,
            )
        )
    return lectures


async def merge_attendance_status(page: Page, lectures: list[Lecture]) -> list[Lecture]:
    """온라인출석부의 제목·O/X·학습시간을 VOD 목록과 결합한다."""

    table = page.locator("table.user_progress_table").first
    if not await table.count():
        # 출석부가 없는 강좌는 영상 목록 자체는 유지하되 상태를 추측하지 않는다.
        return lectures
    # 강의 목록은 ``[동영상] 제목``으로, 출석부는 ``제목``으로 보여주는
    # 강좌가 있다. 두 화면의 표시용 말머리와 한글 Unicode 표현을 통일해 결합한다.
    # 같은 제목이 여러 번 나올 수 있으므로 dict의 단일 값으로 덮어쓰지 않고
    # 출석부 행 순서대로 하나씩 소비한다.
    progress_by_title: dict[
        str, deque[tuple[EntityStatus, float | None]]
    ] = defaultdict(deque)
    rows = table.locator("tbody tr")
    for index in range(await rows.count()):
        cells = rows.nth(index).locator("td")
        texts = [normalize_text(await cells.nth(i).inner_text()) for i in range(await cells.count())]
        parsed = _parse_attendance_row(texts)
        if parsed is None:
            continue
        title, status, progress = parsed
        progress_by_title[_lecture_match_key(title)].append((status, progress))

    merged: list[Lecture] = []
    for lecture in lectures:
        matches = progress_by_title.get(_lecture_match_key(lecture.title))
        status, progress = (
            matches.popleft() if matches else (EntityStatus.UNKNOWN, None)
        )
        merged.append(
            lecture.model_copy(
                update={
                    "attendance_status": status,
                    "progress_percent": progress,
                    "status": status,
                }
            )
        )
    return merged


def _lecture_match_key(title: str) -> str:
    """강의 목록과 온라인출석부의 같은 영상 제목을 비교하는 키를 만든다."""

    normalized = unicodedata.normalize("NFC", normalize_text(title)).casefold()
    return re.sub(r"^\[\s*(?:동영상|vod|video)\s*\]\s*", "", normalized)


def _parse_attendance_row(
    texts: list[str],
) -> tuple[str, EntityStatus, float | None] | None:
    """강좌마다 다른 4~6열 온라인출석부 행을 제목·출석·진도율로 통일한다.

    실제 E-Class에는 ``주차, 제목, 학습시간, 출석, 진도`` 형태의 5열 표와
    ``주차, 제목, 기준시간, 학습시간, 출석, ...`` 형태의 6열 표가 함께 존재한다.
    """

    if len(texts) < 4:
        return None
    # 첫 열이 숫자 주차이면 두 번째 열이 제목이다. 주차 열이 없는 표는 첫 열이 제목이다.
    title_index = (
        1
        if len(texts) >= 5 or re.fullmatch(r"\d{1,2}", texts[0].strip())
        else 0
    )
    if title_index >= len(texts):
        return None
    title = texts[title_index]
    tail = texts[title_index + 1 :]
    attendance_values = [
        value.strip().upper()
        for value in tail
        if value.strip().upper() in {"O", "X"}
    ]
    attendance = attendance_values[-1] if attendance_values else ""
    status = (
        EntityStatus.COMPLETE
        if attendance == "O"
        else EntityStatus.INCOMPLETE
        if attendance == "X"
        else EntityStatus.UNKNOWN
    )

    durations = [seconds for value in tail if (seconds := duration_seconds(value)) is not None]
    progress: float | None = None
    if len(durations) >= 2 and durations[0] > 0:
        progress = round(min(100.0, durations[1] / durations[0] * 100), 2)
    elif status is EntityStatus.COMPLETE:
        # 5열 출석부는 기준 시간 없이 최종 O/X만 주므로 O를 완료율 100%로 정규화한다.
        progress = 100.0
    return title, status, progress


async def find_course_notice_board_url(page: Page) -> str | None:
    """강좌 게시판 목록에서 첫 공지사항 게시판 URL을 찾는다."""

    tables = page.locator("table.ubboard_table")
    for table_index in range(await tables.count()):
        rows = tables.nth(table_index).locator("tbody tr")
        for row_index in range(await rows.count()):
            row = rows.nth(row_index)
            link = row.locator("a[href*='/mod/ubboard/view.php?id=']").first
            if not await link.count():
                continue
            text = normalize_text(await link.inner_text()).casefold()
            if "공지" in text or "announcement" in text:
                href = await link.get_attribute("href")
                return urljoin(page.url, href or "") if href else None
    return None


async def parse_announcements_board(
    page: Page,
    *,
    course_id: str | None,
    limit: int,
) -> list[Announcement]:
    table = page.locator("table.ubboard_table").first
    if not await table.count():
        raise EclassParserChangedError("공지 목록 표를 찾을 수 없습니다.")
    rows = table.locator("tbody tr")
    announcements: list[Announcement] = []
    for index in range(await rows.count()):
        row = rows.nth(index)
        link = row.locator("a[href*='/mod/ubboard/article.php']").first
        if not await link.count():
            continue
        href = await link.get_attribute("href")
        announcement_id = query_id(href or "", "bwid")
        if not href or not announcement_id:
            continue
        cells = row.locator("td")
        texts = [normalize_text(await cells.nth(i).inner_text()) for i in range(await cells.count())]
        posted_at = parse_eclass_datetime(texts[3]) if len(texts) >= 4 else None
        announcements.append(
            Announcement(
                id=announcement_id,
                course_id=course_id,
                title=normalize_text(await link.inner_text()),
                url=urljoin(page.url, href),
                posted_at=posted_at,
            )
        )
        if len(announcements) >= limit:
            break
    return announcements


async def parse_announcement_details(page: Page, announcement_id: str) -> AnnouncementDetails:
    """한성 E-Class 공지 상세 페이지의 제목·작성자·작성일·본문을 정규화한다."""

    article = page.locator(".ubboard_view").first
    if not await article.count():
        raise EclassParserChangedError("공지 상세 영역을 찾을 수 없습니다.")

    subject = article.locator(".subject").first
    content_node = article.locator(".content .text_to_html, .content").first
    if not await subject.count() or not await content_node.count():
        raise EclassParserChangedError("공지 제목 또는 본문을 찾을 수 없습니다.")

    title = normalize_text(await subject.inner_text())
    raw_content = await content_node.inner_text()
    # 문단 경계는 유지하되 각 줄 안의 불규칙한 공백만 정리한다.
    content = "\n".join(
        line for line in (normalize_text(raw_line) for raw_line in raw_content.splitlines()) if line
    )
    if not title or not content:
        raise EclassParserChangedError("공지 제목 또는 본문이 비어 있습니다.")

    info_node = article.locator(".info").first
    info = normalize_text(await info_node.inner_text()) if await info_node.count() else ""
    author_match = re.search(r"작성자\s*:\s*(.*?)\s+(?:작성일|조회수)\s*:", info)
    date_match = re.search(r"작성일\s*:\s*(\d{4}[-/]\d{2}[-/]\d{2}(?:\s+\d{2}:\d{2})?)", info)

    actual_id = query_id(page.url, "bwid")
    if actual_id != announcement_id:
        raise EclassNotFoundError("요청한 공지와 상세 페이지가 일치하지 않습니다.")
    return AnnouncementDetails(
        id=announcement_id,
        course_id=await current_course_id(page),
        title=title,
        url=page.url,
        posted_at=parse_eclass_datetime(date_match.group(1)) if date_match else None,
        author=normalize_text(author_match.group(1)) if author_match else None,
        content=content,
    )


async def parse_grades_page(page: Page, course_id: str) -> list[Grade]:
    unavailable = page.get_by_text(re.compile(r"성적을 볼 수 없음|grades? (?:are )?not available", re.I))
    if await unavailable.count():
        return []
    table = page.locator("table.user-grade, table.generaltable, table[class*='grade']").first
    if not await table.count():
        raise EclassParserChangedError("성적 표 또는 비공개 안내를 찾을 수 없습니다.")

    headers = [
        normalize_text(await table.locator("thead th, tr:first-child th").nth(i).inner_text())
        for i in range(await table.locator("thead th, tr:first-child th").count())
    ]
    grade_index = _header_index(headers, "성적", "grade")
    item_index = _header_index(headers, "성적 항목", "grade item", "항목")
    if grade_index is None:
        raise EclassParserChangedError("성적 점수 열을 찾을 수 없습니다.")
    item_index = item_index if item_index is not None else 0

    grades: list[Grade] = []
    rows = table.locator("tbody tr")
    for index in range(await rows.count()):
        row = rows.nth(index)
        cells = row.locator("th, td")
        if await cells.count() <= max(item_index, grade_index):
            continue
        item = normalize_text(await cells.nth(item_index).inner_text())
        score = normalize_text(await cells.nth(grade_index).inner_text())
        if not item:
            continue
        row_id = await row.get_attribute("id") or ""
        numeric = re.search(r"(\d+)", row_id)
        grade_id = numeric.group(1) if numeric else hashlib.sha256(
            f"{course_id}:{item}".encode("utf-8")
        ).hexdigest()[:32]
        grades.append(
            Grade(
                id=grade_id,
                course_id=course_id,
                item=item,
                score=score or None,
                status=EntityStatus.COMPLETE if score and score != "-" else EntityStatus.UNKNOWN,
            )
        )
    return grades


def _submission_boolean(value: str) -> bool | None:
    if any(marker in value for marker in ("제출 완료", "submitted", "제출함")):
        return True
    if any(
        marker in value
        for marker in ("미제출", "제출하지", "not submitted", "no attempt", "no submission")
    ):
        return False
    return None


def _assignment_header_indexes(headers: list[str]) -> dict[str, int]:
    """표시 언어와 열 순서가 달라도 과제 마감·제출 열을 헤더 이름으로 찾는다."""

    normalized = [normalize_text(header).casefold() for header in headers]

    def find(*labels: str) -> int | None:
        for index, header in enumerate(normalized):
            if any(label in header for label in labels):
                return index
        return None

    columns = {
        "week": find("주", "week"),
        "due": find("종료 일시", "마감", "due date", "due"),
        "submitted": find("제출", "submission"),
    }
    if columns["due"] is None or columns["submitted"] is None:
        raise EclassParserChangedError("과제 표의 마감 또는 제출 헤더를 찾을 수 없습니다.")
    return {name: index for name, index in columns.items() if index is not None}


def _assignment_row_week(cell_text: str, previous_week: int | None) -> int | None:
    """병합된 Moodle 주차 셀이 비어 있으면 같은 표의 직전 주차를 이어받는다."""

    parsed = parse_week(cell_text)
    return parsed if parsed is not None else previous_week


def _is_empty_assignment_page(
    is_assignment_page: bool,
    assignment_link_count: int,
    main_text: str,
) -> bool:
    """언어별 Moodle 빈 상태 문구가 있는 실제 과제 모듈만 빈 목록으로 인정한다."""

    empty_markers = (
        "there are no assignments in this course",
        "이 강좌에는 과제가 없습니다",
        "등록된 과제가 없습니다",
    )
    return (
        is_assignment_page
        and assignment_link_count == 0
        and any(marker in main_text.casefold() for marker in empty_markers)
    )


def _value_for_labels(values: dict[str, str], *labels: str) -> str | None:
    normalized = {key.casefold(): value for key, value in values.items()}
    for label in labels:
        if label.casefold() in normalized:
            return normalized[label.casefold()]
    return None


def _header_index(headers: list[str], *needles: str) -> int | None:
    for index, header in enumerate(headers):
        folded = header.casefold()
        if any(needle.casefold() in folded for needle in needles):
            return index
    return None
