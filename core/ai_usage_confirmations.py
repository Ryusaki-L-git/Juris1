"""Single-use, user-bound approval for a JURIS AI Credit budget."""
from __future__ import annotations

import asyncio
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any

from core.config import settings
from core.errors import ConfigurationError

_QUOTE_TTL_SECONDS = 300


class AIUsageConfirmationStore:
    def __init__(self, client: Any | None = None) -> None:
        self._client = client
        self._records: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _message_hash(message: str) -> str:
        return hashlib.sha256(message.encode("utf-8")).hexdigest()

    @classmethod
    def _record(
        cls, user_id: str, session_id: str, message: str, model: str,
        budget_minor_units: int,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        return {
            "user_id": user_id,
            "session_id": session_id,
            "message_hash": cls._message_hash(message),
            "model": model,
            "budget_minor_units": budget_minor_units,
            "expires_at": now + timedelta(seconds=_QUOTE_TTL_SECONDS),
            "created_at": now,
        }

    async def issue(
        self, user_id: str, session_id: str, message: str, model: str,
        budget_minor_units: int,
    ) -> str:
        if not user_id or not session_id or not model or budget_minor_units < 1:
            raise ValueError("A valid user, session, model, and positive budget are required.")
        token = secrets.token_urlsafe(32)
        record = self._record(user_id, session_id, message, model, budget_minor_units)
        if self._client is None:
            async with self._lock:
                self._purge_expired()
                self._records[token] = record
        else:
            await asyncio.to_thread(
                self._client.collection("_juris_pending_ai_usage").document(token).create,
                record,
            )
        return token

    async def consume(
        self, token: str, user_id: str, session_id: str, message: str, model: str,
    ) -> int | None:
        if not token:
            return None
        expected = {
            "user_id": user_id,
            "session_id": session_id,
            "message_hash": self._message_hash(message),
            "model": model,
        }
        if self._client is None:
            async with self._lock:
                self._purge_expired()
                record = self._records.get(token)
                if not record or not self._matches(record, expected):
                    return None
                self._records.pop(token, None)
                return int(record["budget_minor_units"])
        return await asyncio.to_thread(self._consume_firestore, token, expected)

    def _consume_firestore(self, token: str, expected: dict[str, Any]) -> int | None:
        from google.cloud import firestore
        ref = self._client.collection("_juris_pending_ai_usage").document(token)
        transaction = self._client.transaction(max_attempts=5)

        @firestore.transactional
        def consume_txn(txn: Any) -> int | None:
            snapshot = ref.get(transaction=txn)
            if not snapshot.exists:
                return None
            record = snapshot.to_dict() or {}
            if not self._matches(record, expected):
                return None
            txn.delete(ref)
            return int(record["budget_minor_units"])

        return consume_txn(transaction)

    @staticmethod
    def _matches(record: dict[str, Any], expected: dict[str, Any]) -> bool:
        try:
            raw_expiry = record.get("expires_at")
            expiry = raw_expiry if isinstance(raw_expiry, datetime) else datetime.fromisoformat(
                str(raw_expiry or "").replace("Z", "+00:00")
            )
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return False
        return datetime.now(timezone.utc) < expiry and all(
            record.get(key) == value for key, value in expected.items()
        )

    def _purge_expired(self) -> None:
        now = datetime.now(timezone.utc)
        for token, record in list(self._records.items()):
            try:
                expiry = record["expires_at"]
                if not isinstance(expiry, datetime):
                    expiry = datetime.fromisoformat(str(expiry).replace("Z", "+00:00"))
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
                if now >= expiry:
                    self._records.pop(token, None)
            except (KeyError, TypeError, ValueError):
                self._records.pop(token, None)


@lru_cache(maxsize=1)
def get_ai_usage_confirmation_store() -> AIUsageConfirmationStore:
    if settings.firebase_project_id:
        from core.firestore_client import get_firestore_client
        return AIUsageConfirmationStore(get_firestore_client())
    if not settings.is_development:
        raise ConfigurationError(
            "Persistent AI usage confirmations require JURIS_FIREBASE_PROJECT_ID."
        )
    return AIUsageConfirmationStore()
