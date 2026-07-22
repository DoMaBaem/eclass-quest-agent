"""강좌 약칭을 실제 E-Class 강좌와 안전하게 연결하는 계약 테스트."""

import unittest

from app.schemas.domain import Assignment, Course, Lecture
from mcp_server.services.course_resolution import (
    filter_assignments_by_query,
    filter_lectures_by_query,
    resolve_course_query,
)


def _course(course_id: str, name: str) -> Course:
    return Course(
        id=course_id,
        name=name,
        url=f"https://learn.hansung.ac.kr/course/view.php?id={course_id}",
        year=2026,
        semester=1,
    )


class CourseResolutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.courses = [
            _course("46500", "빅데이터프로그래밍[7,A,N]"),
            _course("46499", "데이터마이닝[A,B]"),
            _course("46516", "딥러닝[A,B,N]"),
        ]

    def test_abbreviated_course_name_is_resolved_without_guessing_an_id(self) -> None:
        result = resolve_course_query("빅데프", self.courses)

        self.assertEqual(result.status, "MATCHED")
        self.assertEqual(result.course.id, "46500")  # type: ignore[union-attr]

    def test_unknown_course_is_not_replaced_with_another_course(self) -> None:
        result = resolve_course_query("운영체제", self.courses)

        self.assertEqual(result.status, "NOT_FOUND")
        self.assertIsNone(result.course)

    def test_course_name_typo_is_tolerated(self) -> None:
        result = resolve_course_query("빅데이타프로그래밍", self.courses)

        self.assertEqual(result.status, "MATCHED")
        self.assertEqual(result.course.id, "46500")  # type: ignore[union-attr]

    def test_assignment_title_typo_returns_verified_candidate(self) -> None:
        assignments = [
            Assignment(
                id="1",
                course_id="46500",
                title="실습과제 제출 가이드라인",
                url="https://learn.hansung.ac.kr/mod/assign/view.php?id=1",
            ),
            Assignment(
                id="2",
                course_id="46500",
                title="기말 프로젝트 결과보고서",
                url="https://learn.hansung.ac.kr/mod/assign/view.php?id=2",
            ),
        ]

        result = filter_assignments_by_query("실습과재 제출 가이드", assignments)

        self.assertEqual([assignment.id for assignment in result], ["1"])

    def test_lecture_title_typo_returns_verified_candidate_without_changing_id(self) -> None:
        lectures = [
            Lecture(
                id="501",
                course_id="46500",
                title="[동영상] 02주차 Python 개요 및 가상환경 구축",
                url="https://learn.hansung.ac.kr/mod/vod/view.php?id=501",
                week=2,
            ),
            Lecture(
                id="502",
                course_id="46500",
                title="03주차 데이터 전처리",
                url="https://learn.hansung.ac.kr/mod/vod/view.php?id=502",
                week=3,
            ),
        ]

        result = filter_lectures_by_query("가상한경 구축", lectures)

        self.assertEqual([lecture.id for lecture in result], ["501"])


if __name__ == "__main__":
    unittest.main()
