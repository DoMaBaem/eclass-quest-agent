"""실제 한성 e-Class에서 확정한 URL 규칙과 선택자의 회귀 테스트."""

import unittest
from unittest.mock import AsyncMock, patch

from app.config import Settings
from mcp_server.adapters.hansung_playwright import HansungPlaywrightAdapter
from mcp_server.browser.credential_login import automatic_login_available
from mcp_server.browser.language import playwright_locale, with_eclass_language
from mcp_server.browser.selectors import HansungSelectors
from mcp_server.browser.session import AuthRequiredError
from mcp_server.parsers.common import SEOUL, parse_week, parse_week_window
from app.schemas.domain import EntityStatus
from mcp_server.parsers.course_content import (
    _assignment_row_week,
    _assignment_header_indexes,
    _is_empty_assignment_page,
    _is_assignment_intro_attachment_url,
    _lecture_match_key,
    _parse_attendance_row,
    _pluginfile_filename,
    _submission_boolean,
)


class HansungAdapterContractTest(unittest.TestCase):
    def test_assignment_columns_are_resolved_from_korean_headers(self) -> None:
        columns = _assignment_header_indexes(["주", "과제", "종료 일시", "제출", "성적"])

        self.assertEqual(columns["week"], 0)
        self.assertEqual(columns["due"], 2)
        self.assertEqual(columns["submitted"], 3)

    def test_assignment_columns_are_resolved_after_reordering(self) -> None:
        columns = _assignment_header_indexes(
            ["Assignment", "Submission status", "Week", "Due date"]
        )

        self.assertEqual(columns["submitted"], 1)
        self.assertEqual(columns["week"], 2)
        self.assertEqual(columns["due"], 3)

    def test_korean_is_default_but_explicit_language_is_preserved(self) -> None:
        self.assertEqual(
            with_eclass_language(
                "https://learn.hansung.ac.kr/mod/assign/index.php?id=1",
                "ko",
            ),
            "https://learn.hansung.ac.kr/mod/assign/index.php?id=1&lang=ko",
        )
        self.assertEqual(
            with_eclass_language(
                "https://learn.hansung.ac.kr/mod/assign/index.php?id=1&lang=en",
                "ko",
            ),
            "https://learn.hansung.ac.kr/mod/assign/index.php?id=1&lang=en",
        )
        self.assertEqual(playwright_locale("ko"), "ko-KR")

    def test_assignment_row_inherits_week_from_merged_table_cell(self) -> None:
        self.assertEqual(_assignment_row_week("15Week [09 June - 15 June]", None), 15)
        self.assertEqual(_assignment_row_week("", 15), 15)

    def test_week_parser_supports_korean_and_english_eclass_labels(self) -> None:
        self.assertEqual(parse_week("7주차"), 7)
        self.assertEqual(parse_week("6Week [07 April - 13 April]"), 6)
        self.assertEqual(parse_week("Week 11"), 11)

    def test_korean_week_window_becomes_seoul_start_and_end(self) -> None:
        window = parse_week_window("1주차 [3월03일 - 3월09일]", year=2026)

        self.assertIsNotNone(window)
        starts_at, ends_at = window  # type: ignore[misc]
        self.assertEqual(starts_at.isoformat(), "2026-03-03T00:00:00+09:00")
        self.assertEqual(ends_at.isoformat(), "2026-03-09T23:59:59+09:00")
        self.assertEqual(starts_at.tzinfo, SEOUL)

    def test_winter_week_window_can_cross_into_next_year(self) -> None:
        window = parse_week_window("4주차 [12월29일 - 1월04일]", year=2026)

        self.assertIsNotNone(window)
        starts_at, ends_at = window  # type: ignore[misc]
        self.assertEqual(starts_at.year, 2026)
        self.assertEqual(ends_at.year, 2027)

    def test_lecture_match_key_ignores_media_prefix_and_unicode_form(self) -> None:
        """강의 목록의 표시 말머리가 출석부와 달라도 같은 영상으로 판단한다."""

        self.assertEqual(
            _lecture_match_key("[동영상] 07주차_로지스틱 회귀"),
            _lecture_match_key("07주차_로지스틱 회귀"),
        )

    def test_five_column_attendance_row_uses_second_cell_as_title(self) -> None:
        """주차 열이 있는 5열 출석부에서 숫자 7을 제목으로 오인하지 않는다."""

        parsed = _parse_attendance_row(
            [
                "7",
                "[동영상] 07주차_로지스틱 회귀 및 확률적 경사 하강법",
                "40:53 2회 열람",
                "O",
                "O",
            ]
        )

        self.assertEqual(
            parsed,
            (
                "[동영상] 07주차_로지스틱 회귀 및 확률적 경사 하강법",
                EntityStatus.COMPLETE,
                100.0,
            ),
        )

    def test_service_semester_maps_to_eclass_query_value(self) -> None:
        self.assertEqual(
            HansungPlaywrightAdapter.SEMESTER_QUERY_VALUES,
            {1: "10", 2: "20", 3: "15", 4: "25"},
        )

    def test_course_id_is_read_from_moodle_course_url(self) -> None:
        course_id = HansungPlaywrightAdapter._course_id_from_url(
            "https://learn.hansung.ac.kr/course/view.php?id=46545"
        )
        self.assertEqual(course_id, "46545")

    def test_course_selector_does_not_match_unrelated_course_banner(self) -> None:
        self.assertEqual(
            HansungSelectors.COURSE_LINKS,
            (".my-course-lists a.coursefullname[href*='/course/view.php?id=']",),
        )

    def test_content_selectors_use_confirmed_moodle_module_urls(self) -> None:
        self.assertIn("a[href*='/mod/assign/view.php?id=']", HansungSelectors.ASSIGNMENT_LINKS)
        self.assertIn("a[href*='/mod/vod/view.php?id=']", HansungSelectors.LECTURE_LINKS)
        self.assertIn("a[href*='/mod/ubboard/view.php?id=']", HansungSelectors.ANNOUNCEMENT_LINKS)
        self.assertIn("a[href*='/grade/report/user/index.php?id=']", HansungSelectors.GRADE_LINKS)

    def test_encoded_assignment_intro_attachment_url_is_accepted(self) -> None:
        """한성 E-Class의 query형 pluginfile URL도 교수 배포 첨부로 판정한다."""

        page_url = "https://learn.hansung.ac.kr/mod/assign/view.php?id=1140975"
        attachment_url = (
            "https://learn.hansung.ac.kr/pluginfile.php?"
            "file=%2F1295828%2Fmod_assign%2Fintroattachment%2F0%2F"
            "DM_2026-1_%EC%A0%95%EB%8B%B5%EC%A7%80.docx&forcedownload=1"
        )

        self.assertTrue(
            _is_assignment_intro_attachment_url(attachment_url, page_url)
        )
        self.assertEqual(
            _pluginfile_filename(attachment_url),
            "DM_2026-1_정답지.docx",
        )

    def test_assignment_attachment_url_rejects_submission_and_external_links(self) -> None:
        """학생 제출 파일과 외부 호스트는 과제 배포 첨부에 섞지 않는다."""

        page_url = "https://learn.hansung.ac.kr/mod/assign/view.php?id=1140975"
        submission_url = (
            "https://learn.hansung.ac.kr/pluginfile.php?"
            "file=%2F1%2Fassignsubmission_file%2Fsubmission_files%2F0%2Fanswer.ipynb"
        )
        external_url = (
            "https://evil.example/pluginfile.php?"
            "file=%2F1%2Fmod_assign%2Fintroattachment%2F0%2Fguide.pdf"
        )

        self.assertFalse(
            _is_assignment_intro_attachment_url(submission_url, page_url)
        )
        self.assertFalse(
            _is_assignment_intro_attachment_url(external_url, page_url)
        )

    def test_credentials_are_hidden_from_settings_repr(self) -> None:
        settings = Settings(
            eclass_auto_login=True,
            eclass_username="student-secret",
            eclass_password="password-secret",
        )

        self.assertTrue(automatic_login_available(settings))
        self.assertNotIn("student-secret", repr(settings))
        self.assertNotIn("password-secret", repr(settings))


