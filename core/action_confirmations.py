"""Single-use, user-bound confirmations for JURIS write actions.

Production uses Firestore so confirmations survive API workers and are consumed
atomically. The in-memory implementation is for local development only.
"""
from __future__ import annotations

import asyncio
import secrets
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any

from core.config import settings
from core.errors import ConfigurationError

_CONFIRMATION_TTL_SECONDS = 300


class ActionConfirmationStore:
    def __init__(self, client: Any | None = None) -> None:
        self._client = client
        self._records: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _payload(user_id: str, session_id: str, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "user_id": user_id,
            "session_id": session_id,
            "tool_name": tool_name,
            "arguments": arguments,
            # Store native timestamps so Firestore TTL can purge expired tokens.
            "expires_at": datetime.now(timezone.utc) + timedelta(seconds=_CONFIRMATION_TTL_SECONDS),
            "created_at": datetime.now(timezone.utc),
        }

    async def issue(self, user_id: str, session_id: str, tool_name: str, arguments: dict[str, Any]) -> str:
        token = secrets.token_urlsafe(32)
        record = self._payload(user_id, session_id, tool_name, arguments)
        if self._client is None:
            async with self._lock:
                self._purge_expired()
                self._records[token] = record
        else:
            await asyncio.to_thread(self._client.collection("_juris_pending_actions").document(token).create, record)
        return token

    async def consume(self, token: str, user_id: str, session_id: str, tool_name: str, arguments: dict[str, Any]) -> bool:
        if not token:
            return False
        expected = {"user_id": user_id, "session_id": session_id, "tool_name": tool_name, "arguments": arguments}
        if self._client is None:
            async with self._lock:
                self._purge_expired()
                record = self._records.get(token)
                if not record or not self._matches(record, expected):
                    return False
                self._records.pop(token, None)
                return True
        return await asyncio.to_thread(self._consume_firestore, token, expected)

    def _consume_firestore(self, token: str, expected: dict[str, Any]) -> bool:
        from google.cloud import firestore
        ref = self._client.collection("_juris_pending_actions").document(token)
        transaction = self._client.transaction(max_attempts=5)

        @firestore.transactional
        def consume_txn(txn: Any) -> bool:
            snapshot = ref.get(transaction=txn)
            if not snapshot.exists:
                return False
            record = snapshot.to_dict() or {}
            if not self._matches(record, expected):
                return False
            txn.delete(ref)
            return True

        return bool(consume_txn(transaction))

    @staticmethod
    def _matches(record: dict[str, Any], expected: dict[str, Any]) -> bool:
        try:
            raw_expiry = record.get("expires_at")
            if isinstance(raw_expiry, datetime):
                expiry = raw_expiry
            else:
                expiry = datetime.fromisoformat(str(raw_expiry or "").replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return False
        if datetime.now(timezone.utc) >= expiry:
            return False
        return all(record.get(key) == value for key, value in expected.items())

    def _purge_expired(self) -> None:
        now = datetime.now(timezone.utc)
        expired = []
        for token, record in self._records.items():
            try:
                raw_expiry = record["expires_at"]
                if isinstance(raw_expiry, datetime):
                    expiry = raw_expiry
                else:
                    expiry = datetime.fromisoformat(str(raw_expiry))
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
                if now >= expiry:
                    expired.append(token)
            except (KeyError, TypeError, ValueError):
                expired.append(token)
        for token in expired:
            self._records.pop(token, None)


@lru_cache(maxsize=1)
def get_action_confirmation_store() -> ActionConfirmationStore:
    if settings.firebase_project_id:
        from core.firestore_client import get_firestore_client
        return ActionConfirmationStore(get_firestore_client())
    if not settings.is_development:
        raise ConfigurationError("Persistent action confirmations require JURIS_FIREBASE_PROJECT_ID.")
    return ActionConfirmationStore()
