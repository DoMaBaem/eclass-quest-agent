"""Agent 공통 JSON 문자열 스트림 추출기 테스트."""

import unittest

from app.agent.streaming import JsonStringFieldDeltaExtractor


class JsonStringFieldDeltaExtractorTest(unittest.TestCase):
    def test_extracts_field_split_across_chunks(self) -> None:
        extractor = JsonStringFieldDeltaExtractor("reply")

        self.assertEqual(extractor.feed('{"mode":"CHAT","re'), "")
        self.assertEqual(extractor.feed('ply":"안녕\\n세상 \\u'), "안녕\n세상 ")
        self.assertEqual(extractor.feed('263a","task_request":null}'), "☺")

    def test_decodes_escaped_quote_and_backslash(self) -> None:
        extractor = JsonStringFieldDeltaExtractor("message")

        self.assertEqual(extractor.feed('{"message":"A\\\"B\\\\C"}'), 'A"B\\C')
        self.assertEqual(extractor.feed("ignored after closing quote"), "")

    def test_ignores_missing_field(self) -> None:
        extractor = JsonStringFieldDeltaExtractor("reply")

        self.assertEqual(extractor.feed('{"mode":"CHAT"}'), "")


if __name__ == "__main__":
    unittest.main()

