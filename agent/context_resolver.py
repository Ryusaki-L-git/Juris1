"""
agent/context_resolver.py — Builds and enriches RequestContext for JURIS.

Responsibility:
    Take the raw ChatRequest (what LED sent), the resolved RequestIdentity
    (who is making the request), and the server-authorized LED access layer to
    produce a validated, enriched RequestContext that the orchestrator can work with.

RULES (Section 15):
    - If case context already exists: Reuse it after verifying access.
    - If user explicitly names a case: Resolve it using authorized LED data only.
    - If the case cannot be uniquely resolved: Note ambiguity for clarification.
    - If no case context exists: Do not invent one.
    - If multiple candidates exist: Ask the user to choose.
    - Never use an inaccessible case simply because its name matches.
"""

from __future__ import annotations

import re
from typing import Any

from core.authorized_access import AuthorizedLedAccess
from core.errors import AuthorizationFailure, NotFoundError
from core.identity import RequestIdentity
from models.chat import ChatRequest
from models.context import ItemRef, RequestContext


class ContextResolver:
    """
    Builds and enriches RequestContext from incoming ChatRequest + RequestIdentity + AuthorizedLedAccess.
    """

    async def resolve(
        self,
        request: ChatRequest,
        identity: RequestIdentity,
        access: AuthorizedLedAccess | None = None,
    ) -> RequestContext:
        """
        Produce a validated RequestContext for the orchestrator.
        """
        context = request.context.model_copy(deep=True)
        extra: dict[str, Any] = dict(context.extra)

        # 1. If an active_case is already present, verify it against authorized access
        if context.active_case and context.active_case.resource_id:
            if access is not None:
                try:
                    case = await access.get_case(context.active_case.resource_id)
                    # Re-verify display name if blank
                    if not context.active_case.display_name:
                        context.active_case = ItemRef(
                            resource_type="case",
                            resource_id=case.id,
                            display_name=case.case_title or case.case_number or case.id,
                        )
                except (AuthorizationFailure, NotFoundError):
                    # Inaccessible or deleted case must never be retained
                    context.active_case = None
                    extra["inaccessible_case_attempted"] = True

        # 2. If active_case is still None, attempt to resolve from user message
        if context.active_case is None and access is not None:
            message = request.message.strip()

            # Check if user says "this case" or "current case" without providing one
            if re.search(r"\b(this|current|the)\s+case\b", message, re.IGNORECASE):
                # Check if user also named a case in the same message
                candidate_query = self._extract_case_query(message)
                if candidate_query:
                    await self._resolve_from_query(candidate_query, access, context, extra)
                else:
                    extra["case_context_missing"] = True
            else:
                candidate_query = self._extract_case_query(message)
                if candidate_query:
                    await self._resolve_from_query(candidate_query, access, context, extra)

        context.extra = extra
        return context

    def resolve_sync(
        self,
        request: ChatRequest,
        identity: RequestIdentity,
    ) -> RequestContext:
        """Synchronous pass-through for backwards compatibility."""
        return request.context

    async def _resolve_from_query(
        self,
        query: str,
        access: AuthorizedLedAccess,
        context: RequestContext,
        extra: dict[str, Any],
    ) -> None:
        """Search accessible cases for a unique match."""
        try:
            matches = await access.search_cases(query=query, limit=5)
        except Exception:
            return

        if len(matches) == 1:
            case = matches[0]
            context.active_case = ItemRef(
                resource_type="case",
                resource_id=case.id,
                display_name=case.case_title or case.case_number or case.id,
            )
        elif len(matches) > 1:
            # Check for exact case_number or case_title match
            exact_matches = [
                c for c in matches
                if c.case_number.strip().lower() == query.lower()
                or c.case_title.strip().lower() == query.lower()
                or (c.cnr and c.cnr.strip().lower() == query.lower())
            ]
            if len(exact_matches) == 1:
                case = exact_matches[0]
                context.active_case = ItemRef(
                    resource_type="case",
                    resource_id=case.id,
                    display_name=case.case_title or case.case_number or case.id,
                )
            else:
                extra["ambiguous_cases"] = [
                    {"id": c.id, "title": c.case_title or c.case_number}
                    for c in matches
                ]

    def _extract_case_query(self, message: str) -> str | None:
        """Extract potential case name, CNR, or party vs party reference."""
        # 1. 16-character CNR
        cnr_match = re.search(r"\b([A-Z]{4}[0-9]{12})\b", message, re.IGNORECASE)
        if cnr_match:
            return cnr_match.group(1)

        # 2. "about <Case Name>" or "case <Case Name>"
        explicit_match = re.search(
            r"\b(?:about|case|in)\s+([A-Za-z0-9\s\.\-]{3,40})(?:\?|\.|$)",
            message,
            re.IGNORECASE,
        )
        if explicit_match:
            candidate = explicit_match.group(1).strip()
            # Exclude generic words
            if candidate.lower() not in {"this", "the", "a", "all", "any", "my cases", "cases"}:
                return candidate

        # 3. Party vs Party (e.g. "Ramesh vs State", "Sharma v. Verma")
        vs_match = re.search(
            r"([A-Za-z0-9\s]{2,25}\s+(?:vs\.?|v\.?|versus)\s+[A-Za-z0-9\s]{2,25})",
            message,
            re.IGNORECASE,
        )
        if vs_match:
            return vs_match.group(1).strip()

        return None
