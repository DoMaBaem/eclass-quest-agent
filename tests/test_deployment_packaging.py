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

    def test_local_launcher_starts_mysql_migrates_and_then_runs_tui(self) -> None:
        launcher = (PROJECT_ROOT / "run.sh").read_text(encoding="utf-8")

        mysql = launcher.index("docker compose up -d mysql")
        migration = launcher.index("-m alembic upgrade head")
        tui = launcher.rindex("-m app.main")
        self.assertLess(mysql, migration)
        self.assertLess(migration, tui)
        self.assertIn(".venv/bin/python", launcher)
        self.assertIn('export MYSQL_URL="${MYSQL_URL:-$LOCAL_MYSQL_URL}"', launcher)

    def test_development_mysql_port_is_bound_to_localhost_only(self) -> None:
        compose = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

        self.assertIn('"127.0.0.1:3306:3306"', compose)


if __name__ == "__main__":
    unittest.main()
