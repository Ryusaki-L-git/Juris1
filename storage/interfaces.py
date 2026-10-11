"""Durable storage contracts for JURIS state.

Business logic depends on these interfaces rather than on Firestore directly.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any

class MonitoringRepository(ABC):
    @abstractmethod
    async def create(self, user_id: str, record: dict[str, Any]) -> str: ...
    @abstractmethod
    async def list_for_user(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]: ...
    @abstractmethod
    async def delete(self, user_id: str, monitor_id: str) -> bool: ...
