"""
api/monitoring.py — JURIS case monitoring API router.

Endpoints:
    POST   /monitor         — Create/enable a case monitor
    GET    /monitor         — List the authenticated user's monitors
    DELETE /monitor/{id}    — Disable/delete a specific monitor

Security:
    - All endpoints require authenticated identity (get_request_identity)
    - In production (firebase mode), requires verified Firebase token
    - Monitoring is a paid feature — server-side entitlement check enforced
    - Entitlement check uses Feature.JURIS_ACCESS (PREMIUM_CLOUD or above)
    - Client cannot bypass: the entitlement check runs server-side always
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from api.deps import get_request_identity
from core.led_access import get_led_data_access
from core.permission_engine import LedPermissionEngine
from core.permissions import Action, PermissionContext, ResourceType
from core.cnr import normalize_cnr
from core.entitlements import EntitlementService, Feature
from core.errors import InvalidCnrError
from core.finance_store import get_finance_repository
from core.identity import RequestIdentity
from monitoring.models import (
    CaseMonitorRecord,
    MonitorCreateRequest,
    MonitorResponse,
    MonitoringStatus,
)
from monitoring.store_factory import get_monitoring_store

router = APIRouter(tags=["monitoring"])

# ── Maximum monitors per user (prevent abuse) ───────────────────────────────────
_MAX_MONITORS_PER_USER = 50


async def _check_monitoring_entitlement(identity: RequestIdentity) -> None:
    """Require a verified identity and a current server-owned paid entitlement.

    Development mode does not bypass paid-feature checks. Missing Firebase or
    entitlement infrastructure fails closed rather than granting access.
    """
    try:
        service = EntitlementService(get_finance_repository())
        allowed = await asyncio.to_thread(
            service.has_entitlement, identity, Feature.JURIS_ACCESS
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Monitoring entitlement could not be verified.",
        ) from exc
    if not allowed:
        raise HTTPException(
            status_code=403,
            detail="Case monitoring requires an active eligible paid plan.",
        )


# ── POST /monitor ────────────────────────────────────────────────────────────────

@router.post("/monitor", response_model=MonitorResponse, status_code=201)
async def create_monitor(
    body: MonitorCreateRequest,
    identity: RequestIdentity = Depends(get_request_identity),
) -> MonitorResponse:
    """
    Enable automatic eCourts monitoring for a case.

    The CNR is validated before the record is created.
    Idempotent: if a monitor already exists for this user+case, returns it.
    """
    await _check_monitoring_entitlement(identity)

    # Validate CNR format
    try:
        cnr = normalize_cnr(body.cnr)
    except InvalidCnrError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Bind the monitor to the authorized LED case and its canonical CNR.
    # A caller cannot pair an accessible case ID with another case's CNR.
    try:
        data_access = get_led_data_access()
        case_access = await data_access.get_case_access(
            body.case_id, user_id=identity.user.user_id
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="LED case access could not be verified.",
        ) from exc
    if case_access is None:
        raise HTTPException(status_code=403, detail="This Case is not accessible to you.")
    try:
        stored_cnr = normalize_cnr(case_access.case.cnr or "")
    except InvalidCnrError:
        raise HTTPException(
            status_code=409,
            detail="This LED Case has no valid stored CNR and cannot be monitored.",
        ) from None
    if stored_cnr != cnr:
        raise HTTPException(
            status_code=422,
            detail="The supplied CNR does not match this LED Case.",
        )

    permission = await LedPermissionEngine(data_access).check(
        PermissionContext(
            user=identity.user,
            resource_type=ResourceType.CASE,
            resource_id=body.case_id,
            action=Action.READ,
        )
    )
    if not permission.allowed:
        raise HTTPException(status_code=403, detail="This Case is not accessible to you.")

    store = get_monitoring_store()
    user_id = identity.user.user_id

    # Idempotency: return existing monitor if one exists for this case
    existing = await store.get_for_user_and_case(user_id, body.case_id)
    if existing is not None:
        # Re-enable if it was disabled/errored
        if existing.status != MonitoringStatus.ACTIVE:
            updated = existing.model_copy(update={
                "status": MonitoringStatus.ACTIVE,
                "consecutive_failures": 0,
                "last_error": None,
            })
            await store.upsert(updated)
            return MonitorResponse.from_record(updated)
        return MonitorResponse.from_record(existing)

    # Enforce per-user limit
    count = await store.count_for_user(user_id)
    if count >= _MAX_MONITORS_PER_USER:
        raise HTTPException(
            status_code=429,
            detail=f"Maximum of {_MAX_MONITORS_PER_USER} active monitors reached.",
        )

    now = datetime.now(timezone.utc)
    record = CaseMonitorRecord(
        id=str(uuid.uuid4()),
        user_id=user_id,
        case_id=body.case_id,
        cnr=cnr,
        provider="led_firestore",
        status=MonitoringStatus.ACTIVE,
        created_at=now,
        entitlement_validated_at=now,
    )

    await store.upsert(record)
    return MonitorResponse.from_record(record)


# ── GET /monitor ─────────────────────────────────────────────────────────────────

@router.get("/monitor", response_model=list[MonitorResponse])
async def list_monitors(
    identity: RequestIdentity = Depends(get_request_identity),
) -> list[MonitorResponse]:
    """
    List all case monitors for the authenticated user.
    """
    await _check_monitoring_entitlement(identity)

    store = get_monitoring_store()
    records = await store.list_for_user(identity.user.user_id)
    return [MonitorResponse.from_record(r) for r in records]


# ── DELETE /monitor/{id} ─────────────────────────────────────────────────────────

@router.delete("/monitor/{monitor_id}", status_code=204)
async def delete_monitor(
    monitor_id: str,
    identity: RequestIdentity = Depends(get_request_identity),
) -> None:
    """
    Delete/disable a case monitor.

    Only the owning user can delete their own monitor.
    """
    await _check_monitoring_entitlement(identity)

    store = get_monitoring_store()
    record = await store.get(monitor_id, identity.user.user_id)

    if record is None:
        raise HTTPException(status_code=404, detail="Monitor not found.")

    # Ownership check — users cannot delete other users' monitors
    if record.user_id != identity.user.user_id:
        raise HTTPException(status_code=403, detail="Access denied.")

    await store.delete(monitor_id, identity.user.user_id)
