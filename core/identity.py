"""
core/identity.py — JURIS request identity.

Defines who is making a request to JURIS.

This is the *architecture contract*, not a production auth implementation.
Authentication (JWT verification, Firebase token validation) is production auth.

Key design rule:
    Identity is resolved BEFORE any permission check or tool execution.
    The identity object is immutable once created.
    Nothing downstream can elevate its own privileges.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SubscriptionTier(str, Enum):
    """
    The LED subscription level determines which JURIS capabilities are
    available to the user.  The tiers are defined here but the entitlement
    logic lives in the permission engine (core/permissions.py).
    """

    FREE = "free"
    SOLO = "solo"
    TEAM = "team"
    FIRM = "firm"


class JurisUser(BaseModel):
    """
    Represents an authenticated LED user from JURIS's perspective.

    Populated by the auth dependency (api/deps.py — production auth).
    For development baseline this is the canonical data shape; its source is TBD.

    Fields:
        user_id         — LED / Firebase UID.  Primary key.
        email           — User email (for logging/audit, not routing logic).
        display_name    — Human-readable name for logging.
        team_id         — The team/firm the user belongs to, if any.
        subscription    — Controls which JURIS features are accessible.
        is_verified     — Whether the auth token has been verified.
                          Always False until production auth auth is wired.
        metadata        — Extension point for provider-specific claims.
    """

    user_id: str
    email: str = ""
    display_name: str = ""
    team_id: str | None = None
    subscription: SubscriptionTier = SubscriptionTier.FREE
    is_verified: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(frozen=True)  # identity is immutable once created


class RequestIdentity(BaseModel):
    """
    The full identity context for a single JURIS API request.

    Combines the authenticated user with the session in which the
    request arrives.  The orchestrator and tools receive this, not a
    raw JWT or token string.

    Fields:
        user            — Verified user record.
        session_id      — Identifies the conversation turn sequence in LED.
                          Used for context threading, not authentication.
        request_id      — Unique ID for this specific request (for tracing).
        source          — Where the request originated (e.g. "led_flutter").
    """

    user: JurisUser
    session_id: str
    request_id: str = ""
    source: str = "led_flutter"

    model_config = ConfigDict(frozen=True)
