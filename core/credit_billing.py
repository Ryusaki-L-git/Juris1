"""Authenticated service boundary for metered JURIS AI Credits."""
from __future__ import annotations

from typing import Protocol

from core.authorization import require_verified_user
from core.identity import RequestIdentity
from core.tokens import TokenLedgerEntry


class CreditBillingRepository(Protocol):
    def reserve_credits(
        self, user_id: str, amount_minor_units: int, reason: str, reference_id: str
    ) -> TokenLedgerEntry: ...

    def settle_credits(
        self, user_id: str, reservation_reference: str, actual_minor_units: int
    ) -> float: ...


class CreditBillingService:
    """Server-only reservation and settlement for managed Gemini usage."""

    def __init__(self, repository: CreditBillingRepository) -> None:
        self._repository = repository

    def reserve(
        self,
        identity: RequestIdentity,
        amount_minor_units: int,
        *,
        reference_id: str,
    ) -> TokenLedgerEntry:
        user = require_verified_user(identity)
        return self._repository.reserve_credits(
            user.user_id,
            amount_minor_units,
            "JURIS AI usage reservation",
            reference_id,
        )

    def settle(
        self,
        identity: RequestIdentity,
        reference_id: str,
        actual_minor_units: int,
    ) -> float:
        user = require_verified_user(identity)
        return self._repository.settle_credits(
            user.user_id,
            reference_id,
            actual_minor_units,
        )
