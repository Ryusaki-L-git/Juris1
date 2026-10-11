"""
core/led_access.py — JURIS → LED data-access layer.

PURPOSE
    This is the ONLY component in JURIS permitted to read LED data.
    Tools never talk to Firestore directly.  They receive an
    `AuthorizedLedAccess` (see core/permission_engine.py) built on top of
    this layer, so every read is owner/team/assignment scoped and
    permission-checked.

ARCHITECTURE
    Juris Tool
        ↓
    Permission Check            (core/permission_engine.py)
        ↓
    AuthorizedLedAccess         (authorized, narrow read API)
        ↓
    LedDataAccess               ← this file
        ↓
    Firestore / LED cloud

DATA SAFETY RULES (non-negotiable)
    * JURIS is NEVER a second case database. It only reads LED-owned data.
    * Every query is scoped by the authenticated UID or by a team the
      authenticated UID is actually a member of.  A caller cannot obtain
      another user's Case by supplying a Case ID.
    * eCourts data is read-only everywhere. Nothing here writes to eCourts.
    * Missing / unavailable data is reported as absent — never invented.

Field naming follows the LED Flutter `LocalCaseRecord.toCloudMap()` contract
(lib/repositories/case_repository.dart) so JURIS and LED agree on shape.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from functools import lru_cache
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from core.config import settings
from core.errors import ConfigurationError, InvalidInputError

# Hard ceiling on any single read so a tool can never ask for "everything".
_MAX_LIMIT = 50
_DEFAULT_LIMIT = 10


def _clamp_limit(limit: int | None, *, default: int = _DEFAULT_LIMIT) -> int:
    if limit is None:
        return default
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise InvalidInputError("Limit must be an integer.")
    if limit < 1:
        raise InvalidInputError("Limit must be at least 1.")
    return min(limit, _MAX_LIMIT)


def _utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    # Firestore Timestamp
    to_date = getattr(value, "to_date", None)
    if callable(to_date):
        parsed = to_date()
        if isinstance(parsed, datetime):
            return (
                parsed.replace(tzinfo=timezone.utc)
                if parsed.tzinfo is None
                else parsed
            )
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    return None


# ── Domain models (the JURIS view of LED data) ────────────────────────────────

class LedDocumentMeta(BaseModel):
    """Metadata for a document attached to a case. Content is NOT included."""

    model_config = ConfigDict(frozen=True)

    name: str
    type: str = "Document"
    size: str = ""
    url: str | None = None
    content_available: bool = False


class LedCase(BaseModel):
    """A LED case as JURIS is allowed to see it."""

    model_config = ConfigDict(frozen=True)

    id: str
    owner_uid: str
    assigned_to: str | None = None
    # Deprecated compatibility field. Production authorization never reads this field.
    assigned_user_uid: str | None = None
    assigned_to_name: str = ""
    team_id: str | None = None
    team_owner_uid: str | None = None
    case_title: str = ""
    case_number: str = ""
    cnr: str | None = None
    client_name: str = ""
    opponent_name: str = ""
    court_name: str = ""
    case_type: str = ""
    status: str = ""
    handled_by: str = ""
    client_phone: str | None = None
    is_starred: bool = False
    is_deleted: bool = False
    next_hearing_date: datetime | None = None
    notes: list[str] = Field(default_factory=list)
    documents: list[LedDocumentMeta] = Field(default_factory=list)
    case_source: str = "manual"
    lifecycle_mode: str = "manual"
    ecourts_data: dict[str, Any] = Field(default_factory=dict)
    updated_at: datetime | None = None

    def to_context_dict(self) -> dict[str, Any]:
        """A compact representation safe to hand to the model."""
        return {
            "case_id": self.id,
            "case_title": self.case_title,
            "case_number": self.case_number,
            "cnr": self.cnr,
            "client_name": self.client_name,
            "opponent_name": self.opponent_name,
            "court_name": self.court_name,
            "case_type": self.case_type,
            "status": self.status,
            "handled_by": self.handled_by,
            "is_starred": self.is_starred,
            "next_hearing_date": (
                self.next_hearing_date.isoformat()
                if self.next_hearing_date
                else None
            ),
            "notes": list(self.notes),
            "documents": [
                {"name": d.name, "type": d.type, "size": d.size}
                for d in self.documents
            ],
            "assigned_to": self.assigned_to,
            "assigned_to_name": self.assigned_to_name,
            "team_id": self.team_id,
            "team_owner_uid": self.team_owner_uid,
            "case_source": self.case_source,
            "lifecycle_mode": self.lifecycle_mode,
        }


class LedClient(BaseModel):
    """A LED client profile as JURIS is allowed to see it."""

    model_config = ConfigDict(frozen=True)

    id: str
    owner_uid: str
    name: str = ""
    type: str = ""
    phone: str = ""
    email: str = ""
    address: str = ""
    is_deleted: bool = False

    def to_context_dict(self) -> dict[str, Any]:
        return {
            "client_id": self.id,
            "name": self.name,
            "type": self.type,
            "phone": self.phone,
            "email": self.email,
            "address": self.address,
        }


class LedHearing(BaseModel):
    """A single hearing/date entry for a case."""

    model_config = ConfigDict(frozen=True)

    case_id: str
    case_title: str = ""
    court_name: str = ""
    scheduled_at: datetime | None = None
    source: str = "led_case"

    def to_context_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "case_title": self.case_title,
            "court_name": self.court_name,
            "scheduled_at": (
                self.scheduled_at.isoformat() if self.scheduled_at else None
            ),
            "source": self.source,
        }


class LedTeamMembership(BaseModel):
    """The authenticated user's membership in a team."""

    model_config = ConfigDict(frozen=True)

    team_id: str
    user_id: str
    role: str = "member"
    is_active: bool = False

    @property
    def is_privileged(self) -> bool:
        return self.role in {"owner", "leader"}


