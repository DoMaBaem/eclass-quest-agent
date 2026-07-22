"""모든 Agents SDK 실행에 공통으로 적용하는 개인정보 보호 설정."""

from __future__ import annotations

from agents import RunConfig


def privacy_safe_run_config() -> RunConfig:
    """Trace 구조는 남기되 모델·Tool 입출력 원문은 전송하지 않는다.

    E-Class 결과에는 강좌명, 과제 본문과 첨부 문서 내용이 포함될 수 있다. SDK 기본값이나
    환경변수에 의존하지 않고 매 실행에서 민감 데이터 제외를 명시적으로 강제한다.
    """

    return RunConfig(trace_include_sensitive_data=False)


__all__ = ["privacy_safe_run_config"]
