"""
monitoring/scheduler.py — JURIS automatic case monitoring scheduler.

Runs a periodic asyncio background task that checks due monitoring records
by reading the latest LED Firestore case data.

ARCHITECTURE:
    JURIS does NOT call eCourts directly.
    JURIS does NOT hold an eCourts API key.

    Monitoring detects changes in the LED-stored case data (Firestore).
    If LED has synced new eCourts data into Firestore, JURIS will detect
    the fingerprint change and dispatch a notification to the user.

    To refresh from live eCourts, the user uses LED's "Refresh from eCourts"
    feature. JURIS then detects the change on the next monitoring interval.

DESIGN:
    - Uses asyncio.sleep — no external scheduler dependency
    - Only active when settings.monitoring_enabled = True
    - Wakes every settings.monitor_interval_minutes (default: 180 min)
    - Does NOT run a tight polling loop
    - Skips if no records are due
    - Respects monitor_max_consecutive_failures before setting ERROR status
    - Handles errors gracefully without crashing
    - Records last_checked_at even on failure
    - Only sets last_successful_check_at on genuine success

LIFECYCLE:
    start_scheduler() — called by FastAPI startup event
    stop_scheduler()  — called by FastAPI shutdown event
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from core.config import settings
from core.entitlements import EntitlementService, Feature
from core.finance_store import get_finance_repository
from core.led_access import get_led_data_access
from core.cnr import normalize_cnr
from core.errors import (
    ProviderMalformedResponseError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from ecourts.factory import get_ecourts_provider
from ecourts.service import ECourtsService
from monitoring.fingerprint import compute_fingerprint, has_changed, summarize_change
from monitoring.models import CaseMonitorRecord, MonitoringStatus
from monitoring.notifications import dispatch_change_notification
from monitoring.store_factory import get_monitoring_store

logger = logging.getLogger("juris.monitoring.scheduler")

_scheduler_task: asyncio.Task | None = None
_running: bool = False


async def _validate_monitor_record(record: CaseMonitorRecord) -> str | None:
    """Revalidate current case access, CNR binding, and paid entitlement.

    Returning a reason pauses monitoring. Any infrastructure or validation
    error fails closed; the scheduler never fetches case data on uncertainty.
    """
    try:
        data_access = get_led_data_access()
        case_access = await data_access.get_case_access(
            record.case_id, user_id=record.user_id
        )
        if case_access is None:
            return "Monitoring paused because LED case access is no longer valid."
        try:
            stored_cnr = normalize_cnr(case_access.case.cnr or "")
            record_cnr = normalize_cnr(record.cnr)
        except Exception:
            return "Monitoring paused because the case CNR could not be verified."
        if stored_cnr != record_cnr:
            return "Monitoring paused because the stored case CNR no longer matches."

        repository = get_finance_repository()
        service = EntitlementService(repository)
        entitled = await asyncio.to_thread(
            service.has_entitlement_for_user, record.user_id, Feature.JURIS_ACCESS
        )
        if not entitled:
            return "Monitoring paused because an active paid entitlement could not be verified."
        return None
    except Exception:
        logger.warning(
            "Could not validate monitor access for monitor %s; failing closed.",
            record.id,
            exc_info=True,
        )
        return "Monitoring paused because access or entitlement verification failed."


async def _check_record(record: CaseMonitorRecord, service: ECourtsService) -> None:
    """
    Perform one monitoring check for a single record.

    Reads the LED-stored case data from Firestore (via ECourtsService, which
    uses LedDataECourtsProvider — no eCourts API call made here).

    On success: update fingerprint, dispatch notification if changed.
    On failure: increment consecutive_failures, set ERROR if threshold exceeded.
    Always updates last_checked_at.
    """
    store = get_monitoring_store()
    now = datetime.now(timezone.utc)

    try:
        validation_error = await _validate_monitor_record(record)
        if validation_error:
            paused = record.model_copy(update={
                "status": MonitoringStatus.PAUSED,
                "last_checked_at": now,
                "last_error": validation_error,
            })
            await store.upsert(paused)
            logger.warning("Monitor %s paused: %s", record.id, validation_error)
            return

        # Reads LED Firestore data — NOT a live eCourts call. Access and
        # entitlement have just been revalidated against server-owned records.
        snapshot = await service.get_case(record.cnr)
        try:
            snapshot_cnr = normalize_cnr(snapshot.cnr)
        except Exception:
            snapshot_cnr = ""
        if not snapshot_cnr or snapshot_cnr != normalize_cnr(record.cnr):
            paused = record.model_copy(update={
                "status": MonitoringStatus.PAUSED,
                "last_checked_at": now,
                "last_error": "Monitoring paused because provider data returned an invalid or different CNR.",
            })
            await store.upsert(paused)
            return
        new_fingerprint = compute_fingerprint(snapshot)
        changed = has_changed(record.last_known_fingerprint, new_fingerprint)

        change_summary: str | None = None
        if changed:
            change_summary = summarize_change(None, snapshot)
            logger.info(
                "Change detected for CNR %s (user %s): %s",
                record.cnr,
                record.user_id,
                change_summary,
            )

        updated = record.model_copy(update={
            "last_checked_at": now,
            "last_successful_check_at": now,
            "last_known_fingerprint": new_fingerprint,
            "consecutive_failures": 0,
            "last_error": None,
            "last_detected_change_at": now if changed else record.last_detected_change_at,
            "last_change_summary": change_summary if changed else record.last_change_summary,
        })
        await store.upsert(updated)

        if changed and change_summary:
            await dispatch_change_notification(record, snapshot, change_summary)

    except (
        ProviderUnavailableError,
        ProviderTimeoutError,
        ProviderRateLimitError,
        ProviderMalformedResponseError,
    ) as exc:
        failures = record.consecutive_failures + 1
        new_status = (
            MonitoringStatus.ERROR
            if failures >= settings.monitor_max_consecutive_failures
            else record.status
        )
        updated = record.model_copy(update={
            "last_checked_at": now,
            "consecutive_failures": failures,
            "last_error": str(exc)[:500],
            "status": new_status,
        })
        await store.upsert(updated)

        if new_status == MonitoringStatus.ERROR:
            logger.error(
                "Monitor for CNR %s (user %s) set to ERROR after %d consecutive failures.",
                record.cnr,
                record.user_id,
                failures,
            )
        else:
            logger.warning(
                "Monitor check failed for CNR %s (failure %d/%d): %s",
                record.cnr,
                failures,
                settings.monitor_max_consecutive_failures,
                exc,
            )


async def _run_scheduler_loop() -> None:
    """
    Main scheduler loop. Runs until stop_scheduler() is called.

    Sleeps for monitor_interval_minutes, then checks all due records.
    Uses the LED-data-only ECourtsService (no eCourts API call).
    """
    global _running
    _running = True
    interval = settings.monitor_interval_minutes * 60
    logger.info(
        "JURIS monitoring scheduler started (interval: %d min, batch: %d). "
        "Monitoring LED Firestore case data — JURIS does not call eCourts directly.",
        settings.monitor_interval_minutes,
        settings.monitor_batch_size,
    )

    while _running:
        try:
            await asyncio.sleep(interval)
            if not _running:
                break

            store = get_monitoring_store()
            # get_ecourts_provider() returns LedDataECourtsProvider (reads Firestore)
            provider = get_ecourts_provider()
            service = ECourtsService(provider)
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=settings.monitor_interval_minutes)
            due = await store.list_due(before=cutoff, batch_size=settings.monitor_batch_size)

            if not due:
                logger.debug("Monitoring tick: no records due for check.")
                continue

            logger.info("Monitoring tick: checking %d record(s).", len(due))

            for record in due:
                if not _running:
                    break
                try:
                    await _check_record(record, service)
                except Exception as exc:
                    logger.exception(
                        "Unhandled error checking CNR %s: %s", record.cnr, exc
                    )

        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.exception("Scheduler loop error: %s", exc)

    _running = False
    logger.info("JURIS monitoring scheduler stopped.")


def start_scheduler() -> None:
    """
    Start the background monitoring scheduler.

    Called by FastAPI startup event. Does nothing if monitoring is disabled
    or if the scheduler is already running.

    Monitoring reads LED Firestore data — no eCourts API key required.
    """
    global _scheduler_task

    if not settings.monitoring_enabled:
        logger.info(
            "Case monitoring disabled (JURIS_MONITORING_ENABLED=false). "
            "Set it to true to enable automatic LED case change detection."
        )
        return

    if _scheduler_task is not None and not _scheduler_task.done():
        logger.debug("Monitoring scheduler already running.")
        return

    _scheduler_task = asyncio.create_task(_run_scheduler_loop())
    logger.info("Monitoring scheduler task created.")


async def stop_scheduler() -> None:
    """
    Stop the monitoring scheduler gracefully.

    Called by FastAPI shutdown event.
    """
    global _scheduler_task, _running

    _running = False

    if _scheduler_task is not None and not _scheduler_task.done():
        _scheduler_task.cancel()
        try:
            await _scheduler_task
        except asyncio.CancelledError:
            pass
        logger.info("Monitoring scheduler cancelled.")

    _scheduler_task = None
