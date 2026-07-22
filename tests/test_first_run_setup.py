"""비개발자용 최초 실행 설정과 암호화 저장 경로 테스트."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.config import Settings, get_settings
from app.setup_store import LocalSetupStore, remove_legacy_setup_env
from app.setup_wizard import run_setup_wizard
from mcp_server.browser.credential_login import automatic_login_available


class LocalSetupStoreTest(unittest.TestCase):
    def test_round_trip_keeps_secrets_out_of_plain_settings_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = LocalSetupStore(Path(directory))
            store.save(
                openai_model="model-choice",
                openai_api_key="api-secret",
                eclass_username="student-secret",
                eclass_password="password-secret",
            )

            values = store.load_overrides()

            self.assertTrue(store.is_complete())
            self.assertEqual(values["openai_model"], "model-choice")
            self.assertEqual(values["openai_api_key"], "api-secret")
            self.assertNotIn("api-secret", store.settings_path.read_text(encoding="utf-8"))
            self.assertNotIn(b"password-secret", store.credentials_path.read_bytes())
            self.assertEqual(store.key_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(store.credentials_path.stat().st_mode & 0o777, 0o600)

    def test_saved_setup_overrides_legacy_environment_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = LocalSetupStore(Path(directory))
            store.save(
                openai_model="selected-model",
                openai_api_key="saved-api-key",
                eclass_username="saved-user",
                eclass_password="saved-password",
            )

            settings = get_settings(store)

            self.assertEqual(settings.openai_model, "selected-model")
            self.assertEqual(settings.openai_api_key, "saved-api-key")
            self.assertEqual(settings.eclass_username.get_secret_value(), "saved-user")
            self.assertEqual(settings.eclass_password.get_secret_value(), "saved-password")

    def test_automatic_login_and_retention_are_fixed_defaults(self) -> None:
        settings = Settings(
            _env_file=None,
            eclass_auto_login=False,
            download_retention_hours=1,
            eclass_username="student",
            eclass_password="password",
        )

        self.assertTrue(automatic_login_available(settings))
        self.assertEqual(settings.download_retention_hours, 24)


class SetupWizardTest(unittest.TestCase):
    def test_wizard_runs_once_and_uses_masked_secret_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = LocalSetupStore(Path(directory) / "config")
            env_path = Path(directory) / ".env"
            env_path.write_text("MYSQL_URL=mysql://local\n", encoding="utf-8")
            visible_inputs = iter(("chosen-model", "student"))
            secrets = iter(("api-key", "password"))
            messages: list[str] = []
            with patch("app.setup_wizard.remove_legacy_setup_env", return_value=False):
                changed = run_setup_wizard(
                    store,
                    input_fn=lambda _prompt: next(visible_inputs),
                    secret_input_fn=lambda _prompt: next(secrets),
                    output_fn=messages.append,
                )
                repeated = run_setup_wizard(store, output_fn=messages.append)

            self.assertTrue(changed)
            self.assertFalse(repeated)
            self.assertEqual(store.load_overrides()["openai_model"], "chosen-model")
            self.assertTrue(any("설정이 저장" in message for message in messages))

    def test_legacy_env_cleanup_preserves_non_secret_connections(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "MYSQL_URL=mysql://local\n"
                "OPENAI_API_KEY=secret\n"
                "OPENAI_MODEL=old-model\n"
                "ECLASS_BASE_URL=https://learn.hansung.ac.kr\n"
                "ECLASS_USERNAME=user\n"
                "ECLASS_PASSWORD=password\n"
                "ECLASS_AUTO_LOGIN=true\n"
                "DOWNLOAD_RETENTION_HOURS=24\n",
                encoding="utf-8",
            )

            self.assertTrue(remove_legacy_setup_env(path))
            content = path.read_text(encoding="utf-8")

            self.assertIn("MYSQL_URL=mysql://local", content)
            self.assertIn("ECLASS_BASE_URL=https://learn.hansung.ac.kr", content)
            self.assertNotIn("OPENAI_API_KEY", content)
            self.assertNotIn("ECLASS_PASSWORD", content)
            self.assertNotIn("DOWNLOAD_RETENTION_HOURS", content)

    def test_main_runs_wizard_before_tui_when_setup_is_missing(self) -> None:
        store = MagicMock()
        store.is_complete.return_value = False
        settings = Settings(_env_file=None, openai_api_key="configured")
        app = MagicMock()
        with (
            patch("app.main.LocalSetupStore", return_value=store),
            patch("app.main.run_setup_wizard") as wizard,
            patch("app.main.get_settings", return_value=settings),
            patch("app.main.EclassQuestApp", return_value=app),
        ):
            from app.main import main

            result = main([])

        self.assertEqual(result, 0)
        wizard.assert_called_once_with(store, force=False)
        app.run.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
