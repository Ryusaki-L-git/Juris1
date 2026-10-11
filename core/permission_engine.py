"""
core/permission_engine.py — Concrete JURIS permission engine.

This is the enforcement boundary between the JURIS agent and LED data.

DESIGN PRINCIPLE
    The model does not decide what it is allowed to access.
    The model may *request* a tool call; JURIS decides whether to run it.

    Request flow:
        Model requests tool
            ↓
        Orchestrator calls LedPermissionEngine.check(...)
            ↓
        Engine evaluates identity + resource without trusting the client
            ↓
        ALLOWED → tool executes
        DENIED  → AccessDenied / AuthorizationFailure, tool never runs

FAIL-CLOSED RULE
    If authorization cannot be established, the answer is DENY.
    The engine NEVER returns "allowed" by default, and never treats a
    client-supplied ownership claim as evidence.

The engine deliberately consumes ``CaseAccess`` evidence produced by the
data layer (core/led_access.py) instead of re-deriving ownership, so there
is exactly one implementation of "who can see this case".
"""

from __future__ import annotations

import asyncio

from core.entitlements import EntitlementService, Feature
from core.identity import JurisUser
from core.led_access import CaseRelation, LedDataAccess
from core.permissions import (
    Action,
    PermissionContext,
    PermissionEngine,
    PermissionResult,
    ResourceType,
)

# Case relations that may READ a case.
_READ_RELATIONS = frozenset(
    {CaseRelation.OWNER, CaseRelation.ASSIGNED, CaseRelation.TEAM_OWNER, CaseRelation.TEAM}
)
# Only the owner may delete a case.
_DELETE_RELATIONS = frozenset({CaseRelation.OWNER})
# Drafting is available to owner/assigned/team, but still behind JURIS_ACCESS.
_DRAFT_RELATIONS = _READ_RELATIONS


