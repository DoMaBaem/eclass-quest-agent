"""E-Class 첨부 다운로드 응답의 파일 형식·viewer URL 판정 테스트."""

from __future__ import annotations

import unittest

from app.config import Settings
from mcp_server.services.downloads import (
    _content_matches_filename,
    _looks_like_html,
    _viewer_file_url,
)


class DownloadResponseHelperTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(_env_file=None)

    def test_looks_like_html_uses_header_or_body_signature(self) -> None:
        """HTML content-type과 HTML 본문 sniffing 중 하나만 맞아도 wrapper로 본다."""

        self.assertTrue(_looks_like_html(b"not important", "text/html; charset=utf-8"))
        self.assertTrue(
            _looks_like_html(
                b"\n  <!DOCTYPE html><html><body>viewer</body></html>",
                "application/octet-stream",
            )
        )
        self.assertTrue(
            _looks_like_html(
                b"  <HTML><body>login</body></HTML>",
                None,
            )
        )
        self.assertFalse(_looks_like_html(b"%PDF-1.7\nbody", "application/pdf"))

    def test_content_matches_filename_checks_binary_magic(self) -> None:
        """PDF와 ZIP 기반 Office 문서는 확장자와 실제 magic byte가 일치해야 한다."""

        self.assertTrue(_content_matches_filename("guide.pdf", b"%PDF-1.7\ncontent"))
        self.assertFalse(
            _content_matches_filename(
                "guide.pdf",
                b"<html><body>login</body></html>",
            )
        )
        for filename in ("answer.docx", "table.xlsx", "slides.pptx", "sample.zip"):
            with self.subTest(filename=filename):
                self.assertTrue(
                    _content_matches_filename(filename, b"PK\x03\x04archive-data")
                )
                self.assertFalse(
                    _content_matches_filename(filename, b"<html>viewer</html>")
                )
        self.assertTrue(_content_matches_filename("code.py", b"print('ok')\n"))
        self.assertFalse(_content_matches_filename("empty.txt", b""))

    def test_viewer_file_url_returns_only_allowed_eclass_pluginfile(self) -> None:
        """HTML wrapper에서는 같은 E-Class의 pluginfile URL만 원본 후보로 허용한다."""

        wrapper = b"""
        <html><body>
          <iframe src="/local/viewer/index.html"></iframe>
          <a href="https://evil.example/pluginfile.php/steal.pdf">evil</a>
          <object data="/pluginfile.php/77/mod_assign/intro/guide.pdf?x=1&amp;y=2">
          </object>
        </body></html>
        """

        result = _viewer_file_url(
            wrapper,
            base_url="https://learn.hansung.ac.kr/mod/assign/view.php?id=1140975",
            settings=self.settings,
        )

        self.assertEqual(
            result,
            "https://learn.hansung.ac.kr/pluginfile.php/77/mod_assign/intro/guide.pdf?x=1&y=2",
        )

    def test_viewer_file_url_rejects_external_or_non_pluginfile_candidates(self) -> None:
        wrapper = b"""
        <html><body>
          <a href="https://learn.hansung.ac.kr.evil.example/pluginfile.php/steal.pdf">
            external
          </a>
          <a href="/mod/assign/view.php?id=1140975">same page</a>
        </body></html>
        """

        self.assertIsNone(
            _viewer_file_url(
                wrapper,
                base_url="https://learn.hansung.ac.kr/mod/assign/view.php?id=1140975",
                settings=self.settings,
            )
        )


if __name__ == "__main__":
    unittest.main()
