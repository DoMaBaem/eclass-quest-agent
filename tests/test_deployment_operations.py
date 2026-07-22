"""9단계 운영 배포 계약이 개발 설정이나 평문 Secret으로 퇴행하지 않는지 검증한다."""

from __future__ import annotations

import asyncio
import json
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.container_entrypoint import build_mysql_url, prepare_environment
from scripts.deployment_smoke import ECLASS_TOOLS, check
from scripts.init_deployment_secrets import write_secret
from scripts.verify_mcp_stdio import EXPECTED_TOOLS


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ContainerEntrypointTest(unittest.TestCase):
    def test_secret_password_is_encoded_into_mysql_url_without_auxiliary_plain_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            password_file = Path(directory) / "password"
            password_file.write_text("p@ss/word#value\n", encoding="utf-8")
            environment = {
                "DEPLOYMENT_ROLE": "migration",
                "MYSQL_HOST": "mysql",
                "MYSQL_PORT": "3306",
                "MYSQL_DATABASE": "eclass_quest_staging",
                "MYSQL_USER": "eclass_app",
                "MYSQL_PASSWORD_FILE": str(password_file),
            }

            prepare_environment(environment)

        self.assertEqual(
            environment["MYSQL_URL"],
            "mysql+asyncmy://eclass_app:p%40ss%2Fword%23value@mysql:3306/"
            "eclass_quest_staging?charset=utf8mb4",
        )
        self.assertNotIn("MYSQL_PASSWORD", environment)

    def test_file_secret_mode_refuses_missing_external_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            api_key_file = Path(directory) / "openai_api_key"
            api_key_file.write_text("test-key\n", encoding="utf-8")
            environment = {
                "MYSQL_URL": "mysql+asyncmy://user:pass@mysql/db",
                "OPENAI_API_KEY_FILE": str(api_key_file),
            }
            with self.assertRaisesRegex(RuntimeError, "배포 필수 Secret"):
                prepare_environment(environment)

    def test_mysql_url_builder_keeps_development_url_without_component_mode(self) -> None:
        existing = "mysql+asyncmy://dev:secret@localhost/dev"
        self.assertEqual(build_mysql_url({"MYSQL_URL": existing}), existing)


class SecretInitializationTest(unittest.TestCase):
    def test_secret_file_is_created_with_owner_only_permission_and_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secret.txt"
            self.assertTrue(write_secret(path, "first"))
            self.assertFalse(write_secret(path, "second"))
            self.assertEqual(path.read_text(encoding="utf-8"), "first\n")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


@unittest.skipUnless(shutil.which("docker"), "Docker Compose가 설치된 환경에서만 검증합니다.")
class ComposeDeploymentTest(unittest.TestCase):
    @staticmethod
    def config(environment: str) -> dict[str, object]:
        result = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                "docker-compose.yml",
                "-f",
                f"compose.{environment}.yml",
                "--profile",
                "app",
                "config",
                "--format",
                "json",
            ],
            cwd=PROJECT_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(result.stdout)

    def test_production_config_has_no_development_password_or_public_mysql_port(self) -> None:
        config = self.config("production")
        serialized = json.dumps(config)
        app = config["services"]["app"]
        mysql = config["services"]["mysql"]

        self.assertNotIn("local_password", serialized)
        self.assertNotIn("local_root_password", serialized)
        self.assertEqual(app["environment"]["MYSQL_URL"], "")
        self.assertNotIn("ports", mysql)
        self.assertNotIn("MYSQL_PASSWORD", mysql["environment"])
        self.assertEqual(mysql["environment"]["MYSQL_PASSWORD_FILE"], "/run/secrets/mysql_app_password")
        self.assertEqual(config["services"]["migrate"]["command"], ["alembic", "upgrade", "head"])
        init = config["services"]["app-data-init"]
        self.assertEqual(init["user"], "0:0")
        self.assertIn("chown -R", " ".join(init["command"]))
        self.assertEqual(
            app["depends_on"]["app-data-init"]["condition"],
            "service_completed_successfully",
        )

    def test_staging_and_production_use_distinct_database_and_volumes(self) -> None:
        staging = self.config("staging")
        production = self.config("production")

        self.assertNotEqual(staging["name"], production["name"])
        self.assertNotEqual(
            staging["services"]["mysql"]["environment"]["MYSQL_DATABASE"],
            production["services"]["mysql"]["environment"]["MYSQL_DATABASE"],
        )
        staging_volumes = {volume["source"] for volume in staging["services"]["mysql"]["volumes"]}
        production_volumes = {volume["source"] for volume in production["services"]["mysql"]["volumes"]}
        self.assertTrue(staging_volumes.isdisjoint(production_volumes))


class DeploymentObservabilityTest(unittest.TestCase):
    def test_live_mcp_verifier_and_deployment_smoke_expect_same_registry(self) -> None:
        self.assertEqual(EXPECTED_TOOLS, ECLASS_TOOLS)
        self.assertIn("get_dashboard_snapshot", EXPECTED_TOOLS)
        self.assertGreaterEqual(len(EXPECTED_TOOLS), 20)

    def test_check_result_does_not_expose_exception_message(self) -> None:
        async def failing() -> str:
            raise RuntimeError("secret-token-should-not-be-logged")

        result = asyncio.run(check("openai", "OPENAI_HEALTH_FAILED", failing))

        self.assertEqual(result.status, "FAIL")
        self.assertEqual(result.error_code, "OPENAI_HEALTH_FAILED")
        self.assertEqual(result.detail, "RuntimeError")
        self.assertNotIn("secret-token", json.dumps(result.__dict__))

    def test_backup_restore_use_secret_file_and_explicit_confirmation(self) -> None:
        backup = (PROJECT_ROOT / "scripts/mysql_backup.sh").read_text(encoding="utf-8")
        restore = (PROJECT_ROOT / "scripts/mysql_restore.sh").read_text(encoding="utf-8")

        self.assertIn("/run/secrets/mysql_root_password", backup)
        self.assertIn("MYSQL_PWD", backup)
        self.assertIn("sha256sum", backup)
        self.assertIn("/run/secrets/mysql_root_password", restore)
        self.assertIn('CONFIRM="${3:-}"', restore)
        self.assertIn('"--yes"', restore)

    def test_dockerfile_runs_as_non_root_with_secret_entrypoint(self) -> None:
        dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn("PLAYWRIGHT_BROWSERS_PATH=/ms-playwright", dockerfile)
        self.assertIn("USER eclass", dockerfile)
        self.assertIn('ENTRYPOINT ["python", "/app/scripts/container_entrypoint.py"]', dockerfile)


if __name__ == "__main__":
    unittest.main()
