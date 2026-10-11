"""
monitoring/models.py — JURIS case monitoring Pydantic models.

CaseMonitorRecord tracks the monitoring state for one user/case/CNR triple.
It is NOT a second case-management system — it references LED case IDs
and uses the existing JURIS permission/entitlement architecture.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class MonitoringStatus(str, Enum):
    ACTIVE = "active"     # actively checking
    DISABLED = "disabled" # user disabled
    ERROR = "error"       # too many consecutive failures
    PAUSED = "paused"     # temporarily paused


class CaseMonitorRecord(BaseModel):
    """
    Monitoring record for one user/case/CNR pair.

    Fields:
        id                          — UUID string, primary key
        user_id                     — LED / Firebase UID of the user who created this
        case_id                     — LED internal case ID (not invented by JURIS)
        cnr                         — Normalized eCourts CNR (from core.cnr.normalize_cnr)
        provider                    — eCourts provider name (e.g. 'ecourtsindia')
        status                      — Current monitoring status
        created_at                  — When monitoring was enabled
        last_checked_at             — When we last attempted a check (success or failure)
        last_successful_check_at    — When we last got a valid response from eCourts
        last_known_fingerprint      — SHA-256 hash of last stable case state
        last_detected_change_at     — When we last detected a case state change
        last_change_summary         — Human-readable description of last change
        consecutive_failures        — How many consecutive check failures
        last_error                  — Last error message (for diagnostics)
        entitlement_validated_at    — When we last confirmed the user's entitlement
    """

    model_config = ConfigDict(frozen=True)

    id: str
    user_id: str
    case_id: str
    cnr: str
    provider: str = "ecourtsindia"
    status: MonitoringStatus = MonitoringStatus.ACTIVE
    created_at: datetime
    last_checked_at: datetime | None = None
    last_successful_check_at: datetime | None = None
    last_known_fingerprint: str | None = None
    last_detected_change_at: datetime | None = None
    last_change_summary: str | None = None
    consecutive_failures: int = 0
    last_error: str | None = None
    entitlement_validated_at: datetime | None = None


class MonitorCreateRequest(BaseModel):
    """Request body for creating/enabling a case monitor."""
    case_id: str = Field(..., description="LED case ID")
    cnr: str = Field(..., description="eCourts CNR number (16 alphanumeric chars)")


class MonitorResponse(BaseModel):
    """Response returned to the API caller."""
    id: str
    user_id: str
    case_id: str
    cnr: str
    provider: str
    status: str
    created_at: datetime
    last_checked_at: datetime | None = None
    last_successful_check_at: datetime | None = None
    last_detected_change_at: datetime | None = None
    last_change_summary: str | None = None
    consecutive_failures: int
    last_error: str | None = None

    @classmethod
    def from_record(cls, record: CaseMonitorRecord) -> "MonitorResponse":
        return cls(
            id=record.id,
            user_id=record.user_id,
            case_id=record.case_id,
            cnr=record.cnr,
            provider=record.provider,
            status=record.status.value,
            created_at=record.created_at,
            last_checked_at=record.last_checked_at,
            last_successful_check_at=record.last_successful_check_at,
            last_detected_change_at=record.last_detected_change_at,
            last_change_summary=record.last_change_summary,
            consecutive_failures=record.consecutive_failures,
            last_error=record.last_error,
        )
