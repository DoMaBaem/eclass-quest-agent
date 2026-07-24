"""배포 이미지에 로컬 Runtime 모듈이 빠지지 않는지 확인한다."""

from __future__ import annotations

import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class DeploymentPackagingTest(unittest.TestCase):
    def test_dockerfile_copies_both_local_mcp_servers(self) -> None:
        dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")

        self.assertRegex(dockerfile, r"COPY(?: --chown=\S+)? mcp_server ./mcp_server")
        self.assertRegex(
            dockerfile,
            r"COPY(?: --chown=\S+)? document_mcp_server ./document_mcp_server",
        )

    def test_desktop_image_contains_web_desktop_audio_and_autostart(self) -> None:
        dockerfile = (PROJECT_ROOT / "Dockerfile.desktop").read_text(encoding="utf-8")
        compose = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

        self.assertIn("lscr.io/linuxserver/webtop:ubuntu-xfce", dockerfile)
        self.assertIn("python -m playwright install --with-deps chromium", dockerfile)
        self.assertIn("/usr/local/bin:/lsiopy/bin", dockerfile)
        self.assertIn("/etc/xdg/autostart/eclass-quest.desktop", dockerfile)
        self.assertIn("scripts/desktop_start.sh", dockerfile)
        self.assertIn('Exec=xterm -maximized -hold -title "E-Class Quest"', dockerfile)
        self.assertNotIn("Exec=xfce4-terminal", dockerfile)
        self.assertIn('SELKIES_AUDIO_ENABLED: "true"', compose)
        self.assertIn('"127.0.0.1:3001:3001"', compose)
        self.assertIn("ollama/ollama:latest", compose)
        self.assertIn("curlimages/curl:latest", compose)
        self.assertIn("http://ollama:11434/api/chat", compose)
        self.assertIn("qwen3:0.6b", compose)
        self.assertIn("http://ollama:11434/api/pull", compose)
        self.assertIn("ollama_data:/root/.ollama", compose)

    def test_local_launcher_starts_mysql_migrates_and_then_runs_tui(self) -> None:
        launcher = (PROJECT_ROOT / "scripts/local_launcher.py").read_text(encoding="utf-8")
        posix_wrapper = (PROJECT_ROOT / "run.sh").read_text(encoding="utf-8")
        powershell_wrapper = (PROJECT_ROOT / "run.ps1").read_text(encoding="utf-8")
        cmd_wrapper = (PROJECT_ROOT / "run.cmd").read_text(encoding="utf-8")

        mysql = launcher.index('"compose", "up", "-d", "mysql"')
        migration = launcher.index('"alembic", "upgrade", "head"')
        tui = launcher.rindex('"app.main"')
        self.assertLess(mysql, migration)
        self.assertLess(migration, tui)
        self.assertIn(".venv/bin/python", posix_wrapper)
        self.assertIn("scripts.local_launcher", posix_wrapper)
        self.assertIn(".venv\\Scripts\\python.exe", powershell_wrapper)
        self.assertIn("scripts.local_launcher", powershell_wrapper)
        self.assertIn(".venv\\Scripts\\python.exe", cmd_wrapper)
        self.assertIn("scripts.local_launcher", cmd_wrapper)

    def test_development_mysql_port_is_bound_to_localhost_only(self) -> None:
        compose = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

        self.assertIn('"127.0.0.1:3306:3306"', compose)


if __name__ == "__main__":
    unittest.main()