class LedPermissionEngine(PermissionEngine):
    """
    Concrete permission engine backed by the LED data-access layer.

    Args:
        data_access:  The LED data-access implementation (reads only).
        entitlements: Optional EntitlementService.  When provided, features
                      that require a paid plan are checked against the
                      server-owned entitlement store.
    """

    def __init__(
        self,
        data_access: LedDataAccess,
        entitlements: EntitlementService | None = None,
    ) -> None:
        self._data_access = data_access
        self._entitlements = entitlements

    # ── public API ──────────────────────────────────────────────────────────

    async def check(self, context: PermissionContext) -> PermissionResult:
        user = context.user

        # Rule 0 — fail closed: an unverified identity can never be allowed.
        if not user.is_verified or not user.user_id.strip():
            return self._deny("The caller is not an authenticated LED user.")

        try:
            return await self._evaluate(context)
        except Exception:
            # Fail closed on any unexpected error. Never assume allowed.
            return self._deny("Access could not be verified; request denied.")

    # ── evaluation ──────────────────────────────────────────────────────────

    async def _evaluate(self, context: PermissionContext) -> PermissionResult:
        resource = context.resource_type

        if resource is ResourceType.CASE:
            return await self._check_case(context)
        if resource is ResourceType.CLIENT:
            return await self._check_client(context)
        if resource is ResourceType.HEARING:
            return await self._check_hearing(context)
        if resource is ResourceType.DOCUMENT:
            return await self._check_document(context)
        if resource is ResourceType.TEAM:
            return await self._check_team(context)
        if resource is ResourceType.LEGAL_RESEARCH:
            return await self._check_legal_research(context)
        if resource is ResourceType.ECOURTS:
            return await self._check_ecourts(context)

        # Unknown resource → deny (fail closed).
        return self._deny("Unknown resource type.")

    # ── resource-specific checks ────────────────────────────────────────────

    async def _check_case(self, context: PermissionContext) -> PermissionResult:
        # Class-level permissions are intentionally narrow. Actions that
        # target or mutate a resource must identify it and pass resource-level
        # authorization below; a missing ID is never an authorization bypass.
        if not context.resource_id:
            if context.action in {Action.READ, Action.SEARCH}:
                return self._allow(
                    "Verified user may read/search within their own Case scope."
                )
            if context.action is Action.CREATE and context.extra.get("tool_name") == "create_reminder":
                return self._allow("Verified user may prepare a reminder for confirmation.")
            return self._deny("A Case identifier is required for this action.")

        access = await self._data_access.get_case_access(
            context.resource_id, user_id=context.user.user_id
        )
        if access is None:
            return self._deny(
                "This Case is not owned by, assigned to, or shared with you."
            )

        if context.action is Action.DELETE:
            if access.relation not in _DELETE_RELATIONS:
                return self._deny("Only the Case owner may delete this Case.")
            return self._allow("Case owner may delete their Case.")

        if context.action is Action.DRAFT:
            if access.relation not in _DRAFT_RELATIONS:
                return self._deny("You do not have drafting access to this Case.")
            return await self._require_feature(
                context.user,
                Feature.JURIS_ACCESS,
                allow_reason="You may draft on this Case.",
            )

        if context.action is Action.CREATE and context.extra.get("tool_name") == "create_reminder":
            if access.relation in _READ_RELATIONS:
                return self._allow("You may prepare a reminder for this Case.")

        if context.action in {Action.READ, Action.SEARCH} and access.relation in _READ_RELATIONS:
            return self._allow("You may access this Case.")

        return self._deny("This action is not permitted for the requested Case.")

    async def _check_client(self, context: PermissionContext) -> PermissionResult:
        # Search is scoped by the data layer. A direct read must prove the
        # client exists, belongs to this user, and is not soft-deleted.
        if context.action is Action.SEARCH and not context.resource_id:
            return self._allow("You may search Clients within your own scope.")
        if context.action is not Action.READ or not context.resource_id:
            return self._deny("A Client identifier is required for this action.")
        client = await self._data_access.get_client(
            context.resource_id, owner_uid=context.user.user_id
        )
        if client is None or client.is_deleted:
            return self._deny("This Client is not accessible to you.")
        return self._allow("You may read this Client within your own scope.")

    async def _check_hearing(self, context: PermissionContext) -> PermissionResult:
        if context.action not in {Action.READ, Action.SEARCH}:
            return self._deny("This action is not permitted for hearings.")
        if not context.resource_id:
            return self._allow("You may list hearings within your own scope.")
        access = await self._data_access.get_case_access(
            context.resource_id, user_id=context.user.user_id
        )
        if access is None:
            return self._deny("Hearings for this Case are not accessible to you.")
        return self._allow("You may read hearings for this Case.")

    async def _check_document(self, context: PermissionContext) -> PermissionResult:
        case_id = str(context.extra.get("case_id") or "").strip()
        if not case_id:
            return self._deny("A Case must be specified to access a document.")
        access = await self._data_access.get_case_access(
            case_id, user_id=context.user.user_id
        )
        if access is None:
            return self._deny("This document's Case is not accessible to you.")
        if context.action in {Action.READ, Action.SEARCH}:
            return self._allow("You may read documents for this Case.")
        if context.action is Action.DRAFT and access.relation in _DRAFT_RELATIONS:
            return await self._require_feature(
                context.user,
                Feature.JURIS_ACCESS,
                allow_reason="You may draft a document for this Case.",
            )
        return self._deny("This action is not permitted for documents.")

    async def _check_team(self, context: PermissionContext) -> PermissionResult:
        team_id = context.resource_id
        if not team_id:
            return self._deny("A team must be specified.")
        membership = await self._data_access.get_team_membership(
            team_id, user_id=context.user.user_id
        )
        if membership is None:
            return self._deny("You are not an active member of this team.")
        if context.action not in {Action.READ, Action.SEARCH}:
            return self._deny("This action is not permitted for teams.")
        return self._allow("You are an active member of this team.")

    async def _check_legal_research(
        self, context: PermissionContext
    ) -> PermissionResult:
        if context.action not in {Action.READ, Action.SEARCH}:
            return self._deny("Legal research is read-only.")
        return await self._require_feature(
            context.user,
            Feature.JURIS_ACCESS,
            allow_reason="You may use legal research.",
        )

    async def _check_ecourts(self, context: PermissionContext) -> PermissionResult:
        # eCourts is read-only AND must resolve through an authorized LED case.
        if context.action is not Action.READ:
            return self._deny("eCourts is read-only; this operation is refused.")
        cnr = str(context.extra.get("cnr") or context.resource_id or "").strip()
        if not cnr:
            return self._deny("A CNR is required to access eCourts data.")
        access = await self._data_access.get_case_access_by_cnr(cnr, user_id=context.user.user_id)
        if access is None:
            return self._deny("This CNR does not belong to a Case you are authorized to access in LED.")
        return await self._require_feature(
            context.user,
            Feature.JURIS_ACCESS,
            allow_reason="You may read eCourts data stored on this authorized LED Case.",
        )

    # ── helpers ─────────────────────────────────────────────────────────────

    async def _require_feature(
        self, user: JurisUser, feature: Feature, *, allow_reason: str
    ) -> PermissionResult:
        if self._entitlements is None:
            # No entitlement source configured → fail closed for paid features.
            return self._deny(
                "This feature requires an eligible paid plan and the "
                "entitlement service is not configured."
            )
        if not await self._has_feature(user, feature):
            return self._deny("Your plan does not include this feature.")
        return self._allow(allow_reason)

    async def _has_feature(self, user: JurisUser, feature: Feature) -> bool:
        """
        Consult the server-owned entitlement store off the event loop.

        A Firestore read is blocking; running it in a worker thread keeps the
        JURIS event loop responsive while still failing closed.
        """
        assert self._entitlements is not None
        return await asyncio.to_thread(
            self._entitlements.has_entitlement_for_user, user.user_id, feature
        )

    @staticmethod
    def _allow(reason: str) -> PermissionResult:
        return PermissionResult(allowed=True, reason=reason)

    @staticmethod
    def _deny(reason: str) -> PermissionResult:
        return PermissionResult(allowed=False, reason=reason)
