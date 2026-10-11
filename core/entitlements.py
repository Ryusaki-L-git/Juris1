from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from core.authorization import require_verified_user
from core.errors import AuthorizationFailure, SubscriptionNotEligible
from core.identity import RequestIdentity


class PlanId(str, Enum):
    FREE = "free"
    PREMIUM_CLOUD = "premium_cloud"
    CHAMBER_ENTERPRISE = "chamber_enterprise"


class EntitlementStatus(str, Enum):
    ACTIVE = "active"
    GRACE_PERIOD = "grace_period"
    CANCELED = "canceled"
    EXPIRED = "expired"
    PENDING = "pending"


class Feature(str, Enum):
    LOCAL_CASES = "local_cases"
    CLOUD_SYNC = "cloud_sync"
    JURIS_ACCESS = "juris_access"
    TEAM_COLLABORATION = "team_collaboration"


PLAN_FEATURES: dict[PlanId, frozenset[Feature]] = {
    PlanId.FREE: frozenset({Feature.LOCAL_CASES}),
    PlanId.PREMIUM_CLOUD: frozenset(
        {Feature.LOCAL_CASES, Feature.CLOUD_SYNC, Feature.JURIS_ACCESS}
    ),
    PlanId.CHAMBER_ENTERPRISE: frozenset(
        {
            Feature.LOCAL_CASES,
            Feature.CLOUD_SYNC,
            Feature.JURIS_ACCESS,
            Feature.TEAM_COLLABORATION,
        }
    ),
}


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    raise ValueError("Invalid entitlement timestamp.")


class EntitlementSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    plan_id: PlanId
    status: EntitlementStatus
    enabled_features: frozenset[Feature] = Field(default_factory=frozenset)
    effective_at: datetime | None = None
    expires_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_paid(self) -> bool:
        return self.plan_id is not PlanId.FREE

    @classmethod
    def free(cls) -> EntitlementSnapshot:
        return cls(
            plan_id=PlanId.FREE,
            status=EntitlementStatus.ACTIVE,
            enabled_features=PLAN_FEATURES[PlanId.FREE],
        )

    @classmethod
    def from_document(cls, data: dict[str, Any]) -> EntitlementSnapshot:
        try:
            plan_id = PlanId(data["planId"])
            status = EntitlementStatus(data["status"])
            raw_features = data["enabledFeatures"]
            if not isinstance(raw_features, list) or any(
                not isinstance(item, str) for item in raw_features
            ):
                raise ValueError("Invalid enabledFeatures.")
            features = frozenset(Feature(item) for item in raw_features)
            if not features.issubset(PLAN_FEATURES[plan_id]):
                raise ValueError("Entitlement feature exceeds plan capabilities.")
            return cls(
                plan_id=plan_id,
                status=status,
                enabled_features=features,
                effective_at=_parse_timestamp(data.get("effectiveAt")),
                expires_at=_parse_timestamp(data.get("expiresAt")),
                metadata=data.get("metadata", {}),
            )
        except (KeyError, TypeError, ValueError):
            raise AuthorizationFailure(
                "Entitlement data is invalid; access is denied."
            ) from None

    def is_effective(self, at: datetime | None = None) -> bool:
        now = at or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        if self.status not in {
            EntitlementStatus.ACTIVE,
            EntitlementStatus.GRACE_PERIOD,
        }:
            return False
        if self.effective_at is not None and now < self.effective_at:
            return False
        if self.expires_at is not None and now >= self.expires_at:
            return False
        return True

    def allows(self, feature: Feature | str, at: datetime | None = None) -> bool:
        try:
            required = Feature(feature)
        except (TypeError, ValueError):
            return False
        return self.is_effective(at) and required in self.enabled_features


class EntitlementRepository(Protocol):
    def get_entitlement(self, user_id: str) -> dict[str, Any] | None: ...


class EntitlementService:
    def __init__(self, repository: EntitlementRepository) -> None:
        self._repository = repository

    def get_current(self, identity: RequestIdentity) -> EntitlementSnapshot:
        user = require_verified_user(identity)
        document = self._repository.get_entitlement(user.user_id)
        if document is None:
            return EntitlementSnapshot.free()
        return EntitlementSnapshot.from_document(document)

    def has_entitlement(
        self,
        identity: RequestIdentity,
        feature: Feature | str,
        *,
        at: datetime | None = None,
    ) -> bool:
        try:
            return self.get_current(identity).allows(feature, at)
        except AuthorizationFailure:
            return False

    def has_entitlement_for_user(
        self,
        user_id: str,
        feature: Feature | str,
        *,
        at: datetime | None = None,
    ) -> bool:
        """
        Fail-closed entitlement check keyed by the authenticated LED user id.

        Used by the permission engine, which works with a verified JurisUser
        rather than a RequestIdentity.  A missing, malformed, or expired
        entitlement record always yields False — never True.
        """
        clean_user_id = (user_id or "").strip()
        if not clean_user_id:
            return False
        document = self._repository.get_entitlement(clean_user_id)
        if document is None:
            return False
        try:
            snapshot = EntitlementSnapshot.from_document(document)
        except AuthorizationFailure:
            return False
        return snapshot.allows(feature, at)

    def require_entitlement(
        self,
        identity: RequestIdentity,
        feature: Feature | str,
        *,
        at: datetime | None = None,
    ) -> EntitlementSnapshot:
        snapshot = self.get_current(identity)
        if not snapshot.allows(feature, at):
            raise SubscriptionNotEligible()
        return snapshot