class LedDocumentContent(BaseModel):
    """Document content, ONLY when the existing architecture exposes it."""

    model_config = ConfigDict(frozen=True)

    document_id: str
    case_id: str
    name: str = ""
    type: str = ""
    content: str | None = None
    content_available: bool = False
    unavailable_reason: str = ""


class CaseRelation(str, Enum):
    """How the authenticated user is related to a case."""

    OWNER = "owner"
    ASSIGNED = "assigned"
    TEAM_OWNER = "team_owner"
    TEAM = "team"  # deprecated: legacy compatibility only


class CaseAccess(BaseModel):
    """
    Evidence that a user may access a case, produced only by the data layer.

    The permission engine never re-derives ownership on its own; it consumes
    this record so there is exactly one place where "who can see this case"
    is computed.
    """

    model_config = ConfigDict(frozen=True)

    case: LedCase
    relation: CaseRelation
    team_role: str | None = None


# ── Data-access contract ──────────────────────────────────────────────────────

class LedDataAccess(Protocol):
    """
    Read-only access to LED data, scoped by an explicit owner identity.

    Implementations MUST scope every query by the passed ``user_id`` (or the
    team they belong to).  They must never return unscoped results.
    """

    async def get_case(
        self, case_id: str, *, owner_uid: str
    ) -> LedCase | None: ...

    async def list_cases_for_owner(
        self, owner_uid: str, *, limit: int | None = None
    ) -> list[LedCase]: ...

    async def list_cases_assigned_to(
        self, user_id: str, *, limit: int | None = None
    ) -> list[LedCase]: ...

    async def list_cases_for_team(
        self, team_id: str, *, user_id: str, limit: int | None = None
    ) -> list[LedCase]: ...

    async def get_client(
        self, client_id: str, *, owner_uid: str
    ) -> LedClient | None: ...

    async def list_clients_for_owner(
        self, owner_uid: str, *, limit: int | None = None
    ) -> list[LedClient]: ...

    async def get_team_membership(
        self, team_id: str, *, user_id: str
    ) -> LedTeamMembership | None: ...

    async def get_case_access(
        self, case_id: str, *, user_id: str
    ) -> CaseAccess | None: ...

    async def get_case_access_by_cnr(
        self, cnr: str, *, user_id: str
    ) -> CaseAccess | None: ...

    async def list_accessible_cases(
        self, user_id: str, *, limit: int | None = None
    ) -> list[LedCase]: ...

    async def list_team_ids_for_user(self, user_id: str) -> list[str]: ...


