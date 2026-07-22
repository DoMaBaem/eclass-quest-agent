"""사용자 강좌명·약칭을 실제 E-Class 강좌 목록과 결정적으로 연결한다."""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

from app.schemas.domain import Assignment, Course, Lecture
from mcp_server.schemas import CourseResolution


def _course_key(value: str) -> str:
    """분반 표식과 구두점을 제외하고 강좌명 비교에 사용할 문자열을 만든다."""

    normalized = unicodedata.normalize("NFC", value).casefold()
    normalized = re.sub(r"\[[^\[\]]*]", "", normalized)
    return re.sub(r"[^0-9a-z가-힣]+", "", normalized)


def _is_subsequence(query: str, target: str) -> bool:
    """`빅데프`처럼 원래 강좌명의 글자를 순서대로 줄인 약칭을 인식한다."""

    iterator = iter(target)
    return all(any(candidate == char for candidate in iterator) for char in query)


def _partial_similarity(query: str, target: str) -> float:
    """긴 제목 안의 일부 구간과 비교해 한두 글자 오타가 있는 짧은 표현도 평가한다."""

    if not query or not target:
        return 0.0
    scores = [SequenceMatcher(None, query, target).ratio()]
    minimum = max(2, len(query) - 2)
    maximum = min(len(target), len(query) + 2)
    for size in range(minimum, maximum + 1):
        scores.extend(
            SequenceMatcher(None, query, target[start : start + size]).ratio()
            for start in range(0, len(target) - size + 1)
        )
    return max(scores)


def _ranked_matches(query: str, values: list[str], *, strip_groups: bool = False) -> list[int]:
    query_key = _course_key(query) if strip_groups else re.sub(
        r"[^0-9a-z가-힣]+", "", unicodedata.normalize("NFC", query).casefold()
    )
    if len(query_key) < 2:
        return []
    scores: list[tuple[float, int]] = []
    for index, value in enumerate(values):
        target = _course_key(value) if strip_groups else re.sub(
            r"[^0-9a-z가-힣]+", "", unicodedata.normalize("NFC", value).casefold()
        )
        if query_key == target:
            score = 3.0
        elif query_key in target:
            score = 2.5
        elif len(query_key) >= 3 and _is_subsequence(query_key, target):
            score = 2.0
        else:
            similarity = _partial_similarity(query_key, target)
            threshold = 0.64 if len(query_key) <= 4 else 0.72
            score = similarity if similarity >= threshold else 0.0
        if score:
            scores.append((score, index))
    if not scores:
        return []
    best = max(score for score, _index in scores)
    tolerance = 0.04 if best < 1.0 else 0.0
    return [index for score, index in scores if score >= best - tolerance]


def resolve_course_query(query: str, courses: list[Course]) -> CourseResolution:
    """정확 일치→포함→순서 약칭 순으로 평가하고 동점이면 임의 선택하지 않는다."""

    query_key = _course_key(query)
    if len(query_key) < 2:
        return CourseResolution(query=query, status="NOT_FOUND")

    matched_indexes = _ranked_matches(query, [course.name for course in courses], strip_groups=True)
    if not matched_indexes:
        return CourseResolution(query=query, status="NOT_FOUND")
    candidates = [courses[index] for index in matched_indexes]
    if len(candidates) == 1:
        return CourseResolution(
            query=query,
            status="MATCHED",
            course=candidates[0],
            candidates=candidates,
        )
    return CourseResolution(query=query, status="AMBIGUOUS", candidates=candidates[:20])


def filter_assignments_by_query(query: str, assignments: list[Assignment]) -> list[Assignment]:
    """과제 제목 오타를 허용하되 최고 점수 동률 후보는 모두 남겨 임의 선택을 막는다."""

    indexes = _ranked_matches(query, [assignment.title for assignment in assignments])
    return [assignments[index] for index in indexes]


def filter_lectures_by_query(query: str, lectures: list[Lecture]) -> list[Lecture]:
    """강의 제목의 말머리·공백 차이와 작은 오타를 허용해 검증된 후보만 반환한다.

    이 함수는 최종 후보를 임의로 하나 고르지 않는다. 최고 점수가 같은 영상이 여러 개면
    호출자가 ``AMBIGUOUS`` 상태로 사용자에게 다시 선택을 받아야 한다.
    """

    indexes = _ranked_matches(query, [lecture.title for lecture in lectures])
    return [lectures[index] for index in indexes]


__all__ = [
    "filter_assignments_by_query",
    "filter_lectures_by_query",
    "resolve_course_query",
]
