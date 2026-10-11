"""
core/permissions.py — JURIS Permission Engine.

The permission layer is the enforcement boundary between the JURIS agent
and LED data.  This is one of the most critical architectural components.

DESIGN PRINCIPLE:
    The LLM does not decide what it is allowed to access.
    The model may *request* a tool call; JURIS decides whether to execute it.

    Request flow:
        Model requests tool
            ↓
        Orchestrator calls PermissionEngine.check(...)
            ↓
        PermissionEngine evaluates identity + resource
            ↓
        ALLOWED  → tool executes, result returned to model
        DENIED   → PermissionDenied raised, model receives denial notice

    The model never sees raw permission logic.
    The model never receives data it was not authorized to receive.

For development baseline, this module defines the contracts and raises
NotImplementedError stubs.  Real evaluation logic (against Firestore
rules / LED team records) is introduced in production integration.

DO NOT add fake "always allow" logic here.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any

from pydantic import BaseModel

from core.identity import JurisUser


# ── Resource types JURIS may interact with ────────────────────────────────────

class ResourceType(str, Enum):
    """
    The categories of LED resources JURIS tools may access.
    Extend this enum as new resource types are supported.
    """

    CASE = "case"
    CLIENT = "client"
    DOCUMENT = "document"
    HEARING = "hearing"
    TEAM = "team"
    LEGAL_RESEARCH = "legal_research"  # external, read-only
    ECOURTS = "ecourts"               # external, read-only — NEVER written


class Action(str, Enum):
    """
    The operations the permission engine can authorise or deny.
    """

    READ = "read"
    SEARCH = "search"
    DRAFT = "draft"       # AI-assisted document drafting
    CREATE = "create"     # LED resource creation
    UPDATE = "update"     # LED resource update
    DELETE = "delete"     # LED resource deletion (highly restricted)
    NOTIFY = "notify"     # Send notification to the user or team


# ── Permission check contracts ─────────────────────────────────────────────────

class PermissionContext(BaseModel):
    """
    Everything the permission engine needs to make a decision.

    Fields:
        user            — The authenticated user (from core/identity.py).
        resource_type   — What kind of LED resource is being accessed.
        resource_id     — The specific resource ID (case_id, doc_id, etc.).
                          None means the check applies to the resource class
                          (e.g. "can this user search cases at all?").
        action          — The operation being requested.
        extra           — Extension point for resource-specific context.
    """

    user: JurisUser
    resource_type: ResourceType
    resource_id: str | None = None
    action: Action = Action.READ
    extra: dict[str, Any] = {}


class PermissionResult(BaseModel):
    """
    The result of a permission check.

    Fields:
        allowed         — True if the action is permitted.
        reason          — Human-readable explanation (for logging/audit).
        restricted_to   — If not None, limits what subset of data may
                          be returned (e.g. only documents the user owns).
    """

    allowed: bool
    reason: str = ""
    restricted_to: dict[str, Any] | None = None


class PermissionDenied(Exception):
    """
    Raised by the orchestrator when the permission engine returns
    allowed=False.  The message is safe to surface to the model.
    """

    def __init__(self, reason: str = "Access denied.") -> None:
        super().__init__(reason)
        self.reason = reason


# ── Abstract engine ────────────────────────────────────────────────────────────

class PermissionEngine(ABC):
    """
    Abstract interface for the JURIS permission engine.

    Concrete implementations will evaluate against:
      - Firebase Auth claims (production auth)
      - LED Firestore team/case ownership records (external integration)
      - eCourts read-only policy (always enforced)

    development baseline: Only the interface is defined.
    """

    @abstractmethod
    async def check(self, context: PermissionContext) -> PermissionResult:
        """
        Evaluate whether the user may perform *action* on *resource*.

        Must return a PermissionResult — never raise directly.
        The caller (orchestrator) decides whether to raise PermissionDenied.
        """
        ...

    async def require(self, context: PermissionContext) -> PermissionResult:
        """
        Convenience wrapper: check and raise PermissionDenied if denied.
        """
        result = await self.check(context)
        if not result.allowed:
            raise PermissionDenied(result.reason or "Access denied.")
        return result
