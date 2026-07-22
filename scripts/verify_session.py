"""저장된 한성 e-Class 세션으로 지정 학기 강좌를 한 번 읽어 보는 단독 검증 스크립트."""

from __future__ import annotations

import argparse
import asyncio

from app.config import get_settings
from mcp_server.adapters.hansung_playwright import HansungPlaywrightAdapter, SelectorChangedError
from mcp_server.browser.session import AuthRequiredError


async def verify(year: int, semester: int) -> int:
    """세션으로 강좌를 읽고 성공 시 첫 Course JSON만 출력한다.

    반환 코드 2는 재로그인 필요, 3은 LMS 선택자 변경을 의미해 셸 스크립트에서도 원인을 구분할 수 있다.
    """

    try:
        scoped = await HansungPlaywrightAdapter(get_settings()).list_courses(
            year=year,
            semester=semester,
        )
    except AuthRequiredError as exc:
        print(f"AUTH_REQUIRED: {exc}")
        return 2
    except SelectorChangedError as exc:
        print(f"PARSER_CHANGED: {exc}")
        return 3
    courses = scoped.data
    print(
        f"조회 학기: {scoped.selected_term.year}년 {scoped.selected_term.semester_name}",
        flush=True,
    )
    print(f"강좌 수: {len(courses)}개", flush=True)
    # 전체 강좌를 터미널에 노출하지 않고 연결 검증에 필요한 첫 항목만 보여 준다.
    if courses:
        print(courses[0].model_dump_json(indent=2))
    return 0


def main() -> int:
    """연도·학기 CLI 인자를 받아 비동기 verify를 실행한다."""

    parser = argparse.ArgumentParser(description="암호화 E-Class 세션을 검증합니다.")
    parser.add_argument("--year", type=int, default=2026)
    parser.add_argument("--semester", type=int, default=1)
    args = parser.parse_args()
    return asyncio.run(verify(args.year, args.semester))


if __name__ == "__main__":
    raise SystemExit(main())
