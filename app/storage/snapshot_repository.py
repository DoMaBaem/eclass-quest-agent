"""정규화 수집 결과의 변경 여부를 판정하는 Repository.

첫 수집은 비교 기준인 baseline만 만들고 알림을 만들지 않는다. 이후 같은 엔터티의 fingerprint가
바뀔 때만 ChangeEvent를 생성하며, DB 고유 인덱스와 사전 조회로 중복 알림을 막는다.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.workflow import ManagerInputEvent
from app.storage.models import ChangeEventModel, DownloadedFileModel, EntitySnapshotModel


@dataclass(frozen=True)
class SnapshotChange:
    """snapshot 저장 결과를 호출자에게 간단히 알려 주는 값 객체."""

    status: str  # baseline | unchanged | updated
    fingerprint: str
    change_event_created: bool = False
    runtime_event_id: str | None = None


class SnapshotRepository:
    """최초 수집은 기준점만 만들고, 이후 실제 값 변경만 이벤트로 남긴다."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def fingerprint(payload: dict[str, object]) -> str:
        """키 순서에 영향받지 않는 동일한 JSON을 만든 뒤 SHA-256 해시를 계산한다."""

        # sort_keys와 고정 separators가 없으면 내용이 같아도 JSON 문자열 차이로 해시가 달라질 수 있다.
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    async def record_snapshot(
        self,
        *,
        user_id: str,
        entity_type: str,
        entity_id: str,
        payload: dict[str, object],
        notify_on_first_seen: bool = False,
    ) -> SnapshotChange:
        """snapshot을 저장하고 baseline/unchanged/updated 중 하나를 반환한다."""

        fingerprint = self.fingerprint(payload)
        # 가장 최근 관측값과 먼저 비교하면 대다수 unchanged 요청을 빠르게 끝낼 수 있다.
        previous = await self.session.scalar(
            select(EntitySnapshotModel)
            .where(
                EntitySnapshotModel.user_id == user_id,
                EntitySnapshotModel.entity_type == entity_type,
                EntitySnapshotModel.entity_id == entity_id,
            )
            .order_by(EntitySnapshotModel.observed_at.desc(), EntitySnapshotModel.id.desc())
            .limit(1)
        )
        if previous is not None and previous.fingerprint == fingerprint:
            return SnapshotChange(status="unchanged", fingerprint=fingerprint)

        # 예전에 같은 값으로 되돌아간 경우에는 유니크 인덱스가 중복 스냅샷을 막는다.
        already_stored = await self.session.scalar(
            select(EntitySnapshotModel.id).where(
                EntitySnapshotModel.user_id == user_id,
                EntitySnapshotModel.entity_type == entity_type,
                EntitySnapshotModel.entity_id == entity_id,
                EntitySnapshotModel.fingerprint == fingerprint,
            )
        )
        if already_stored is None:
            self.session.add(
                EntitySnapshotModel(
                    user_id=user_id,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    fingerprint=fingerprint,
                    payload=payload,
                )
            )
            await self.session.flush()

        if previous is None and not notify_on_first_seen:
            # 최초 수집은 비교 대상이 없으므로 변경 알림을 만들지 않는다.
            return SnapshotChange(status="baseline", fingerprint=fingerprint)

        # 같은 변경 이벤트가 재시도나 동시 요청으로 이미 생성됐는지 확인한다.
        event_exists = await self.session.scalar(
            select(ChangeEventModel.id).where(
                ChangeEventModel.user_id == user_id,
                ChangeEventModel.entity_type == entity_type,
                ChangeEventModel.entity_id == entity_id,
                ChangeEventModel.fingerprint == fingerprint,
            )
        )
        if event_exists is not None:
            return SnapshotChange(status="updated", fingerprint=fingerprint)
        event = ChangeEventModel(
            user_id=user_id,
            entity_type=entity_type,
            entity_id=entity_id,
            fingerprint=fingerprint,
            event_type="created" if previous is None else "updated",
            payload=payload,
        )
        self.session.add(event)
        await self.session.flush()
        return SnapshotChange(
            status="created" if previous is None else "updated",
            fingerprint=fingerprint,
            change_event_created=True,
            runtime_event_id=event.runtime_event_id,
        )

    async def get_latest_snapshot(
        self, *, user_id: str, entity_type: str, entity_id: str
    ) -> dict[str, object] | None:
        """정규화한 Pydantic payload를 가장 최근 스냅샷에서 안전하게 읽는다."""

        row = await self.session.scalar(
            select(EntitySnapshotModel)
            .where(
                EntitySnapshotModel.user_id == user_id,
                EntitySnapshotModel.entity_type == entity_type,
                EntitySnapshotModel.entity_id == entity_id,
            )
            .order_by(EntitySnapshotModel.observed_at.desc(), EntitySnapshotModel.id.desc())
            .limit(1)
        )
        return dict(row.payload) if row is not None else None

    async def get_pending_manager_events(
        self, *, user_id: str, limit: int = 50
    ) -> list[ManagerInputEvent]:
        """아직 처리하지 않은 ChangeEvent를 Manager용 안전한 계약으로 변환한다."""

        if not 1 <= limit <= 200:
            raise ValueError("limit은 1 이상 200 이하여야 합니다.")
        rows = (
            await self.session.scalars(
                select(ChangeEventModel)
                .where(
                    ChangeEventModel.user_id == user_id,
                    ChangeEventModel.manager_status == "PENDING",
                )
                .order_by(ChangeEventModel.created_at.asc(), ChangeEventModel.id.asc())
                .limit(limit)
            )
        ).all()
        return [
            ManagerInputEvent(
                event_id=row.runtime_event_id,
                change_type=row.event_type,
                entity_type=row.entity_type,
                entity_id=row.entity_id,
                payload=dict(row.payload),
                created_at=row.created_at,
            )
            for row in rows
        ]

    async def mark_manager_event_processed(
        self,
        *,
        event_id: str,
        request_id: str,
        processed_at: datetime | None = None,
    ) -> bool:
        """Manager 처리가 끝난 이벤트를 재전달되지 않도록 완료 상태로 바꾼다."""

        row = await self.session.scalar(
            select(ChangeEventModel).where(ChangeEventModel.runtime_event_id == event_id).with_for_update()
        )
        if row is None or row.manager_status == "PROCESSED":
            return False
        row.manager_status = "PROCESSED"
        row.manager_request_id = request_id
        row.processed_at = processed_at or datetime.now(timezone.utc)
        await self.session.flush()
        return True

    async def cleanup_expired_downloads(
        self, *, storage_root: Path, now: datetime | None = None
    ) -> int:
        """보존 기한이 지난 임시 파일을 지우고 삭제 시각을 남긴다.

        DB에 잘못된 경로가 들어와도 ``storage_root`` 밖의 파일은 절대 삭제하지 않는다.
        """

        current = now or datetime.now(timezone.utc)
        rows = list(
            (
                await self.session.scalars(
                    select(DownloadedFileModel).where(
                        DownloadedFileModel.expires_at < current,
                        DownloadedFileModel.deleted_at.is_(None),
                    )
                )
            ).all()
        )
        root = storage_root.resolve()
        deleted = 0
        for row in rows:
            candidate = Path(row.storage_path).resolve()
            # DB 값이 조작되거나 잘못 저장돼도 허용된 다운로드 루트 밖은 삭제하지 않는다.
            if not candidate.is_relative_to(root):
                continue
            candidate.unlink(missing_ok=True)
            row.deleted_at = current
            deleted += 1
        await self.session.flush()
        return deleted

    async def delete_expired_downloads(self, *, now: datetime | None = None) -> int:
        """만료된 임시 다운로드 레코드를 지운다. 파일 삭제는 호출 계층이 수행한다."""

        current = now or datetime.now(timezone.utc)
        result = await self.session.execute(
            delete(DownloadedFileModel).where(
                DownloadedFileModel.expires_at < current,
                DownloadedFileModel.deleted_at.is_not(None),
            )
        )
        return int(result.rowcount or 0)

    @staticmethod
    def expired_paths(rows: list[DownloadedFileModel], *, now: datetime | None = None) -> list[Path]:
        current = now or datetime.now(timezone.utc)
        return [Path(row.storage_path) for row in rows if row.expires_at < current and row.deleted_at is None]
