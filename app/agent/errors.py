"""여러 Agent와 Runtime이 공통으로 사용하는 예외.

공통 설정 오류를 특정 Agent 파일에 두면 그 Agent를 제거할 때 다른 모듈까지 깨진다. 따라서
OpenAI API 키 누락처럼 실행 계층 전체가 알아야 하는 오류는 이 파일에서 관리한다.
"""


class OpenAiApiKeyRequiredError(RuntimeError):
    """실제 Agent 실행 전 OpenAI API 키가 없는 경우의 명확한 설정 오류."""

