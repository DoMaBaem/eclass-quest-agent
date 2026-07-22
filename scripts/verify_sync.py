"""6단계 실제 E-Class → MySQL 동기화를 개인 내용 출력 없이 검증한다."""

from __future__ import annotations

import asyncio
import argparse
import sys
from pathlib import Path

from sqlalchemy import func, select


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import get_settings
from app.storage.models import EntitySnapshotModel, SyncHistoryModel
from app.sync.schemas import SyncStatus, SyncTrigger
from app.sync.service import SyncService


async def verify(trigger: SyncTrigger = SyncTrigger.STARTUP) -> None:
    service = SyncService(get_settings())
    try:
        result = await service.sync(trigger)
        if result.status is not SyncStatus.COMPLETED:
            raise RuntimeError(f"동기화 실패: {result.error_code or result.status.value}")
        print(f"동기화 트리거: {result.trigger.value}", flush=True)
        term = result.selected_term
        print(f"조회 학기: {term.year}년 {term.semester_name}", flush=True)
        print(f"관찰 엔터티: {result.observed_count}개", flush=True)
        print(f"변경: {result.change_count}개, 마감 알림: {result.deadline_count}개", flush=True)
        async with service.database.session() as session:
            snapshots = await session.scalar(
                select(func.count(EntitySnapshotModel.id)).where(
                    EntitySnapshotModel.user_id == service.user_id
                )
            )
            completed = await session.scalar(
                select(func.count(SyncHistoryModel.id)).where(
                    SyncHistoryModel.user_id == service.user_id,
                    SyncHistoryModel.status == "COMPLETED",
                )
            )
        print(f"MySQL snapshot: {snapshots or 0}개", flush=True)
        print(f"완료된 동기화 기록: {completed or 0}개", flush=True)
    finally:
        await service.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="실제 E-Class와 MySQL 동기화를 검증합니다.")
    parser.add_argument(
        "--trigger",
        choices=("startup", "heartbeat", "manual"),
        default="startup",
        help="검증할 동기화 트리거입니다. 기본값은 startup입니다.",
    )
    args = parser.parse_args()
    trigger = {
        "startup": SyncTrigger.STARTUP,
        "heartbeat": SyncTrigger.HEARTBEAT,
        "manual": SyncTrigger.MANUAL,
    }[args.trigger]
    asyncio.run(verify(trigger))
