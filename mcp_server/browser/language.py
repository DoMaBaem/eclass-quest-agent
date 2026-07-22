"""E-Class URL과 Playwright Context의 기본 표시 언어를 한곳에서 관리한다."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


PLAYWRIGHT_LOCALES = {"ko": "ko-KR", "en": "en-US"}


def with_eclass_language(url: str, language: str) -> str:
    """URL에 언어가 없을 때만 기본값을 추가해 사용자의 명시적 선택은 보존한다."""

    parsed = urlparse(url)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if any(key.casefold() == "lang" for key, _value in query):
        return url
    query.append(("lang", language))
    return urlunparse(parsed._replace(query=urlencode(query)))


def playwright_locale(language: str) -> str:
    """E-Class 언어 코드를 Playwright 브라우저 locale로 변환한다."""

    return PLAYWRIGHT_LOCALES.get(language, "ko-KR")
