"""운영체제별 셸이 공유하는 로컬 런처의 회귀 테스트."""

from __future__ import annotations

import os
import subprocess
import unittest
from unittest.mock import call, patch

from scripts import local_launcher


def _completed(
    command: tuple[str, ...] = ("command",), *, returncode: int = 0, stdout: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr="")


class LocalLauncherTest(unittest.TestCase):
    def test_external_mysql_skips_docker_then_runs_migration_and_app(self) -> None:
        calls: list[tuple[str, ...]] = []

        def fake_run(command, *, capture=False):
            del capture
            calls.append(tuple(command))
            return _completed(tuple(command))

        with (
            patch.dict(os.environ, {"MYSQL_URL": "mysql+asyncmy://external/db"}, clear=False),
            patch.object(local_launcher, "_run", side_effect=fake_run),
            patch.object(local_launcher, "_prepare_local_mysql") as prepare,
        ):
            result = local_launcher.launch(("--setup",))

        self.assertEqual(result, 0)
        prepare.assert_not_called()
        self.assertEqual(calls[0][1:5], ("-m", "alembic", "upgrade", "head"))
        self.assertEqual(calls[1][1:], ("-m", "app.main", "--setup"))

    def test_default_database_starts_compose_and_waits_for_health(self) -> None:
        results = (
            _completed(("docker", "info")),
            _completed(("docker", "compose")),
            _completed(("docker", "inspect"), stdout="healthy\n"),
        )
        with (
            patch.object(local_launcher.shutil, "which", return_value="docker"),
            patch.object(local_launcher, "_run", side_effect=results) as run,
            patch.object(local_launcher.time, "sleep") as sleep,
        ):
            local_launcher._prepare_local_mysql()

        self.assertEqual(run.call_count, 3)
        self.assertEqual(
            run.call_args_list[1],
            call(("docker", "compose", "up", "-d", "mysql")),
        )
        sleep.assert_not_called()

    def test_help_does_not_require_docker_or_database(self) -> None:
        with (
            patch.object(local_launcher, "_run", return_value=_completed()) as run,
            patch.object(local_launcher, "_prepare_local_mysql") as prepare,
        ):
            result = local_launcher.launch(("--help",))

        self.assertEqual(result, 0)
        prepare.assert_not_called()
        self.assertEqual(run.call_args.args[0][1:], ("-m", "app.main", "--help"))


if __name__ == "__main__":
    unittest.main()
