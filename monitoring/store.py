"""
monitoring/store.py — In-memory monitoring record store.

Persistence layer for CaseMonitorRecord objects.

CURRENT IMPLEMENTATION: In-memory dict with asyncio lock.
Records survive the process lifetime but are lost on restart.

PRODUCTION NOTE:
    For production persistence across restarts/deployments, this store
    should be backed by Firestore (collection: _juris_monitors/{user_id}/cases/{id}).
    The interface is identical — swap the implementation without changing callers.

DESIGN:
    - Single global instance accessed via get_monitoring_store()
    - asyncio.Lock ensures safe concurrent access
    - No external dependencies
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from monitoring.models import CaseMonitorRecord, MonitoringStatus


class MonitoringStore:
    """
    In-memory store for CaseMonitorRecord objects.

    Thread-safe via asyncio.Lock for use in async FastAPI contexts.
    """

    def __init__(self) -> None:
        self._records: dict[str, CaseMonitorRecord] = {}  # id → record
        self._lock = asyncio.Lock()

    async def upsert(self, record: CaseMonitorRecord) -> None:
        """Insert or replace a record (keyed by record.id)."""
        async with self._lock:
            self._records[record.id] = record

    async def get(self, record_id: str, user_id: str | None = None) -> CaseMonitorRecord | None:
        """Return a record only when it belongs to the requested user."""
        record = self._records.get(record_id)
        if record is None or (user_id is not None and record.user_id != user_id):
            return None
        return record

    async def list_for_user(self, user_id: str) -> list[CaseMonitorRecord]:
        """Return all records belonging to the given user."""
        return [
            r for r in self._records.values()
            if r.user_id == user_id
        ]

    async def get_for_user_and_case(
        self, user_id: str, case_id: str
    ) -> CaseMonitorRecord | None:
        """Return the active monitor for a specific user+case, if any."""
        for r in self._records.values():
            if r.user_id == user_id and r.case_id == case_id:
                return r
        return None

    async def list_due(
        self,
        before: datetime,
        batch_size: int,
    ) -> list[CaseMonitorRecord]:
        """
        Return ACTIVE records that haven't been checked since `before`.

        Used by the scheduler to find records due for their periodic check.
        """
        due = [
            r for r in self._records.values()
            if r.status == MonitoringStatus.ACTIVE
            and (r.last_checked_at is None or r.last_checked_at < before)
        ]
        return due[:batch_size]

    async def delete(self, record_id: str, user_id: str | None = None) -> bool:
        """Delete a record only if it belongs to the requested user."""
        async with self._lock:
            record = self._records.get(record_id)
            if record is None or (user_id is not None and record.user_id != user_id):
                return False
            self._records.pop(record_id, None)
            return True

    async def count_for_user(self, user_id: str) -> int:
        """Return the number of active monitors for the user."""
        return sum(1 for r in self._records.values()
                   if r.user_id == user_id and r.status == MonitoringStatus.ACTIVE)

    async def all_records(self) -> list[CaseMonitorRecord]:
        """Return all records. Used for diagnostics/testing only."""
        return list(self._records.values())


# ── Singleton ───────────────────────────────────────────────────────────────────

_store = MonitoringStore()


def get_monitoring_store() -> MonitoringStore:
    """Return the global singleton MonitoringStore."""
    return _store
