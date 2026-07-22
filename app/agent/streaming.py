"""구조화된 Agent JSON 스트림에서 사용자용 문자열만 추출하는 공통 기능."""

from __future__ import annotations

import re


class JsonStringFieldDeltaExtractor:
    """스트리밍되는 JSON에서 지정 문자열 필드의 내용만 점진적으로 추출한다."""

    def __init__(self, field_name: str) -> None:
        self._field_pattern = re.compile(rf'"{re.escape(field_name)}"\s*:\s*"')
        self._search_buffer = ""
        self._started = False
        self._finished = False
        self._escaped = False
        self._unicode_digits: list[str] | None = None

    def feed(self, chunk: str) -> str:
        """새 JSON 조각을 받아 이번 조각에서 새로 확인된 필드 문자만 반환한다."""

        if self._finished or not chunk:
            return ""
        if not self._started:
            # 필드 시작 표식이 여러 네트워크 delta로 잘릴 수 있어 버퍼에서 이어서 검색한다.
            self._search_buffer += chunk
            match = self._field_pattern.search(self._search_buffer)
            if match is None:
                self._search_buffer = self._search_buffer[-256:]
                return ""
            chunk = self._search_buffer[match.end() :]
            self._search_buffer = ""
            self._started = True

        output: list[str] = []
        escape_map = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
        for character in chunk:
            # \uXXXX가 delta 사이에서 끊겨도 네 자리를 모은 뒤 실제 문자로 복원한다.
            if self._unicode_digits is not None:
                self._unicode_digits.append(character)
                if len(self._unicode_digits) == 4:
                    try:
                        output.append(chr(int("".join(self._unicode_digits), 16)))
                    except ValueError:
                        pass
                    self._unicode_digits = None
                continue
            if self._escaped:
                self._escaped = False
                if character == "u":
                    self._unicode_digits = []
                else:
                    output.append(escape_map.get(character, character))
                continue
            if character == "\\":
                self._escaped = True
                continue
            if character == '"':
                self._finished = True
                break
            output.append(character)
        return "".join(output)
