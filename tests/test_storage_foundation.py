"""Manager Runtime 준비용 ORM과 migration 고정 상태 테스트."""

import unittest
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from app.schemas.workflow import ManagerInputEvent

from app.storage.models import (
    AttachmentModel,
    ChangeEventModel,
    EclassSessionModel,
    MissionModel,
    NotificationHistoryModel,
    SyncHistoryModel,
    UserModel,
    WorkflowRunModel,
)


class StorageFoundationTest(unittest.TestCase):
    def test_manager_runtime_tables_are_registered(self) -> None:
        self.assertEqual(MissionModel.__tablename__, "missions")
        self.assertEqual(NotificationHistoryModel.__tablename__, "notification_history")
        self.assertEqual(SyncHistoryModel.__tablename__, "sync_history")
        self.assertEqual(AttachmentModel.__tablename__, "attachments")

    def test_workflow_run_records_trigger_origin(self) -> None:
        columns = WorkflowRunModel.__table__.columns

        self.assertIn("trigger_type", columns)
        self.assertIn("event_id", columns)
        self.assertIn("initiated_by", columns)

    def test_change_event_has_durable_manager_delivery_fields(self) -> None:
        columns = ChangeEventModel.__table__.columns

        self.assertIn("runtime_event_id", columns)
        self.assertIn("manager_status", columns)
        self.assertIn("manager_request_id", columns)
        self.assertIn("processed_at", columns)

    def test_database_session_model_stores_only_encrypted_reference(self) -> None:
        columns = set(EclassSessionModel.__table__.columns.keys())

        self.assertIn("encrypted_state_ref", columns)
        self.assertTrue({"password", "login_id", "cookie", "storage_state"}.isdisjoint(columns))

    def test_user_settings_reject_credentials_even_when_nested(self) -> None:
        with self.assertRaises(ValueError):
            UserModel(id="user", settings_json={"profile": {"password": "secret"}})

    def test_manager_event_rejects_credentials(self) -> None:
        with self.assertRaises(ValidationError):
            ManagerInputEvent(
                event_id="event-id",
                change_type="updated",
                entity_type="assignment",
                entity_id="assignment-1",
                payload={"cookie": "secret"},
                created_at=datetime.now(timezone.utc),
            )

    def test_initial_migration_does_not_use_dynamic_metadata(self) -> None:
        source = Path("alembic/versions/20260717_0001_initial_schema.py").read_text(encoding="utf-8")

        self.assertNotIn("Base.metadata.create_all", source)
        self.assertIn('op.create_table(\n        "users"', source)

    def test_second_revision_points_to_initial_revision(self) -> None:
        source = Path("alembic/versions/20260720_0002_manager_runtime_foundation.py").read_text(
            encoding="utf-8"
        )

        self.assertIn('down_revision = "20260717_0001"', source)
        self.assertIn('"missions"', source)

    def test_schema_hardening_revision_is_chained(self) -> None:
        source = Path("alembic/versions/20260720_0003_schema_hardening.py").read_text(encoding="utf-8")

        self.assertIn('down_revision = "20260720_0002"', source)
        self.assertIn('"attachments"', source)
        self.assertIn('"entity_type"', source)

    def test_manager_event_revision_is_chained(self) -> None:
        source = Path("alembic/versions/20260720_0004_manager_event_queue.py").read_text(encoding="utf-8")

        self.assertIn('down_revision = "20260720_0003"', source)
        self.assertIn('"manager_status"', source)


if __name__ == "__main__":
    unittest.main()
