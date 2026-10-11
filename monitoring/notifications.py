"""
monitoring/notifications.py — JURIS change notification dispatch.

Writes case-change notification events to Firestore under the
_juris_notifications/{user_id}/items/{id} path.

LED's existing notification architecture can poll or listen
(real-time snapshot) to this collection to surface alerts in the
Home screen notification bar — without JURIS inventing a separate
notification stream.

DEVELOPMENT MODE:
    If firebase_project_id is not set, the notification is logged
    locally instead of written to Firestore.

SECURITY:
    - Notifications are per-user (user_id scoped)
    - No sensitive case data is placed in the notification
    - Only metadata and summary is dispatched
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from ecourts.base import ECourtsSnapshot
from monitoring.models import CaseMonitorRecord

logger = logging.getLogger("juris.monitoring.notifications")


async def dispatch_change_notification(
    record: CaseMonitorRecord,
    snapshot: ECourtsSnapshot,
    change_summary: str,
) -> None:
    """
    Dispatch a case-change notification for the given record.

    If Firestore is configured, writes a notification document.
    Otherwise logs it (development mode).
    """
    from core.config import settings

    notification_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    payload: dict = {
        "id": notification_id,
        "user_id": record.user_id,
        "case_id": record.case_id,
        "cnr": record.cnr,
        "type": "case_status_changed",
        "title": "Case Update Detected",
        "body": change_summary,
        "court_name": snapshot.court_name,
        "next_hearing_date": snapshot.next_hearing_date,
        "case_status": snapshot.status,
        "stage_of_case": snapshot.stage_of_case,
        "created_at": now.isoformat(),
        "read": False,
        "provider": snapshot.provider,
    }

    if not settings.firebase_project_id:
        # Development mode — log instead of writing to Firestore
        logger.info(
            "[DEV] Case change notification (no Firestore configured): "
            "user=%s case=%s cnr=%s summary=%s",
            record.user_id,
            record.case_id,
            record.cnr,
            change_summary,
        )
        return

    try:
        from google.cloud import firestore  # type: ignore
        client = firestore.Client(project=settings.firebase_project_id)
        doc_ref = (
            client
            .collection("_juris_notifications")
            .document(record.user_id)
            .collection("items")
            .document(notification_id)
        )
        doc_ref.set(payload)
        logger.info(
            "Notification written: %s for user %s (case %s)",
            notification_id,
            record.user_id,
            record.case_id,
        )
    except Exception as exc:
        # Notification failure must NOT crash the monitoring loop
        logger.error(
            "Failed to write notification to Firestore: %s (user=%s case=%s)",
            exc,
            record.user_id,
            record.case_id,
        )
