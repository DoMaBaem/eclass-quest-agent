"""과제 마감과 강의 출석 인정 종료를 24·6·1시간 구간으로 탐지한다."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from app.schemas.domain import Assignment, EntityStatus, Lecture
from app.sync.schemas import DeadlineCandidate


class DeadlineService:
    """완료된 항목을 제외하고 현재 해당하는 가장 긴급한 구간만 선택한다."""

    THRESHOLDS = (1, 6, 24)

    def evaluate(
        self,
        assignments: list[Assignment],
        lectures: list[Lecture],
        *,
        now: datetime | None = None,
    ) -> list[DeadlineCandidate]:
        current = now or datetime.now(timezone.utc)
        candidates: list[DeadlineCandidate] = []
        for assignment in assignments:
            if assignment.submitted is True or assignment.status is EntityStatus.COMPLETE:
                continue
            candidate = self._candidate(
                entity_type="assignment",
                entity_id=assignment.id,
                title=assignment.title,
                deadline=assignment.due_at,
                prefix="assignment_due",
                current=current,
            )
            if candidate:
                candidates.append(candidate)

        for lecture in lectures:
            if lecture.status is EntityStatus.COMPLETE or lecture.attendance_status is EntityStatus.COMPLETE:
                continue
            candidate = self._candidate(
                entity_type="lecture",
                entity_id=lecture.id,
                title=lecture.title,
                deadline=lecture.available_until,
                prefix="attendance_due",
                current=current,
            )
            if candidate:
                candidates.append(candidate)
        return candidates

    def _candidate(
        self,
        *,
        entity_type: str,
        entity_id: str,
        title: str,
        deadline: datetime | None,
        prefix: str,
        current: datetime,
    ) -> DeadlineCandidate | None:
        if deadline is None:
            return None
        normalized_deadline = self._aware(deadline)
        remaining_seconds = (normalized_deadline - self._aware(current)).total_seconds()
        if remaining_seconds <= 0 or remaining_seconds > 24 * 3600:
            return None
        remaining_hours = remaining_seconds / 3600
        threshold = next(hours for hours in self.THRESHOLDS if remaining_hours <= hours)
        notification_type = f"{prefix}_{threshold}h"
        raw_key = f"{entity_type}|{entity_id}|{notification_type}|{normalized_deadline.isoformat()}"
        dedupe_key = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
        return DeadlineCandidate(
            entity_type=entity_type,
            entity_id=entity_id,
            notification_type=notification_type,
            dedupe_key=dedupe_key,
            payload={
                "entity_type": entity_type,
                "entity_id": entity_id,
                "title": title,
                "deadline": normalized_deadline.isoformat(),
                "threshold_hours": threshold,
            },
        )

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