# ── Firestore implementation ─────────────────────────────────────────────────

class FirestoreLedDataAccess:
    """
    Reads LED data from Firestore using the server Admin SDK.

    Every method is scoped by an explicit identity parameter.  Even if a
    caller knows a Case ID, ownership is re-verified in the query itself
    (``where('ownerUid', '==', uid)``), so a foreign Case ID yields ``None``.
    """

    def __init__(self, client: Any | None = None) -> None:
        if client is None:
            if not settings.firebase_project_id:
                raise ConfigurationError(
                    "JURIS Firebase project ID is not configured."
                )
            from google.cloud import firestore

            client = firestore.Client(project=settings.firebase_project_id)
        self._client = client

    # ── internal helpers ────────────────────────────────────────────────────

    def _collection(self, name: str) -> Any:
        return self._client.collection(name)

    def _owned_case_query(self, owner_uid: str) -> list[Any]:
        """
        LED stores owner identity either in ``ownerUid`` (canonical) or the
        legacy ``userId`` field.  Both scopes are queried and merged so no
        owned case is missed, mirroring LED's own sync data source.
        """
        collection = self._collection("cases")
        return [
            collection.where("ownerUid", "==", owner_uid),
            collection.where("userId", "==", owner_uid),
        ]

    def _case_from_doc(self, document: Any) -> LedCase:
        data = document.data() or {}
        owner_uid = data.get("ownerUid") or data.get("userId") or ""
        raw_documents = data.get("documents")
        documents: list[LedDocumentMeta] = []
        if isinstance(raw_documents, list):
            for item in raw_documents:
                if isinstance(item, dict):
                    documents.append(
                        LedDocumentMeta(
                            name=str(item.get("name", "Document")),
                            type=str(item.get("type", "Document")),
                            size=str(item.get("size", "")),
                            url=item.get("url"),
                            # LED is local-first: cloud stores metadata only.
                            content_available=False,
                        )
                    )
                elif isinstance(item, str):
                    documents.append(LedDocumentMeta(name=item, type="Document"))

        raw_notes = data.get("notes")
        notes: list[str] = []
        if isinstance(raw_notes, list):
            notes = [str(note) for note in raw_notes if note is not None]

        return LedCase(
            id=document.id,
            owner_uid=str(owner_uid),
            assigned_to=data.get("assignedTo"),
            assigned_to_name=str(data.get("assignedToName") or ""),
            team_id=data.get("teamId"),
            team_owner_uid=data.get("teamOwnerUid"),
            case_title=str(data.get("caseTitle") or data.get("title") or ""),
            case_number=str(data.get("cnrNumber") or data.get("caseNumber") or ""),
            cnr=str(data.get("cnrNumber") or "") or None,
            client_name=str(data.get("clientName") or ""),
            opponent_name=str(data.get("opponentName") or ""),
            court_name=str(data.get("courtName") or ""),
            case_type=str(data.get("caseType") or ""),
            status=str(data.get("status") or "active").lower(),
            handled_by=str(data.get("handledBy") or ""),
            client_phone=data.get("clientPhone") or data.get("phone"),
            is_starred=bool(data.get("isStarred", False)),
            is_deleted=bool(data.get("isDeleted", False)),
            next_hearing_date=_utc(data.get("nextHearingDate")),
            notes=notes,
            documents=documents,
            case_source=str(data.get("caseSource") or "manual"),
            lifecycle_mode=str(data.get("lifecycleMode") or "manual"),
            ecourts_data=data.get("ecourts_data") if isinstance(data.get("ecourts_data"), dict) else {},
            updated_at=_utc(data.get("updatedAt") or data.get("lastUpdated")),
        )

    def _dedupe_cases(self, cases: list[LedCase]) -> list[LedCase]:
        by_id: dict[str, LedCase] = {}
        for case in cases:
            if case.id not in by_id:
                by_id[case.id] = case
        ordered = sorted(
            by_id.values(),
            key=lambda c: c.updated_at or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        return ordered

    def _get_owned_case_sync(self, case_id: str, owner_uid: str) -> LedCase | None:
        """
        Load a case by ID and re-verify ownership before returning it.

        A caller supplying another user's Case ID receives ``None`` —
        ownership is checked here, not trusted from the caller.
        """
        snapshot = self._client.document(f"cases/{case_id}").get()
        if not snapshot.exists:
            return None
        case = self._case_from_doc(snapshot)
        if case.owner_uid != owner_uid or case.is_deleted:
            return None
        return case

    def _list_owned_cases_sync(self, owner_uid: str, limit: int) -> list[LedCase]:
        found: list[LedCase] = []
        for query in self._owned_case_query(owner_uid):
            for document in query.limit(_MAX_LIMIT).stream():
                data = document.data() or {}
                if (data.get("ownerUid") or data.get("userId")) != owner_uid:
                    continue
                case = self._case_from_doc(document)
                if not case.is_deleted:
                    found.append(case)
        return self._dedupe_cases(found)[:limit]

    def _list_assigned_cases_sync(self, user_id: str, limit: int) -> list[LedCase]:
        collection = self._collection("cases")
        found: list[LedCase] = []
        for document in (
            collection.where("assignedTo", "==", user_id)
            .limit(_MAX_LIMIT)
            .stream()
        ):
            case = self._case_from_doc(document)
            # Assignment is valid only when a team is explicitly recorded and
            # the assignee is still an active member of that team.
            if case.is_deleted or not case.team_id:
                continue
            if self._membership_sync(case.team_id, user_id) is None:
                continue
            found.append(case)
        return self._dedupe_cases(found)[:limit]

    def _list_team_cases_sync(self, team_id: str, user_id: str, limit: int) -> list[LedCase]:
        # Never list a team's cases based on a caller-supplied team ID alone.
        # Active membership is the source of truth for team-scoped access.
        if self._membership_sync(team_id, user_id) is None:
            return []
        collection = self._collection("cases")
        found: list[LedCase] = []
        for document in (
            collection.where("teamId", "==", team_id).limit(_MAX_LIMIT).stream()
        ):
            case = self._case_from_doc(document)
            if case.is_deleted:
                continue
            # Preserve LED's rule: team membership alone does not expose every
            # case; the user must be owner, assigned, or explicit team owner.
            access = self._case_access_sync(case.id, user_id)
            if access is not None:
                found.append(access.case)
        return self._dedupe_cases(found)[:limit]

    def _client_from_doc(self, document: Any) -> LedClient:
        data = document.data() or {}
        return LedClient(
            id=document.id,
            owner_uid=str(data.get("userId") or data.get("ownerUid") or ""),
            name=str(data.get("name") or ""),
            type=str(data.get("type") or ""),
            phone=str(data.get("phone") or ""),
            email=str(data.get("email") or ""),
            address=str(data.get("address") or ""),
            is_deleted=bool(data.get("isDeleted", False)),
        )

    def _get_client_sync(self, client_id: str, owner_uid: str) -> LedClient | None:
        snapshot = self._client.document(f"clients/{client_id}").get()
        if not snapshot.exists:
            return None
        client = self._client_from_doc(snapshot)
        if client.owner_uid != owner_uid or client.is_deleted:
            return None
        return client

    def _list_clients_sync(self, owner_uid: str, limit: int) -> list[LedClient]:
        found: list[LedClient] = []
        for document in (
            self._collection("clients")
            .where("userId", "==", owner_uid)
            .limit(_MAX_LIMIT)
            .stream()
        ):
            client = self._client_from_doc(document)
            if client.owner_uid != owner_uid or client.is_deleted:
                continue
            found.append(client)
        found.sort(key=lambda c: c.name.lower())
        return found[:limit]

    def _membership_sync(self, team_id: str, user_id: str) -> LedTeamMembership | None:
        snapshot = (
            self._client.document(f"teams/{team_id}/members/{user_id}").get()
        )
        if not snapshot.exists:
            return None
        data = snapshot.data() or {}
        membership = LedTeamMembership(
            team_id=team_id,
            user_id=user_id,
            role=str(data.get("role") or "member"),
            is_active=bool(data.get("isActive", False)),
        )
        return membership if membership.is_active else None

    def _team_ids_sync(self, user_id: str) -> list[str]:
        """
        Every team the user is an ACTIVE member of.

        Uses a collection-group query over ``teams/*/members`` so membership
        is authoritative (never inferred from a case or from the client).
        """
        team_ids: list[str] = []
        for document in (
            self._client.collection_group("members")
            .where("userId", "==", user_id)
            .where("isActive", "==", True)
            .stream()
        ):
            parent = document.reference.parent.parent
            if parent is not None:
                team_ids.append(parent.id)
        return team_ids

    def _case_access_by_cnr_sync(self, cnr: str, user_id: str) -> CaseAccess | None:
        clean_cnr = (cnr or "").strip().upper()
        if not clean_cnr:
            return None
        for document in self._collection("cases").where("cnrNumber", "==", clean_cnr).limit(5).stream():
            access = self._case_access_sync(document.id, user_id)
            if access is not None:
                return access
        return None

    def _case_access_sync(self, case_id: str, user_id: str) -> CaseAccess | None:
        """Resolve access using LED's canonical case-assignment rules.

        Rules: owner always has access; an assigned user has access only when
        active in the case team; the team owner may access team cases; other
        active team members do NOT automatically inherit case access.
        """
        snapshot = self._client.document(f"cases/{case_id}").get()
        if not snapshot.exists:
            return None
        case = self._case_from_doc(snapshot)
        if case.is_deleted:
            return None
        if case.owner_uid == user_id:
            return CaseAccess(case=case, relation=CaseRelation.OWNER)
        if case.team_owner_uid == user_id:
            # A team-owner claim on a case is not enough by itself: membership
            # must still be active in the referenced team.
            if not case.team_id or self._membership_sync(case.team_id, user_id) is None:
                return None
            return CaseAccess(case=case, relation=CaseRelation.TEAM_OWNER)
        if case.assigned_to == user_id:
            # Assignment grants access only when tied to an active team
            # membership. Missing team metadata is ambiguous, so fail closed.
            if not case.team_id or self._membership_sync(case.team_id, user_id) is None:
                return None
            return CaseAccess(case=case, relation=CaseRelation.ASSIGNED)
        return None

    def _list_team_owner_cases_sync(self, user_id: str, limit: int) -> list[LedCase]:
        collection = self._collection("cases")
        found: list[LedCase] = []
        for document in collection.where("teamOwnerUid", "==", user_id).limit(_MAX_LIMIT).stream():
            case = self._case_from_doc(document)
            if case.is_deleted:
                continue
            access = self._case_access_sync(case.id, user_id)
            if access is not None:
                found.append(access.case)
        return self._dedupe_cases(found)[:limit]

    def _list_accessible_cases_sync(self, user_id: str, limit: int) -> list[LedCase]:
        found: list[LedCase] = []
        found.extend(self._list_owned_cases_sync(user_id, _MAX_LIMIT))
        found.extend(self._list_assigned_cases_sync(user_id, _MAX_LIMIT))
        found.extend(self._list_team_owner_cases_sync(user_id, _MAX_LIMIT))
        active = [case for case in found if not case.is_deleted]
        return self._dedupe_cases(active)[:limit]

    # ── public async API ────────────────────────────────────────────────────

    async def get_case(self, case_id: str, *, owner_uid: str) -> LedCase | None:
        import asyncio

        return await asyncio.to_thread(self._get_owned_case_sync, case_id, owner_uid)

    async def list_cases_for_owner(
        self, owner_uid: str, *, limit: int | None = None
    ) -> list[LedCase]:
        import asyncio

        return await asyncio.to_thread(
            self._list_owned_cases_sync, owner_uid, _clamp_limit(limit)
        )

    async def list_cases_assigned_to(
        self, user_id: str, *, limit: int | None = None
    ) -> list[LedCase]:
        import asyncio

        return await asyncio.to_thread(
            self._list_assigned_cases_sync, user_id, _clamp_limit(limit)
        )

    async def list_cases_for_team(
        self, team_id: str, *, user_id: str, limit: int | None = None
    ) -> list[LedCase]:
        import asyncio

        clean_team_id = (team_id or "").strip()
        clean_user_id = (user_id or "").strip()
        if not clean_team_id or not clean_user_id:
            return []
        return await asyncio.to_thread(
            self._list_team_cases_sync, clean_team_id, clean_user_id, _clamp_limit(limit)
        )

    async def get_client(self, client_id: str, *, owner_uid: str) -> LedClient | None:
        import asyncio

        return await asyncio.to_thread(self._get_client_sync, client_id, owner_uid)

    async def list_clients_for_owner(
        self, owner_uid: str, *, limit: int | None = None
    ) -> list[LedClient]:
        import asyncio

        return await asyncio.to_thread(
            self._list_clients_sync, owner_uid, _clamp_limit(limit)
        )

    async def get_team_membership(
        self, team_id: str, *, user_id: str
    ) -> LedTeamMembership | None:
        import asyncio

        return await asyncio.to_thread(self._membership_sync, team_id, user_id)

    async def get_case_access(
        self, case_id: str, *, user_id: str
    ) -> CaseAccess | None:
        import asyncio

        return await asyncio.to_thread(self._case_access_sync, case_id, user_id)

    async def get_case_access_by_cnr(
        self, cnr: str, *, user_id: str
    ) -> CaseAccess | None:
        import asyncio

        return await asyncio.to_thread(self._case_access_by_cnr_sync, cnr, user_id)

    async def list_accessible_cases(
        self, user_id: str, *, limit: int | None = None
    ) -> list[LedCase]:
        import asyncio

        return await asyncio.to_thread(
            self._list_accessible_cases_sync, user_id, _clamp_limit(limit)
        )

    async def list_team_ids_for_user(self, user_id: str) -> list[str]:
        import asyncio

        return await asyncio.to_thread(self._team_ids_sync, user_id)


class UnconfiguredLedDataAccess:
    """
    Fail-closed data access used when JURIS has no LED data source.

    Every call raises ConfigurationError, so no request can ever fall back
    to unscoped or fabricated data. This keeps the JURIS process bootable in
    a development environment that has not been pointed at Firebase.
    """

    def __getattr__(self, _name: str):
        async def _fail(*_args: Any, **_kwargs: Any):
            raise ConfigurationError(
                "JURIS is not configured to read LED data. "
                "Set JURIS_FIREBASE_PROJECT_ID and provide server credentials."
            )

        return _fail


@lru_cache(maxsize=1)
def get_led_data_access() -> LedDataAccess:
    """
    Process-wide LED data-access singleton.

    Returns a fail-closed implementation when Firebase is not configured,
    rather than raising at import time so the API can still start and report
    a structured configuration error per request.
    """
    if not settings.firebase_project_id:
        return UnconfiguredLedDataAccess()  # type: ignore[return-value]
    return FirestoreLedDataAccess()
