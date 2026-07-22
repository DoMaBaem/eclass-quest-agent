"""여러 E-Class Parser가 공유하는 텍스트·URL·날짜 처리 함수."""

from __future__ import annotations

import re
from datetime import datetime, time
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo


SEOUL = ZoneInfo("Asia/Seoul")


def normalize_text(value: str) -> str:
    return " ".join(value.split())


def query_id(url: str, *keys: str) -> str | None:
    query = parse_qs(urlparse(url).query)
    for key in keys or ("id",):
        value = query.get(key, [None])[0]
        if value:
            return value
    return None


def parse_eclass_datetime(value: str) -> datetime | None:
    """현재 E-Class의 YYYY-MM-DD HH:MM 또는 YYYY/MM/DD HH:MM을 서울 시각으로 읽는다."""

    normalized = normalize_text(value)
    for pattern in ("%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(normalized, pattern).replace(tzinfo=SEOUL)
        except ValueError:
            continue
    return None


def parse_week(value: str) -> int | None:
    # 한성 E-Class의 표시 언어에 따라 ``7주차``, ``7Week``, ``Week 7``이 모두 사용된다.
    matched = re.search(r"(?i)(?:week\s*(\d+)|(\d+)\s*(?:주|week))", value)
    if matched is None:
        return None
    return int(matched.group(1) or matched.group(2))


def parse_week_window(value: str, *, year: int) -> tuple[datetime, datetime] | None:
    """``1주차 [3월03일 - 3월09일]``을 서울 시각의 주차 운영 기간으로 읽는다."""

    normalized = normalize_text(value)
    matched = re.search(
        r"(\d{1,2})\s*월\s*(\d{1,2})\s*일\s*-\s*"
        r"(\d{1,2})\s*월\s*(\d{1,2})\s*일",
        normalized,
    )
    if matched is None:
        return None
    start_month, start_day, end_month, end_day = map(int, matched.groups())
    end_year = year + 1 if end_month < start_month else year
    try:
        starts_at = datetime.combine(
            datetime(year, start_month, start_day).date(),
            time.min,
            tzinfo=SEOUL,
        )
        ends_at = datetime.combine(
            datetime(end_year, end_month, end_day).date(),
            time(23, 59, 59),
            tzinfo=SEOUL,
        )
    except ValueError:
        return None
    return starts_at, ends_at


def duration_seconds(value: str) -> int | None:
    """학습시간 앞부분의 MM:SS를 초로 변환한다."""

    matched = re.search(r"(\d+):(\d{2})", value)
    if not matched:
        return None
    return int(matched.group(1)) * 60 + int(matched.group(2))
