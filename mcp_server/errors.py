"""Adapter·Parser·Service 사이에서 사용하는 안전한 E-Class 읽기 오류."""


class EclassNotFoundError(RuntimeError):
    """요청한 강좌·과제·강의가 현재 사용자 범위에서 발견되지 않음."""


class EclassParserChangedError(RuntimeError):
    """필수 DOM 구조가 바뀌어 잘못된 데이터를 반환할 위험이 있음."""


class EclassTemporaryError(RuntimeError):
    """네트워크·브라우저 등 재시도 가능한 일시 오류."""
