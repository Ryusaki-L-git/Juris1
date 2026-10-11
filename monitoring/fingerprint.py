"""
monitoring/fingerprint.py — Deterministic case-state fingerprinting.

Computes a stable SHA-256 hash of meaningful case fields to detect
when the eCourts case state has actually changed between checks.

DESIGN RULES:
    - Hash ONLY stable, meaningful case-data fields.
    - EXCLUDE retrieval metadata (retrieved_at, provider).
    - EXCLUDE transient fields that change on every fetch even if
      the underlying case state hasn't changed.
    - Sort list fields before hashing for stability.
    - Use json.dumps with sort_keys=True for deterministic serialization.

A fingerprint change means the case state has genuinely changed.
A fingerprint match means the case state is identical to the last check.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ecourts.base import ECourtsSnapshot


def _stable_dict(snapshot: ECourtsSnapshot) -> dict[str, Any]:
    """
    Build the stable representation of a snapshot for fingerprinting.

    Only includes fields that represent actual case state.
    Excludes: retrieved_at, provider (metadata, not case state).
    Sorts list fields for stability regardless of return order.
    """
    return {
        "status": snapshot.status,
        "stage_of_case": snapshot.stage_of_case,
        "next_hearing_date": snapshot.next_hearing_date,
        "last_hearing_date": snapshot.last_hearing_date,
        "court_name": snapshot.court_name,
        "court_number": snapshot.court_number,
        "petitioner_names": sorted(snapshot.petitioner_names),
        "respondent_names": sorted(snapshot.respondent_names),
        "petitioner_advocate": snapshot.petitioner_advocate,
        "respondent_advocate": snapshot.respondent_advocate,
        "orders_count": len(snapshot.orders),
        # Sort order dates for stability (order of orders may vary)
        "orders_dates": sorted(
            o.get("date") or "" for o in snapshot.orders
        ),
        "acts": sorted(snapshot.acts),
        "case_type": snapshot.case_type,
    }


def compute_fingerprint(snapshot: ECourtsSnapshot) -> str:
    """
    Compute a deterministic SHA-256 fingerprint of the case state.

    Returns:
        64-character hex string (SHA-256 digest).

    The same logical case state always produces the same fingerprint,
    regardless of when it was retrieved or which provider returned it.
    """
    stable = _stable_dict(snapshot)
    payload = json.dumps(stable, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def has_changed(old_fingerprint: str | None, new_fingerprint: str) -> bool:
    """
    Return True if the case state has changed since the last check.

    If old_fingerprint is None (first check), returns False — we have
    no baseline to compare against, so we can't declare a change.
    """
    if old_fingerprint is None:
        return False
    return old_fingerprint != new_fingerprint


def summarize_change(old_snapshot_data: dict | None, new_snapshot: ECourtsSnapshot) -> str:
    """
    Produce a human-readable summary of what changed.

    Used in notification bodies.
    """
    parts: list[str] = []

    if new_snapshot.status:
        parts.append(f"Status: {new_snapshot.status}")
    if new_snapshot.stage_of_case:
        parts.append(f"Stage: {new_snapshot.stage_of_case}")
    if new_snapshot.next_hearing_date:
        parts.append(f"Next hearing: {new_snapshot.next_hearing_date}")

    if parts:
        return "; ".join(parts)
    return "Case details have been updated on eCourts."
