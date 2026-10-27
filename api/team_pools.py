"""Authenticated LED billing preferences and team pool management.

This API exposes balances and allocation settings only. It never mints or
changes wallet balances; grants/top-ups must use the server-owned payment
ledger. Team allocations are member limits over a shared pool, not extra funds.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from api.deps import get_request_identity
from core.firestore_client import get_firestore_client
from core.identity import RequestIdentity

router = APIRouter(tags=["team-pools"])
_PRIORITIES = {"personal_first", "team_first", "ask"}


class BillingPreferenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    usageDeductionPriority: str = "personal_first"
    preferredTeamId: str | None = Field(default=None, max_length=128)


class TeamMemberAllocationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    caseTokenLimitMinorUnits: int = Field(ge=0, le=10**15)
    jurisCreditLimitMinorUnits: int = Field(ge=0, le=10**15)
    storageLimitBytes: int = Field(ge=0, le=10**18)


def _db():
    try:
        return get_firestore_client()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Billing storage is temporarily unavailable.") from exc


def _require_verified(identity: RequestIdentity) -> str:
    if not identity.user.is_verified or not identity.user.user_id.strip():
        raise HTTPException(status_code=401, detail="A verified LED session is required.")
    return identity.user.user_id


def _data(snapshot: Any) -> dict[str, Any]:
    if snapshot is None or not getattr(snapshot, "exists", False):
        return {}
    value = snapshot.to_dict()
    return value if isinstance(value, dict) else {}


def _team_member(db: Any, team_id: str, uid: str) -> tuple[dict[str, Any], dict[str, Any]]:
    if not team_id or "/" in team_id or team_id in {".", ".."}:
        raise HTTPException(status_code=400, detail="Invalid team identifier.")
    team_snap = db.collection("teams").document(team_id).get()
    member_snap = db.collection("teams").document(team_id).collection("members").document(uid).get()
    team, member = _data(team_snap), _data(member_snap)
    if not team or team.get("isActive") is False:
        raise HTTPException(status_code=404, detail="Active team not found.")
    if (
        not member
        or member.get("isActive") is not True
        or member.get("identityType") != "firebase_uid"
        or member.get("userId") != uid
    ):
        raise HTTPException(status_code=403, detail="An active LED team membership is required.")
    return team, member


def _minor_units(snapshot: Any) -> int:
    data = _data(snapshot)
    value = data.get("balanceMinorUnits")
    if value is not None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise HTTPException(status_code=503, detail="Stored wallet balance is invalid.")
        return value
    legacy = data.get("balance", 0)
    if isinstance(legacy, bool) or not isinstance(legacy, int) or legacy < 0:
        raise HTTPException(status_code=503, detail="Stored legacy wallet balance is invalid.")
    return legacy * 100


def _balance(db: Any, collection: str, owner_id: str, wallet_type: str) -> int:
    return _minor_units(
        db.collection(collection).document(owner_id).collection("balances").document(wallet_type).get()
    )


@router.get("/billing/preferences")
async def get_billing_preferences(
    identity: RequestIdentity = Depends(get_request_identity),
) -> dict[str, Any]:
    uid = _require_verified(identity)
    db = _db()
    profile = _data(db.collection("users").document(uid).get())
    prefs = profile.get("billingPreferences")
    prefs = prefs if isinstance(prefs, dict) else {}
    priority = prefs.get("usageDeductionPriority", "personal_first")
    if priority not in _PRIORITIES:
        priority = "personal_first"
    team_id = prefs.get("preferredTeamId")
    if not isinstance(team_id, str) or not team_id.strip():
        team_id = None
    elif team_id:
        try:
            _team_member(db, team_id, uid)
        except HTTPException:
            team_id = None
    return {"usageDeductionPriority": priority, "preferredTeamId": team_id}


@router.put("/billing/preferences")
async def set_billing_preferences(
    body: BillingPreferenceRequest,
    identity: RequestIdentity = Depends(get_request_identity),
) -> dict[str, Any]:
    uid = _require_verified(identity)
    if body.usageDeductionPriority not in _PRIORITIES:
        raise HTTPException(status_code=422, detail="Unsupported deduction priority.")
    team_id = body.preferredTeamId.strip() if body.preferredTeamId else None
    db = _db()
    if team_id:
        _team_member(db, team_id, uid)
    db.collection("users").document(uid).set({
        "billingPreferences": {
            "usageDeductionPriority": body.usageDeductionPriority,
            "preferredTeamId": team_id,
            "updatedAt": datetime.now(timezone.utc),
        }
    }, merge=True)
    return {"usageDeductionPriority": body.usageDeductionPriority, "preferredTeamId": team_id}


@router.get("/teams/{team_id}/pool")
async def get_team_pool(
    team_id: str,
    identity: RequestIdentity = Depends(get_request_identity),
) -> dict[str, Any]:
    uid = _require_verified(identity)
    db = _db()
    team, member = _team_member(db, team_id, uid)
    limits = member.get("billingLimits")
    limits = limits if isinstance(limits, dict) else {}
    usage = member.get("billingUsage")
    usage = usage if isinstance(usage, dict) else {}
    period = datetime.now(timezone.utc).strftime("%Y-%m")
    current_usage = usage if usage.get("period") == period else {}
    case_limit = limits.get("caseTokenLimitMinorUnits", 0)
    juris_limit = limits.get("jurisCreditLimitMinorUnits", 0)
    storage_limit = limits.get("storageLimitBytes", 0)
    for value in (case_limit, juris_limit, storage_limit):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise HTTPException(status_code=503, detail="Stored member allocation is invalid.")
    case_used = current_usage.get("caseTokenUsedMinorUnits", 0)
    juris_used = current_usage.get("jurisCreditUsedMinorUnits", 0)
    if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in (case_used, juris_used)):
        raise HTTPException(status_code=503, detail="Stored member usage is invalid.")
    pool = team.get("billingPool")
    pool = pool if isinstance(pool, dict) else {}
    return {
        "teamId": team_id,
        "teamName": str(team.get("name", "Team")),
        "role": member.get("role", "member"),
        "isManager": team.get("ownerId") == uid or member.get("role") in {"owner", "leader"},
        "shared": {
            "caseTokensMinorUnits": _balance(db, "_server_team_token_accounts", team_id, "case"),
            "jurisCreditsMinorUnits": _balance(db, "_server_team_token_accounts", team_id, "juris"),
            "storageLimitBytes": pool.get("storageLimitBytes", 0),
            "storageUsedBytes": pool.get("storageUsedBytes", 0),
        },
        "personal": {
            "caseTokensMinorUnits": _balance(db, "_server_token_accounts", uid, "case"),
            "jurisCreditsMinorUnits": _balance(db, "_server_token_accounts", uid, "juris"),
        },
        "allocation": {
            "caseTokenLimitMinorUnits": case_limit,
            "caseTokenUsedMinorUnits": case_used,
            "caseTokenAvailableMinorUnits": max(0, case_limit - case_used),
            "jurisCreditLimitMinorUnits": juris_limit,
            "jurisCreditUsedMinorUnits": juris_used,
            "jurisCreditAvailableMinorUnits": max(0, juris_limit - juris_used),
            "storageLimitBytes": storage_limit,
        },
        "period": period,
    }


@router.put("/teams/{team_id}/pool/members/{member_uid}")
async def set_member_allocation(
    team_id: str,
    member_uid: str,
    body: TeamMemberAllocationRequest,
    identity: RequestIdentity = Depends(get_request_identity),
) -> dict[str, Any]:
    uid = _require_verified(identity)
    db = _db()
    team, actor = _team_member(db, team_id, uid)
    if team.get("ownerId") != uid and actor.get("role") not in {"owner", "leader"}:
        raise HTTPException(status_code=403, detail="Only the team owner or leader can manage allocations.")
    _team_member(db, team_id, member_uid)
    limits = {
        "caseTokenLimitMinorUnits": body.caseTokenLimitMinorUnits,
        "jurisCreditLimitMinorUnits": body.jurisCreditLimitMinorUnits,
        "storageLimitBytes": body.storageLimitBytes,
        "updatedBy": uid,
        "updatedAt": datetime.now(timezone.utc),
    }
    db.collection("teams").document(team_id).collection("members").document(member_uid).set(
        {"billingLimits": limits}, merge=True
    )
    return {"teamId": team_id, "memberUid": member_uid, "billingLimits": limits}
