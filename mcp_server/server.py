"""E-Class 읽기 기능을 MCP stdio Tool로 공개하는 실행 진입점."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from app.config import get_settings
from mcp_server.schemas import (
    AnnouncementDetailsResult,
    AnnouncementListResult,
    AssignmentDetailsResult,
    AssignmentListResult,
    AttachmentListResult,
    CourseAnnouncementResult,
    CourseAssignmentResult,
    CourseLectureResult,
    CourseListResult,
    CourseResolutionResult,
    DashboardSnapshotResult,
    GradeListResult,
    LectureListResult,
    LectureResolutionResult,
    LectureStatusResult,
    McpErrorCode,
    McpOutcomeStatus,
    McpToolError,
    SessionCheckResult,
    PlaybackResult,
    VerifiedPlaybackResult,
    DownloadResult,
)
from mcp_server.services.downloads import AttachmentDownloadService
from mcp_server.services.eclass_read import EclassReadService
from mcp_server.services.playback import LecturePlaybackService


mcp = FastMCP(
    "eclass-quest",
    instructions=(
        "한성대 E-Class의 실제 강좌·공지·과제·강의·성적을 구조화해 읽는 서버입니다. "
        "HTML, CSS 선택자, 로그인 비밀정보는 반환하지 않습니다. "
        "Startup·Heartbeat 전체 동기화에는 다운로드 없는 get_dashboard_snapshot을 사용합니다. "
        "사용자 표현으로 특정 강좌나 강의를 찾을 때는 list_course_*와 resolve_lecture를 우선 사용하고, "
        "강의 재생에는 원시 ID 대신 resolve_lecture가 발급한 reference_id를 사용합니다."
    ),
    log_level="WARNING",
)
service = EclassReadService(get_settings())
playback_service = LecturePlaybackService(get_settings())
download_service = AttachmentDownloadService(get_settings())


@mcp.tool(structured_output=True)
async def check_session() -> SessionCheckResult:
    """저장 세션을 확인하고 만료 시 암호화 저장 계정으로 한 번 자동 로그인합니다."""

    return await service.check_session()


@mcp.tool(structured_output=True)
async def list_courses(
    year: int | None = None,
    semester: int | None = None,
) -> CourseListResult:
    """학기를 생략하면 E-Class 기본 학기, 지정하면 해당 연도·학기 강좌를 조회합니다."""

    return await service.list_courses(year, semester)


@mcp.tool(structured_output=True)
async def get_dashboard_snapshot() -> DashboardSnapshotResult:
    """E-Class 기본 학기의 강좌·공지·과제·강의·성적을 다운로드 없이 일괄 조회합니다."""

    return await service.get_dashboard_snapshot()


@mcp.tool(structured_output=True)
async def resolve_course(
    query: str,
    year: int | None = None,
    semester: int | None = None,
) -> CourseResolutionResult:
    """강좌명 또는 `빅데프` 같은 약칭을 선택 학기의 실제 강좌 후보와 연결합니다."""

    return await service.resolve_course(query, year, semester)


@mcp.tool(structured_output=True)
async def list_announcements(
    course_id: str | None = None,
    limit: int = 20,
    year: int | None = None,
    semester: int | None = None,
) -> AnnouncementListResult:
    """기본 또는 지정 학기의 학교·강좌 공지를 최신순으로 조회합니다."""

    return await service.list_announcements(course_id, limit, year, semester)


@mcp.tool(structured_output=True)
async def list_course_announcements(
    course_query: str,
    limit: int = 20,
    year: int | None = None,
    semester: int | None = None,
) -> CourseAnnouncementResult:
    """과목명·약칭을 실제 강좌로 검증하고 해당 강좌 공지를 한 번에 조회합니다."""

    return await service.list_course_announcements(
        course_query,
        limit,
        year,
        semester,
    )


@mcp.tool(structured_output=True)
async def get_announcement_details(
    announcement_url: str,
    course_id: str | None = None,
    year: int | None = None,
    semester: int | None = None,
) -> AnnouncementDetailsResult:
    """공지 목록의 E-Class URL을 열어 제목·작성자·작성일·본문을 조회합니다."""

    return await service.get_announcement_details(
        announcement_url,
        course_id,
        year,
        semester,
    )


@mcp.tool(structured_output=True)
async def list_assignments(
    days: int | None = None,
    only_incomplete: bool = False,
    year: int | None = None,
    semester: int | None = None,
    course_id: str | None = None,
    course_query: str | None = None,
    assignment_query: str | None = None,
) -> AssignmentListResult:
    """전체 과제 또는 ID·강좌명·약칭으로 제한한 특정 강좌 과제를 조회합니다."""

    return await service.list_assignments(
        days,
        only_incomplete,
        year,
        semester,
        course_id,
        course_query,
        assignment_query,
    )


@mcp.tool(structured_output=True)
async def list_course_assignments(
    course_query: str,
    days: int | None = None,
    only_incomplete: bool = False,
    assignment_query: str | None = None,
    year: int | None = None,
    semester: int | None = None,
) -> CourseAssignmentResult:
    """과목 ID를 복사하지 않고 과목명·과제명으로 검증된 과제 목록을 조회합니다."""

    return await service.list_course_assignments(
        course_query,
        days,
        only_incomplete,
        assignment_query,
        year,
        semester,
    )


@mcp.tool(structured_output=True)
async def get_assignment_details(
    assignment_id: str,
    year: int | None = None,
    semester: int | None = None,
) -> AssignmentDetailsResult:
    """과제 상세의 제출 여부·마감·최종 수정 시간을 조회합니다."""

    return await service.get_assignment_details(assignment_id, year, semester)


@mcp.tool(structured_output=True)
async def list_assignment_attachments(
    assignment_id: str,
    year: int | None = None,
    semester: int | None = None,
) -> AttachmentListResult:
    """과제에 공개된 첨부파일의 메타데이터를 조회합니다."""

    return await service.list_assignment_attachments(assignment_id, year, semester)


@mcp.tool(structured_output=True)
async def list_lectures(
    course_id: str | None = None,
    only_unwatched: bool = False,
    year: int | None = None,
    semester: int | None = None,
) -> LectureListResult:
    """강의 영상과 온라인출석부 시청 상태를 결합해 조회합니다."""

    return await service.list_lectures(course_id, only_unwatched, year, semester)


@mcp.tool(structured_output=True)
async def list_course_lectures(
    course_query: str,
    week: int | None = None,
    only_unwatched: bool = False,
    year: int | None = None,
    semester: int | None = None,
) -> CourseLectureResult:
    """과목명·주차를 실제 강좌와 대조해 조건에 맞는 강의 목록을 조회합니다."""

    return await service.list_course_lectures(
        course_query,
        week,
        only_unwatched,
        year,
        semester,
    )


@mcp.tool(structured_output=True)
async def resolve_lecture(
    course_query: str,
    week: int | None = None,
    title_query: str | None = None,
    only_unwatched: bool = False,
    year: int | None = None,
    semester: int | None = None,
) -> LectureResolutionResult:
    """강좌·주차·제목을 대조해 재생 가능한 단일 강의의 검증 참조를 발급합니다."""

    return await service.resolve_lecture(
        course_query,
        week,
        title_query,
        only_unwatched,
        year,
        semester,
    )


@mcp.tool(structured_output=True)
async def get_lecture_status(
    lecture_id: str,
    year: int | None = None,
    semester: int | None = None,
) -> LectureStatusResult:
    """특정 강의 영상의 출석 상태와 진도율을 조회합니다."""

    return await service.get_lecture_status(lecture_id, year, semester)


@mcp.tool(structured_output=True)
async def get_grades(
    course_id: str | None = None,
    year: int | None = None,
    semester: int | None = None,
) -> GradeListResult:
    """학생에게 현재 공개된 성적만 조회합니다."""

    return await service.get_grades(course_id, year, semester)


@mcp.tool(structured_output=True)
async def play_lecture(
    lecture_id: str,
    explicit_user_request: bool = False,
    max_minutes: int = 180,
    volume_percent: int = 100,
    playback_rate: float = 1.0,
    window_width: int | None = None,
    window_height: int | None = None,
) -> PlaybackResult:
    """headed player를 열고 볼륨·배속·창 크기를 적용해 강의를 재생합니다."""

    return await playback_service.play(
        lecture_id,
        explicit_user_request=explicit_user_request,
        max_minutes=max_minutes,
        volume_percent=volume_percent,
        playback_rate=playback_rate,
        window_width=window_width,
        window_height=window_height,
    )


@mcp.tool(structured_output=True)
async def play_resolved_lecture(
    reference_id: str,
    explicit_user_request: bool = False,
    max_minutes: int = 180,
    volume_percent: int = 100,
    playback_rate: float = 1.0,
    window_width: int | None = None,
    window_height: int | None = None,
) -> VerifiedPlaybackResult:
    """resolve_lecture가 발급한 유효한 참조만 실제 lecture_id로 바꿔 재생합니다."""

    target = service.get_verified_lecture_target(reference_id)
    if target is None:
        return VerifiedPlaybackResult(
            ok=False,
            status=McpOutcomeStatus.NOT_FOUND,
            error=McpToolError(
                code=McpErrorCode.NOT_FOUND,
                message="검증된 강의 참조가 없거나 만료되었습니다. resolve_lecture를 다시 실행해 주세요.",
                retryable=False,
            ),
        )
    playback = await playback_service.play(
        target.lecture_id,
        explicit_user_request=explicit_user_request,
        max_minutes=max_minutes,
        volume_percent=volume_percent,
        playback_rate=playback_rate,
        window_width=window_width,
        window_height=window_height,
    )
    if not playback.ok:
        error = playback.error
        status = {
            McpErrorCode.AUTH_REQUIRED: McpOutcomeStatus.AUTH_REQUIRED,
            McpErrorCode.NOT_FOUND: McpOutcomeStatus.NOT_FOUND,
            McpErrorCode.AMBIGUOUS_MATCH: McpOutcomeStatus.AMBIGUOUS,
            McpErrorCode.PARSER_CHANGED: McpOutcomeStatus.PARSER_CHANGED,
            McpErrorCode.INVALID_REQUEST: McpOutcomeStatus.INVALID_REQUEST,
            McpErrorCode.TEMPORARY_FAILURE: McpOutcomeStatus.TEMPORARY_FAILURE,
        }[error.code if error else McpErrorCode.TEMPORARY_FAILURE]
        return VerifiedPlaybackResult(
            ok=False,
            status=status,
            target=target,
            error=error,
        )
    return VerifiedPlaybackResult(
        ok=True,
        status=McpOutcomeStatus.FOUND,
        target=target,
        data=playback.data,
    )


@mcp.tool(structured_output=True)
async def preview_resolved_lecture(
    reference_id: str,
    explicit_user_request: bool = False,
    seconds: int = 20,
    options: dict[str, int | float] | None = None,
) -> VerifiedPlaybackResult:
    """검증 참조의 강의만 잠시 미리보고 자동 종료합니다.

    ``reference_id``는 반드시 같은 MCP 서버 프로세스의 ``resolve_lecture``가 단일
    ``FOUND`` 결과에서 발급한 값이어야 합니다. 화면 설정은 ``options``의
    ``volume_percent``, ``playback_rate``, ``window_width``, ``window_height``만
    허용합니다. 모델이 원시 ``lecture_id``를 미리보기 Tool에 전달하는 우회 경로를
    만들지 않기 위한 고수준 Tool입니다.
    """

    target = service.get_verified_lecture_target(reference_id)
    if target is None:
        return VerifiedPlaybackResult(
            ok=False,
            status=McpOutcomeStatus.NOT_FOUND,
            error=McpToolError(
                code=McpErrorCode.NOT_FOUND,
                message="검증된 강의 참조가 없거나 만료되었습니다. resolve_lecture를 다시 실행해 주세요.",
                retryable=False,
            ),
        )

    playback_options = options or {}
    allowed_options = {
        "volume_percent",
        "playback_rate",
        "window_width",
        "window_height",
    }
    unknown_options = set(playback_options) - allowed_options
    if unknown_options:
        return VerifiedPlaybackResult(
            ok=False,
            status=McpOutcomeStatus.INVALID_REQUEST,
            target=target,
            error=McpToolError(
                code=McpErrorCode.INVALID_REQUEST,
                message="지원하지 않는 영상 미리보기 설정이 포함되어 있습니다.",
                retryable=False,
            ),
        )

    # bool은 Python에서 int의 하위 타입이므로 명시적으로 거부한다. 나머지 범위 검증은
    # LecturePlaybackService가 일반 재생과 동일한 계약으로 수행한다.
    if any(isinstance(value, bool) for value in playback_options.values()):
        return VerifiedPlaybackResult(
            ok=False,
            status=McpOutcomeStatus.INVALID_REQUEST,
            target=target,
            error=McpToolError(
                code=McpErrorCode.INVALID_REQUEST,
                message="영상 미리보기 설정은 숫자로 입력해 주세요.",
                retryable=False,
            ),
        )

    try:
        volume_percent = int(playback_options.get("volume_percent", 100))
        playback_rate = float(playback_options.get("playback_rate", 1.0))
        window_width_value = playback_options.get("window_width")
        window_height_value = playback_options.get("window_height")
        window_width = int(window_width_value) if window_width_value is not None else None
        window_height = int(window_height_value) if window_height_value is not None else None
    except (TypeError, ValueError, OverflowError):
        return VerifiedPlaybackResult(
            ok=False,
            status=McpOutcomeStatus.INVALID_REQUEST,
            target=target,
            error=McpToolError(
                code=McpErrorCode.INVALID_REQUEST,
                message="영상 미리보기 설정은 숫자로 입력해 주세요.",
                retryable=False,
            ),
        )

    playback = await playback_service.preview(
        target.lecture_id,
        explicit_user_request=explicit_user_request,
        seconds=seconds,
        volume_percent=volume_percent,
        playback_rate=playback_rate,
        window_width=window_width,
        window_height=window_height,
    )
    if not playback.ok:
        error = playback.error
        status = {
            McpErrorCode.AUTH_REQUIRED: McpOutcomeStatus.AUTH_REQUIRED,
            McpErrorCode.NOT_FOUND: McpOutcomeStatus.NOT_FOUND,
            McpErrorCode.AMBIGUOUS_MATCH: McpOutcomeStatus.AMBIGUOUS,
            McpErrorCode.PARSER_CHANGED: McpOutcomeStatus.PARSER_CHANGED,
            McpErrorCode.INVALID_REQUEST: McpOutcomeStatus.INVALID_REQUEST,
            McpErrorCode.TEMPORARY_FAILURE: McpOutcomeStatus.TEMPORARY_FAILURE,
        }[error.code if error else McpErrorCode.TEMPORARY_FAILURE]
        return VerifiedPlaybackResult(
            ok=False,
            status=status,
            target=target,
            error=error,
        )
    return VerifiedPlaybackResult(
        ok=True,
        status=McpOutcomeStatus.FOUND,
        target=target,
        data=playback.data,
    )


@mcp.tool(structured_output=True)
async def stop_lecture(playback_id: str) -> PlaybackResult:
    """play_lecture가 반환한 ID의 브라우저를 닫아 재생을 중지합니다."""

    return await playback_service.stop(playback_id)


@mcp.tool(structured_output=True)
async def preview_lecture(
    lecture_id: str,
    explicit_user_request: bool = False,
    seconds: int = 20,
    volume_percent: int = 100,
    playback_rate: float = 1.0,
    window_width: int | None = None,
    window_height: int | None = None,
) -> PlaybackResult:
    """실제 강의 player를 열어 5~30초 재생한 뒤 자동 종료하는 시연 Tool입니다."""

    return await playback_service.preview(
        lecture_id,
        explicit_user_request=explicit_user_request,
        seconds=seconds,
        volume_percent=volume_percent,
        playback_rate=playback_rate,
        window_width=window_width,
        window_height=window_height,
    )


@mcp.tool(structured_output=True)
async def download_attachment(
    attachment_url: str,
    attachment_id: str,
    filename: str,
) -> DownloadResult:
    """검증된 E-Class 첨부 URL을 격리된 임시 경로에 내려받고 불투명 ID만 반환합니다."""

    return await download_service.download(attachment_url, attachment_id, filename)


def main() -> None:
    """stdout을 MCP JSON-RPC 전용으로 유지하며 stdio 서버를 실행한다."""

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
