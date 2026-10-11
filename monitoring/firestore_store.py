"""Firestore-backed monitoring persistence.

JURIS monitoring state is not case data; it stores references to LED case IDs.
The repository is user-scoped so a monitor ID can never be read or deleted
across users through this abstraction.
"""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone
from typing import Any
from core.config import settings
from core.errors import ConfigurationError
from monitoring.models import CaseMonitorRecord, MonitoringStatus

class FirestoreMonitoringStore:
    def __init__(self, client: Any | None = None) -> None:
        if client is None:
            if not settings.firebase_project_id:
                raise ConfigurationError("JURIS Firebase project ID is not configured.")
            from core.firestore_client import get_firestore_client
            client = get_firestore_client()
        self._client = client

    def _collection(self, user_id: str) -> Any:
        return self._client.collection("_juris_monitors").document(user_id).collection("items")

    @staticmethod
    def _to_record(data: dict[str, Any]) -> CaseMonitorRecord:
        return CaseMonitorRecord.model_validate(data)

    async def upsert(self, record: CaseMonitorRecord) -> None:
        payload = record.model_dump(mode="json")
        await asyncio.to_thread(self._collection(record.user_id).document(record.id).set, payload, merge=True)

    async def get(self, record_id: str, user_id: str | None = None) -> CaseMonitorRecord | None:
        if not user_id:
            raise ValueError("user_id is required for Firestore monitoring reads.")
        snap = await asyncio.to_thread(self._collection(user_id).document(record_id).get)
        if not snap.exists:
            return None
        return self._to_record(snap.to_dict() or {})

    async def list_for_user(self, user_id: str, limit: int = 50) -> list[CaseMonitorRecord]:
        docs = await asyncio.to_thread(lambda: list(self._collection(user_id).limit(limit).stream()))
        return [self._to_record(d.to_dict() or {}) for d in docs]

    async def get_for_user_and_case(self, user_id: str, case_id: str) -> CaseMonitorRecord | None:
        docs = await asyncio.to_thread(lambda: list(self._collection(user_id).where("case_id", "==", case_id).limit(1).stream()))
        return self._to_record(docs[0].to_dict() or {}) if docs else None

    async def list_due(self, before: datetime, batch_size: int) -> list[CaseMonitorRecord]:
        docs = await asyncio.to_thread(lambda: list(self._client.collection_group("items").where("status", "==", MonitoringStatus.ACTIVE.value).limit(batch_size).stream()))
        records: list[CaseMonitorRecord] = []
        for document in docs:
            # Collection-group queries can match unrelated collections also
            # named "items". Accept only this application's monitor path.
            collection = document.reference.parent
            owner_doc = collection.parent
            root_collection = owner_doc.parent if owner_doc is not None else None
            if root_collection is None or root_collection.id != "_juris_monitors":
                continue
            record = self._to_record(document.to_dict() or {})
            if record.user_id != owner_doc.id:
                continue
            if record.last_checked_at is None or record.last_checked_at < before:
                records.append(record)
        return records

    async def delete(self, record_id: str, user_id: str | None = None) -> bool:
        if not user_id:
            raise ValueError("user_id is required for Firestore monitoring deletes.")
        ref = self._collection(user_id).document(record_id)
        snap = await asyncio.to_thread(ref.get)
        if not snap.exists:
            return False
        await asyncio.to_thread(ref.delete)
        return True

    async def count_for_user(self, user_id: str) -> int:
        records = await self.list_for_user(user_id, limit=1000)
        return sum(1 for record in records if record.status == MonitoringStatus.ACTIVE)
