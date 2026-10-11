"""Select the durable monitoring repository."""
from __future__ import annotations
from functools import lru_cache
from core.config import settings
from monitoring.firestore_store import FirestoreMonitoringStore
from monitoring.store import MonitoringStore

@lru_cache(maxsize=1)
def get_monitoring_store():
    if settings.firebase_project_id:
        return FirestoreMonitoringStore()
    return MonitoringStore()
