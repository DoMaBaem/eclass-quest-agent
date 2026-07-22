"""MCP 서비스가 의존하는 최소 LMS 읽기 인터페이스.

서비스 계층은 한성대학교의 CSS 선택자를 직접 알지 않고 이 추상 클래스만 호출한다. 나중에 LMS
구현이 바뀌어도 같은 메서드 계약을 지키는 Adapter로 교체할 수 있다.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Generic, TypeVar

from app.schemas.domain import (
    Announcement,
    AnnouncementDetails,
    Assignment,
    Attachment,
    Course,
    Grade,
    Lecture,
)
from mcp_server.schemas import SelectedTerm


DataT = TypeVar("DataT")


@dataclass(frozen=True, slots=True)
class TermScopedData(Generic[DataT]):
    """정규화된 데이터와 해당 데이터를 읽은 실제 학기를 함께 전달한다."""

    data: DataT
    selected_term: SelectedTerm


class EclassAdapter(ABC):
    """모든 실제 LMS Adapter가 구현해야 하는 읽기 기능."""

    @abstractmethod
    async def ensure_login(self) -> None:
        """저장 세션이 유효한지 확인하고 아니면 AuthRequiredError를 낸다."""

    @abstractmethod
    async def list_courses(
        self, *, year: int | None, semester: int | None
    ) -> TermScopedData[list[Course]]:
        """지정 학기 또는 E-Class 기본 학기의 강좌를 반환한다."""

    @abstractmethod
    async def list_announcements(
        self, *, course_id: str | None, limit: int, year: int | None, semester: int | None
    ) -> TermScopedData[list[Announcement]]: ...

    @abstractmethod
    async def get_announcement_details(
        self,
        announcement_url: str,
        *,
        course_id: str | None,
        year: int | None,
        semester: int | None,
    ) -> TermScopedData[AnnouncementDetails]: ...

    @abstractmethod
    async def list_assignments(
        self,
        *,
        course_id: str | None,
        days: int | None,
        only_incomplete: bool,
        year: int | None,
        semester: int | None,
    ) -> TermScopedData[list[Assignment]]: ...

    @abstractmethod
    async def get_assignment_details(
        self, assignment_id: str, *, year: int | None, semester: int | None
    ) -> TermScopedData[Assignment]: ...

    @abstractmethod
    async def list_assignment_attachments(
        self, assignment_id: str, *, year: int | None, semester: int | None
    ) -> TermScopedData[list[Attachment]]: ...

    @abstractmethod
    async def list_lectures(
        self,
        *,
        course_id: str | None,
        only_unwatched: bool,
        year: int | None,
        semester: int | None,
    ) -> TermScopedData[list[Lecture]]: ...

    @abstractmethod
    async def get_lecture_status(
        self, lecture_id: str, *, year: int | None, semester: int | None
    ) -> TermScopedData[Lecture]: ...

    @abstractmethod
    async def get_grades(
        self, *, course_id: str | None, year: int | None, semester: int | None
    ) -> TermScopedData[list[Grade]]: ...
