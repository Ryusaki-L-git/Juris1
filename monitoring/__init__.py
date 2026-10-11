"""
monitoring/ — JURIS automatic case monitoring package.

Components:
    models.py         — CaseMonitorRecord and related Pydantic models
    fingerprint.py    — Deterministic case-state fingerprinting
    store.py          — In-memory persistence (Firestore-backed in prod)
    notifications.py  — Change event dispatch into LED notification system
    scheduler.py      — asyncio-based periodic check scheduler
"""