class HansungAutomaticReloginTest(unittest.IsolatedAsyncioTestCase):
    def test_english_empty_assignment_page_is_a_valid_empty_list(self) -> None:
        """영문 UI의 과제 없는 강좌를 파서 변경 오류로 오인하지 않는다."""

        self.assertTrue(
            _is_empty_assignment_page(
                True,
                0,
                "Assignments There are no Assignments in this course",
            )
        )
        self.assertFalse(_is_empty_assignment_page(True, 1, "There are no Assignments in this course"))

    def test_english_no_submission_is_incomplete(self) -> None:
        self.assertFalse(_submission_boolean("no submission"))

    async def test_auth_failure_refreshes_session_and_retries_once(self) -> None:
        adapter = HansungPlaywrightAdapter(
            Settings(
                eclass_auto_login=True,
                eclass_username="student",
                eclass_password="password",
            )
        )
        operation = AsyncMock()
        adapter._run_authenticated_once = AsyncMock(  # type: ignore[method-assign]
            side_effect=[AuthRequiredError("expired"), "retried-result"]
        )

        with patch(
            "mcp_server.adapters.hansung_playwright.refresh_encrypted_session",
            new=AsyncMock(),
        ) as refresh:
            result = await adapter._run_with_auto_relogin(operation)

        self.assertEqual(result, "retried-result")
        refresh.assert_awaited_once_with(adapter.settings)
        self.assertEqual(adapter._run_authenticated_once.await_count, 2)

    async def test_second_auth_failure_is_not_retried_forever(self) -> None:
        adapter = HansungPlaywrightAdapter(
            Settings(
                eclass_auto_login=True,
                eclass_username="student",
                eclass_password="password",
            )
        )
        operation = AsyncMock()
        adapter._run_authenticated_once = AsyncMock(  # type: ignore[method-assign]
            side_effect=AuthRequiredError("still expired")
        )

        with patch(
            "mcp_server.adapters.hansung_playwright.refresh_encrypted_session",
            new=AsyncMock(),
        ) as refresh:
            with self.assertRaises(AuthRequiredError):
                await adapter._run_with_auto_relogin(operation)

        refresh.assert_awaited_once_with(adapter.settings)
        self.assertEqual(adapter._run_authenticated_once.await_count, 2)


if __name__ == "__main__":
    unittest.main()
