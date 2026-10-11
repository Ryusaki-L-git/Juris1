"""
core/authorized_access.py — The controlled data-access boundary for tools.

PURPOSE
    Tools never touch Firestore or the raw LedDataAccess.  They receive an
    `AuthorizedLedAccess` bound to one authenticated user, and every method
    re-verifies authorization before returning data.

        Juris Tool
            ↓
        AuthorizedLedAccess.require_case(...)   ← authorization here
            ↓
        LedDataAccess (owner/team/assignment scoped queries)
            ↓
        Authorized data only

FAIL-CLOSED RULE
    A Case ID the user is not entitled to raises AuthorizationFailure
    (HTTP 403) and returns no data.  There is no unscoped fallback and no
    fabricated record.
"""

from __future__ import annotations

from core.errors import AuthorizationFailure, NotFoundError
from core.identity import JurisUser
from core.led_access import (
    CaseAccess,
    LedCase,
    LedClient,
    LedDataAccess,
    LedDocumentContent,
    LedDocumentMeta,
    LedHearing,
)

_SEARCH_LIMIT = 10
_HEARING_LIMIT = 5


class AuthorizedLedAccess:
    """A per-request, per-user view of LED data handed to tools."""

    def __init__(self, data_access: LedDataAccess, user: JurisUser) -> None:
        self._data_access = data_access
        self._user = user

    @property
    def user_id(self) -> str:
        return self._user.user_id

    # ── cases ───────────────────────────────────────────────────────────────

    async def require_case_access(self, case_id: str) -> CaseAccess:
        clean_id = (case_id or "").strip()
        if not clean_id:
            raise NotFoundError("A Case identifier is required.")
        access = await self._data_access.get_case_access(
            clean_id, user_id=self._user.user_id
        )
        if access is None:
            raise AuthorizationFailure("You do not have access to this Case.")
        return access

    async def get_case(self, case_id: str) -> LedCase:
        return (await self.require_case_access(case_id)).case

    async def require_case_access_by_cnr(self, cnr: str) -> CaseAccess:
        clean_cnr = (cnr or "").strip().upper()
        if not clean_cnr:
            raise NotFoundError("A CNR is required.")
        access = await self._data_access.get_case_access_by_cnr(
            clean_cnr, user_id=self._user.user_id
        )
        if access is None:
            raise AuthorizationFailure("You do not have access to a LED Case with this CNR.")
        return access

    async def list_cases(self, limit: int | None = None) -> list[LedCase]:
        return await self._data_access.list_accessible_cases(
            self._user.user_id, limit=limit
        )

    async def search_cases(
        self, query: str = "", limit: int | None = None
    ) -> list[LedCase]:
        """Keyword search over the user's ACCESSIBLE cases only."""
        effective_limit = _SEARCH_LIMIT if limit is None else limit
        cases = await self._data_access.list_accessible_cases(
            self._user.user_id, limit=limit
        )
        needle = (query or "").strip().lower()
        if not needle:
            return cases[:effective_limit]
        matches = [
            case
            for case in cases
            if needle in _case_haystack(case)
        ]
        return matches[:effective_limit]

    # ── clients ─────────────────────────────────────────────────────────────

    async def get_client(self, client_id: str) -> LedClient:
        clean_id = (client_id or "").strip()
        if not clean_id:
            raise NotFoundError("A Client identifier is required.")
        client = await self._data_access.get_client(
            clean_id, owner_uid=self._user.user_id
        )
        if client is None:
            raise AuthorizationFailure("You do not have access to this Client.")
        return client

    async def search_clients(
        self, query: str = "", limit: int | None = None
    ) -> list[LedClient]:
        effective_limit = _SEARCH_LIMIT if limit is None else limit
        clients = await self._data_access.list_clients_for_owner(
            self._user.user_id, limit=limit
        )
        needle = (query or "").strip().lower()
        if not needle:
            return clients[:effective_limit]
        matches = [c for c in clients if needle in _client_haystack(c)]
        return matches[:effective_limit]

    async def get_client_for_case(self, case_id: str) -> LedClient | None:
        """Resolve the Case's client from the user's own client records."""
        case = await self.get_case(case_id)
        target = case.client_name.strip().lower()
        if not target:
            return None
        for client in await self._data_access.list_clients_for_owner(
            self._user.user_id
        ):
            if client.name.strip().lower() == target:
                return client
        return None

    # ── hearings ────────────────────────────────────────────────────────────

    async def list_hearings(
        self, case_id: str | None = None, limit: int | None = None
    ) -> list[LedHearing]:
        """Upcoming hearing dates, derived from authorized cases only."""
        effective_limit = _HEARING_LIMIT if limit is None else limit
        if case_id:
            sources = [await self.get_case(case_id)]
        else:
            sources = await self._data_access.list_accessible_cases(
                self._user.user_id
            )
        hearings = [
            LedHearing(
                case_id=case.id,
                case_title=case.case_title,
                court_name=case.court_name,
                scheduled_at=case.next_hearing_date,
                source="led_case",
            )
            for case in sources
            if case.next_hearing_date is not None
        ]
        hearings.sort(key=lambda hearing: hearing.scheduled_at)  # type: ignore[arg-type,return-value]
        return hearings[:effective_limit]

    # ── documents ───────────────────────────────────────────────────────────

    async def list_documents(self, case_id: str) -> list[LedDocumentMeta]:
        return list((await self.get_case(case_id)).documents)

    async def get_document_content(
        self, case_id: str, document_id: str
    ) -> LedDocumentContent:
        """
        Document content, ONLY where the existing architecture exposes it.

        LED is local-first: the cloud representation stores document
        METADATA only.  JURIS reports content as unavailable rather than
        inventing it or fetching it from an unapproved store.
        """
        case = await self.get_case(case_id)
        wanted = (document_id or "").strip().lower()
        for document in case.documents:
            if document.name.strip().lower() == wanted:
                return LedDocumentContent(
                    document_id=document_id,
                    case_id=case.id,
                    name=document.name,
                    type=document.type,
                    content=None,
                    content_available=False,
                    unavailable_reason=(
                        "LED stores this document on the user's device. It "
                        "is not available to JURIS until the user shares a "
                        "synced copy."
                    ),
                )
        raise NotFoundError("That document is not attached to this Case.")


def _case_haystack(case: LedCase) -> str:
    return " ".join(
        [
            case.case_title,
            case.case_number,
            case.client_name,
            case.opponent_name,
            case.court_name,
            case.case_type,
            case.cnr or "",
        ]
    ).lower()


def _client_haystack(client: LedClient) -> str:
    return " ".join(
        [client.name, client.phone, client.email, client.address, client.type]
    ).lower()
