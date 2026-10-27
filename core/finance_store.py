from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

try:
    from google.cloud import firestore
except ImportError:  # Optional at import time; required only for Firestore-backed runtime.
    firestore = None  # type: ignore[assignment]

from core.config import settings
from core.entitlements import EntitlementRepository
from core.errors import (
    AuthorizationFailure,
    ConfigurationError,
    InsufficientTokens,
    TokenOperationConflict,
)
from core.tokens import (
    TokenLedgerEntry,
    TokenMutationResult,
    TokenRepository,
    TokenType,
)


class BillingSourceChoiceRequired(Exception):
    """Raised when a user's explicit ask-before-charge preference needs UI input."""


class TeamCreditAllocationInsufficient(Exception):
    def __init__(self, required: float, available: float) -> None:
        super().__init__("The member's team JURIS Credit allocation is insufficient.")
        self.required = required
        self.available = available


class FirestoreFinanceRepository(EntitlementRepository, TokenRepository):
    """Admin-SDK repository for server-owned entitlements, balances and ledger."""

    def __init__(self, client: firestore.Client | None = None) -> None:
        if client is None:
            if not settings.firebase_project_id:
                raise ConfigurationError("JURIS Firebase project ID is not configured.")
            from core.firestore_client import get_firestore_client
            client = get_firestore_client()
        self._client = client

    def get_entitlement(self, user_id: str) -> dict[str, Any] | None:
        snapshot = self._client.collection("_server_entitlements").document(user_id).get()
        return snapshot.to_dict() if snapshot.exists else None

    def get_balance(self, user_id: str, token_type: TokenType) -> int | float:
        snapshot = self._balance_ref(user_id, token_type).get()
        if not snapshot.exists:
            return 0
        data = snapshot.to_dict() or {}
        units = self._stored_balance_units(data, token_type)
        return self._balance_in_tokens(units, token_type)

    @staticmethod
    def _stored_balance_units(data: dict[str, Any], token_type: TokenType) -> int:
        if token_type is TokenType.CASE:
            # Legacy accounts store whole tokens in `balance`. Once a case
            # account has been written by the fractional-token system, the
            # canonical integer balance is `balanceMinorUnits` (100ths).
            if "balanceMinorUnits" in data:
                value = data.get("balanceMinorUnits")
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ConfigurationError("Stored case-token balance is invalid.")
                return value
            legacy = data.get("balance", 0)
            if isinstance(legacy, bool) or not isinstance(legacy, int) or legacy < 0:
                raise ConfigurationError("Stored case-token balance is invalid.")
            return legacy * 100

        # Existing JURIS balances are whole Credits. New balances use canonical
        # hundredths so usage charges can be as small as 0.01 Credit.
        if "balanceMinorUnits" in data:
            value = data.get("balanceMinorUnits")
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ConfigurationError("Stored JURIS Credit balance is invalid.")
            return value
        balance = data.get("balance", 0)
        if isinstance(balance, bool) or not isinstance(balance, int) or balance < 0:
            raise ConfigurationError("Stored JURIS Credit balance is invalid.")
        return balance * 100

    @staticmethod
    def _balance_in_tokens(stored_units: int, token_type: TokenType) -> float:
        # Both currencies use hundredths internally; legacy whole-unit balances
        # are normalized by _stored_balance_units before this conversion.
        return stored_units / 100

    @staticmethod
    def _amount_in_storage_units(amount: int, token_type: TokenType) -> int:
        return amount * 100

    @staticmethod
    def _account_balance_fields(
        balance_units: int, token_type: TokenType, updated_at: datetime
    ) -> dict[str, Any]:
        if token_type is TokenType.CASE:
            return {
                "tokenType": token_type.value,
                "balance": balance_units / 100,
                "balanceMinorUnits": balance_units,
                "balanceUnit": "hundredths",
                "updatedAt": updated_at,
            }
        return {
            "tokenType": token_type.value,
            "balance": balance_units / 100,
            "balanceMinorUnits": balance_units,
            "balanceUnit": "hundredths",
            "updatedAt": updated_at,
        }

    def reserve_credits(
        self,
        user_id: str,
        amount_minor_units: int,
        reason: str,
        reference_id: str,
    ) -> TokenLedgerEntry:
        """Reserve Credits from the configured personal/team wallet priority."""
        profile_snapshot = self._client.collection("users").document(user_id).get()
        profile = profile_snapshot.to_dict() if profile_snapshot.exists else {}
        preferences = (profile or {}).get("billingPreferences")
        preferences = preferences if isinstance(preferences, dict) else {}
        priority = preferences.get("usageDeductionPriority", "personal_first")
        team_id = preferences.get("preferredTeamId")
        if not isinstance(team_id, str) or not team_id.strip():
            team_id = None
        else:
            team_id = team_id.strip()
        if priority not in {"personal_first", "team_first", "ask"}:
            priority = "personal_first"
        if priority == "ask" and team_id:
            raise BillingSourceChoiceRequired(
                "Choose personal or team Credits before continuing."
            )
        if not team_id:
            return self._reserve_personal_credits(
                user_id, amount_minor_units, reason, reference_id
            )
        if priority == "team_first":
            try:
                return self._reserve_team_credits(
                    user_id, team_id, amount_minor_units, reason, reference_id
                )
            except (InsufficientTokens, TeamCreditAllocationInsufficient):
                return self._reserve_personal_credits(
                    user_id, amount_minor_units, reason, reference_id
                )
        try:
            return self._reserve_personal_credits(
                user_id, amount_minor_units, reason, reference_id
            )
        except InsufficientTokens as personal_error:
            try:
                return self._reserve_team_credits(
                    user_id, team_id, amount_minor_units, reason, reference_id
                )
            except (InsufficientTokens, TeamCreditAllocationInsufficient):
                raise personal_error

    def _reserve_team_credits(
        self,
        user_id: str,
        team_id: str,
        amount_minor_units: int,
        reason: str,
        reference_id: str,
    ) -> TokenLedgerEntry:
        if isinstance(amount_minor_units, bool) or not isinstance(amount_minor_units, int) or amount_minor_units < 1:
            raise ValueError("Credit reservation must be at least 0.01 Credit.")
        clean_user, clean_team = user_id.strip(), team_id.strip()
        clean_reason, clean_reference = reason.strip(), reference_id.strip()
        if not clean_user or not clean_team or "/" in clean_team or not clean_reason or not clean_reference:
            raise ValueError("Team Credit reservation requires valid user, team, reason, and reference.")
        transaction_id = hashlib.sha256(
            f"{TokenType.JURIS.value}:{clean_reference}".encode("utf-8")
        ).hexdigest()
        team_ref = self._client.collection("teams").document(clean_team)
        member_ref = team_ref.collection("members").document(clean_user)
        account_ref = self._team_balance_ref(clean_team, TokenType.JURIS)
        ledger_ref = self._team_ledger_collection(clean_team).document(transaction_id)
        marker_ref = self._ai_reservation_marker_ref(clean_user, clean_reference)
        period = datetime.now(timezone.utc).strftime("%Y-%m")
        transaction = self._client.transaction(max_attempts=5)

        @firestore.transactional
        def reserve_team_in_transaction(txn: firestore.Transaction) -> TokenLedgerEntry:
            prior_snapshot = ledger_ref.get(transaction=txn)
            team_snapshot = team_ref.get(transaction=txn)
            member_snapshot = member_ref.get(transaction=txn)
            account_snapshot = account_ref.get(transaction=txn)
            marker_snapshot = marker_ref.get(transaction=txn)
            if prior_snapshot.exists:
                prior_data = prior_snapshot.to_dict() or {}
                if (
                    prior_data.get("userId") != clean_user
                    or prior_data.get("teamId") != clean_team
                    or prior_data.get("tokenType") != TokenType.JURIS.value
                    or prior_data.get("direction") != "debit"
                    or prior_data.get("amountMinorUnits") != amount_minor_units
                    or prior_data.get("reason") != clean_reason
                    or prior_data.get("referenceId") != clean_reference
                ):
                    raise TokenOperationConflict()
                return self._ledger_entry(prior_data)

            team = team_snapshot.to_dict() or {}
            member = member_snapshot.to_dict() or {}
            if not team_snapshot.exists or team.get("isActive") is False:
                raise AuthorizationFailure("Active team not found.")
            if (
                not member_snapshot.exists
                or member.get("isActive") is not True
                or member.get("identityType") != "firebase_uid"
                or member.get("userId") != clean_user
            ):
                raise AuthorizationFailure("An active LED team membership is required.")

            limits = member.get("billingLimits")
            limits = limits if isinstance(limits, dict) else {}
            limit = limits.get("jurisCreditLimitMinorUnits", 0)
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
                raise ConfigurationError("Stored member JURIS Credit allocation is invalid.")
            usage = member.get("billingUsage")
            usage = usage if isinstance(usage, dict) else {}
            used = usage.get("jurisCreditUsedMinorUnits", 0) if usage.get("period") == period else 0
            if isinstance(used, bool) or not isinstance(used, int) or used < 0:
                raise ConfigurationError("Stored member JURIS Credit usage is invalid.")
            member_available = max(0, limit - used)
            if member_available < amount_minor_units:
                raise TeamCreditAllocationInsufficient(amount_minor_units / 100, member_available / 100)

            account_data = account_snapshot.to_dict() or {}
            available_units = self._stored_balance_units(account_data, TokenType.JURIS)
            if amount_minor_units > available_units:
                raise InsufficientTokens(amount_minor_units / 100, available_units / 100)
            if marker_snapshot.exists:
                marker_data = marker_snapshot.to_dict() or {}
                if marker_data.get("userId") != clean_user or marker_data.get("referenceId") != clean_reference:
                    raise TokenOperationConflict()
                if marker_data.get("source") != "team" or marker_data.get("teamId") != clean_team:
                    raise TokenOperationConflict()

            balance_after_units = available_units - amount_minor_units
            created_at = datetime.now(timezone.utc)
            entry = TokenLedgerEntry(
                transaction_id=transaction_id,
                user_id=clean_user,
                actor_user_id=clean_user,
                token_type=TokenType.JURIS,
                amount=amount_minor_units / 100,
                direction="debit",
                reason=clean_reason,
                reference_id=clean_reference,
                created_at=created_at,
                balance_after=balance_after_units / 100,
            )
            member_after = dict(member)
            member_after["billingUsage"] = {
                **usage,
                "period": period,
                "caseTokenUsedMinorUnits": usage.get("caseTokenUsedMinorUnits", 0) if usage.get("period") == period else 0,
                "jurisCreditUsedMinorUnits": used + amount_minor_units,
                "updatedAt": created_at,
            }
            txn.set(account_ref, self._account_balance_fields(balance_after_units, TokenType.JURIS, created_at), merge=True)
            txn.set(member_ref, member_after, merge=True)
            txn.create(ledger_ref, {
                **self._ledger_document(entry),
                "teamId": clean_team,
                "billingPeriod": period,
            })
            txn.set(marker_ref, {
                "userId": clean_user,
                "teamId": clean_team,
                "source": "team",
                "referenceId": clean_reference,
                "amountMinorUnits": amount_minor_units,
                "billingPeriod": period,
                "createdAt": created_at,
            })
            return entry

        return reserve_team_in_transaction(transaction)

    def _team_balance_ref(self, team_id: str, token_type: TokenType):
        return (
            self._client.collection("_server_team_token_accounts")
            .document(team_id).collection("balances").document(token_type.value)
        )

    def _team_ledger_collection(self, team_id: str):
        return self._client.collection("_server_team_token_accounts").document(team_id).collection("ledger")

    def _ai_reservation_marker_ref(self, user_id: str, reference_id: str):
        key = hashlib.sha256(f"{user_id}:{reference_id}".encode("utf-8")).hexdigest()
        return self._client.collection("_server_ai_usage_reservations").document(key)

    def _reserve_personal_credits(
        self,
        user_id: str,
        amount_minor_units: int,
        reason: str,
        reference_id: str,
    ) -> TokenLedgerEntry:
        """Atomically reserve JURIS Credits before a managed Gemini call."""
        if isinstance(amount_minor_units, bool) or not isinstance(amount_minor_units, int) or amount_minor_units < 1:
            raise ValueError("Credit reservation must be at least 0.01 Credit.")
        clean_user = user_id.strip()
        clean_reason = reason.strip()
        clean_reference = reference_id.strip()
        if not clean_user or not clean_reason or not clean_reference:
            raise ValueError("Credit reservation requires user, reason, and reference.")
        transaction_id = hashlib.sha256(
            f"{TokenType.JURIS.value}:{clean_reference}".encode("utf-8")
        ).hexdigest()
        account_ref = self._balance_ref(clean_user, TokenType.JURIS)
        ledger_ref = self._ledger_collection(clean_user).document(transaction_id)
        marker_ref = self._ai_reservation_marker_ref(clean_user, clean_reference)
        transaction = self._client.transaction(max_attempts=5)

        @firestore.transactional
        def reserve_in_transaction(txn: firestore.Transaction) -> TokenLedgerEntry:
            prior_snapshot = ledger_ref.get(transaction=txn)
            marker_snapshot = marker_ref.get(transaction=txn)
            if prior_snapshot.exists:
                prior_data = prior_snapshot.to_dict() or {}
                prior = self._ledger_entry(prior_data)
                if (
                    prior.user_id != clean_user
                    or prior.token_type is not TokenType.JURIS
                    or prior.direction != "debit"
                    or prior.reason != clean_reason
                    or prior.reference_id != clean_reference
                    or round(prior.amount * 100) != amount_minor_units
                ):
                    raise TokenOperationConflict()
                return prior

            if marker_snapshot.exists:
                marker_data = marker_snapshot.to_dict() or {}
                if marker_data.get("userId") != clean_user or marker_data.get("referenceId") != clean_reference or marker_data.get("source") not in {None, "personal"}:
                    raise TokenOperationConflict()

            account_snapshot = account_ref.get(transaction=txn)
            account_data = account_snapshot.to_dict() or {}
            available_units = self._stored_balance_units(account_data, TokenType.JURIS)
            if amount_minor_units > available_units:
                raise InsufficientTokens(amount_minor_units / 100, available_units / 100)

            balance_after_units = available_units - amount_minor_units
            created_at = datetime.now(timezone.utc)
            entry = TokenLedgerEntry(
                transaction_id=transaction_id,
                user_id=clean_user,
                actor_user_id=clean_user,
                token_type=TokenType.JURIS,
                amount=amount_minor_units / 100,
                direction="debit",
                reason=clean_reason,
                reference_id=clean_reference,
                created_at=created_at,
                balance_after=balance_after_units / 100,
            )
            txn.set(
                account_ref,
                self._account_balance_fields(balance_after_units, TokenType.JURIS, created_at),
                merge=True,
            )
            txn.create(ledger_ref, self._ledger_document(entry))
            txn.set(marker_ref, {
                "userId": clean_user,
                "teamId": None,
                "source": "personal",
                "referenceId": clean_reference,
                "amountMinorUnits": amount_minor_units,
                "createdAt": created_at,
            })
            return entry

        return reserve_in_transaction(transaction)

    def _settle_team_credits(
        self,
        user_id: str,
        team_id: str,
        reservation_reference: str,
        actual_minor_units: int,
    ) -> float:
        clean_user, clean_team = user_id.strip(), team_id.strip()
        clean_reference = reservation_reference.strip()
        reservation_id = hashlib.sha256(
            f"{TokenType.JURIS.value}:{clean_reference}".encode("utf-8")
        ).hexdigest()
        reservation_ref = self._team_ledger_collection(clean_team).document(reservation_id)
        marker_ref = self._ai_reservation_marker_ref(clean_user, clean_reference)
        settlement_id = hashlib.sha256(
            f"{clean_user}:{clean_reference}:settlement".encode("utf-8")
        ).hexdigest()
        settlement_ref = self._client.collection("_server_ai_usage_settlements").document(settlement_id)
        account_ref = self._team_balance_ref(clean_team, TokenType.JURIS)
        member_ref = self._client.collection("teams").document(clean_team).collection("members").document(clean_user)
        transaction = self._client.transaction(max_attempts=5)

        @firestore.transactional
        def settle_team_in_transaction(txn: firestore.Transaction) -> float:
            prior_settlement = settlement_ref.get(transaction=txn)
            marker_snapshot = marker_ref.get(transaction=txn)
            reservation_snapshot = reservation_ref.get(transaction=txn)
            account_snapshot = account_ref.get(transaction=txn)
            member_snapshot = member_ref.get(transaction=txn)
            if prior_settlement.exists:
                prior_data = prior_settlement.to_dict() or {}
                if prior_data.get("actualMinorUnits") != actual_minor_units:
                    raise TokenOperationConflict()
                return float(prior_data.get("balanceAfter", 0.0))
            marker_data = marker_snapshot.to_dict() or {}
            if (
                not marker_snapshot.exists
                or marker_data.get("userId") != clean_user
                or marker_data.get("referenceId") != clean_reference
                or marker_data.get("source") != "team"
                or marker_data.get("teamId") != clean_team
            ):
                raise TokenOperationConflict()
            if not reservation_snapshot.exists or not account_snapshot.exists or not member_snapshot.exists:
                raise TokenOperationConflict()
            reservation_data = reservation_snapshot.to_dict() or {}
            if (
                reservation_data.get("userId") != clean_user
                or reservation_data.get("teamId") != clean_team
                or reservation_data.get("tokenType") != TokenType.JURIS.value
                or reservation_data.get("direction") != "debit"
                or reservation_data.get("referenceId") != clean_reference
            ):
                raise TokenOperationConflict()
            reserved_units = reservation_data.get("amountMinorUnits")
            if isinstance(reserved_units, bool) or not isinstance(reserved_units, int) or reserved_units < 0:
                reserved_units = round(float(reservation_data.get("amount", 0)) * 100)
            if actual_minor_units > reserved_units:
                raise TokenOperationConflict()
            account_data = account_snapshot.to_dict() or {}
            current_units = self._stored_balance_units(account_data, TokenType.JURIS)
            delta = reserved_units - actual_minor_units
            balance_after_units = current_units + delta
            member = member_snapshot.to_dict() or {}
            usage = member.get("billingUsage")
            usage = usage if isinstance(usage, dict) else {}
            limits = member.get("billingLimits")
            limits = limits if isinstance(limits, dict) else {}
            limit = limits.get("jurisCreditLimitMinorUnits", 0)
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
                raise ConfigurationError("Stored member JURIS Credit allocation is invalid.")
            period = datetime.now(timezone.utc).strftime("%Y-%m")
            usage_is_current = usage.get("period") == period
            same_period = marker_data.get("billingPeriod") == period and usage_is_current
            used = usage.get("jurisCreditUsedMinorUnits", 0) if usage_is_current else 0
            case_used = usage.get("caseTokenUsedMinorUnits", 0) if usage_is_current else 0
            if isinstance(used, bool) or not isinstance(used, int) or used < 0:
                raise ConfigurationError("Stored member JURIS Credit usage is invalid.")
            if isinstance(case_used, bool) or not isinstance(case_used, int) or case_used < 0:
                raise ConfigurationError("Stored member Case Token usage is invalid.")
            used_after = max(0, used - delta) if same_period else used
            now = datetime.now(timezone.utc)
            if delta:
                txn.set(account_ref, self._account_balance_fields(balance_after_units, TokenType.JURIS, now), merge=True)
                member_after = dict(member)
                member_after["billingUsage"] = {
                    **usage,
                    "period": period,
                    "caseTokenUsedMinorUnits": case_used,
                    "jurisCreditUsedMinorUnits": used_after,
                    "updatedAt": now,
                }
                txn.set(member_ref, member_after, merge=True)
            if actual_minor_units == 0:
                txn.delete(reservation_ref)
            else:
                settled_data = dict(reservation_data)
                settled_data.update({
                    "amount": actual_minor_units / 100,
                    "amountMinorUnits": actual_minor_units,
                    "reason": "JURIS AI usage",
                    "balanceAfter": balance_after_units / 100,
                    "balanceAfterMinorUnits": balance_after_units,
                    "settledAt": now,
                })
                txn.set(reservation_ref, settled_data, merge=True)
            txn.create(settlement_ref, {
                "userId": clean_user,
                "teamId": clean_team,
                "source": "team",
                "reservationReference": clean_reference,
                "reservedMinorUnits": reserved_units,
                "actualMinorUnits": actual_minor_units,
                "balanceAfter": balance_after_units / 100,
                "createdAt": now,
            })
            return balance_after_units / 100

        return settle_team_in_transaction(transaction)

    def settle_credits(
        self,
        user_id: str,
        reservation_reference: str,
        actual_minor_units: int,
    ) -> float:
        """Settle a prior reservation to actual usage and refund the unused hold atomically."""
        if isinstance(actual_minor_units, bool) or not isinstance(actual_minor_units, int) or actual_minor_units < 0:
            raise ValueError("Actual Credit charge must be non-negative minor units.")
        clean_user = user_id.strip()
        clean_reference = reservation_reference.strip()
        if not clean_user or not clean_reference:
            raise ValueError("Credit settlement requires user and reservation reference.")
        marker_snapshot = self._ai_reservation_marker_ref(clean_user, clean_reference).get()
        marker_data = marker_snapshot.to_dict() if marker_snapshot.exists else {}
        if marker_data.get("source") == "team":
            team_id = marker_data.get("teamId")
            if not isinstance(team_id, str) or not team_id.strip():
                raise TokenOperationConflict()
            return self._settle_team_credits(
                clean_user, team_id, clean_reference, actual_minor_units
            )

        reservation_id = hashlib.sha256(
            f"{TokenType.JURIS.value}:{clean_reference}".encode("utf-8")
        ).hexdigest()
        reservation_ref = self._ledger_collection(clean_user).document(reservation_id)
        settlement_id = hashlib.sha256(
            f"{clean_user}:{clean_reference}:settlement".encode("utf-8")
        ).hexdigest()
        settlement_ref = self._client.collection("_server_ai_usage_settlements").document(settlement_id)
        account_ref = self._balance_ref(clean_user, TokenType.JURIS)
        transaction = self._client.transaction(max_attempts=5)

        @firestore.transactional
        def settle_in_transaction(txn: firestore.Transaction) -> float:
            prior_settlement = settlement_ref.get(transaction=txn)
            if prior_settlement.exists:
                prior_data = prior_settlement.to_dict() or {}
                if prior_data.get("actualMinorUnits") != actual_minor_units:
                    raise TokenOperationConflict()
                return float(prior_data.get("balanceAfter", 0.0))

            reservation_snapshot = reservation_ref.get(transaction=txn)
            if not reservation_snapshot.exists:
                raise TokenOperationConflict()
            reservation_data = reservation_snapshot.to_dict() or {}
            reservation = self._ledger_entry(reservation_data)
            if (
                reservation.user_id != clean_user
                or reservation.token_type is not TokenType.JURIS
                or reservation.direction != "debit"
                or reservation.reference_id != clean_reference
            ):
                raise TokenOperationConflict()

            account_snapshot = account_ref.get(transaction=txn)
            account_data = account_snapshot.to_dict() or {}
            current_units = self._stored_balance_units(account_data, TokenType.JURIS)
            reserved_minor_units = reservation_data.get("amountMinorUnits")
            if not isinstance(reserved_minor_units, int) or isinstance(reserved_minor_units, bool):
                reserved_minor_units = round(reservation.amount * 100)
            delta = reserved_minor_units - actual_minor_units
            balance_after_units = current_units + delta
            if balance_after_units < 0:
                raise InsufficientTokens(-delta / 100, current_units / 100)

            created_at = datetime.now(timezone.utc)
            if delta:
                txn.set(
                    account_ref,
                    self._account_balance_fields(balance_after_units, TokenType.JURIS, created_at),
                    merge=True,
                )

            if actual_minor_units == 0:
                # Failed provider calls are fully released and do not appear as a charge.
                txn.delete(reservation_ref)
            else:
                settled_entry = TokenLedgerEntry(
                    transaction_id=reservation.transaction_id,
                    user_id=clean_user,
                    actor_user_id=clean_user,
                    token_type=TokenType.JURIS,
                    amount=actual_minor_units / 100,
                    direction="debit",
                    reason="JURIS AI usage",
                    reference_id=clean_reference,
                    created_at=reservation.created_at,
                    balance_after=balance_after_units / 100,
                )
                txn.set(reservation_ref, self._ledger_document(settled_entry))

            txn.create(settlement_ref, {
                "userId": clean_user,
                "reservationReference": clean_reference,
                "reservedMinorUnits": reserved_minor_units,
                "actualMinorUnits": actual_minor_units,
                "balanceAfter": balance_after_units / 100,
                "balanceAfterMinorUnits": balance_after_units,
                "createdAt": created_at,
            })
            return balance_after_units / 100

        return settle_in_transaction(transaction)

    def consume_tokens(
        self,
        user_id: str,
        token_type: TokenType,
        amount: int,
        reason: str,
        reference_id: str,
    ) -> TokenMutationResult:
        transaction_id = hashlib.sha256(
            f"{token_type.value}:{reference_id}".encode("utf-8")
        ).hexdigest()
        account_ref = self._balance_ref(user_id, token_type)
        ledger_ref = self._ledger_collection(user_id).document(transaction_id)
        amount_units = self._amount_in_storage_units(amount, token_type)
        transaction = self._client.transaction(max_attempts=5)

        @firestore.transactional
        def debit_in_transaction(txn: firestore.Transaction) -> TokenMutationResult:
            prior_snapshot = ledger_ref.get(transaction=txn)
            if prior_snapshot.exists:
                prior = self._ledger_entry(prior_snapshot.to_dict() or {})
                if (
                    prior.user_id != user_id
                    or prior.token_type is not token_type
                    or prior.amount != amount
                    or prior.direction != "debit"
                    or prior.actor_user_id != user_id
                    or prior.reason != reason
                    or prior.reference_id != reference_id
                ):
                    raise TokenOperationConflict()
                return TokenMutationResult(entry=prior, duplicate=True)

            account_snapshot = account_ref.get(transaction=txn)
            account_data = account_snapshot.to_dict() or {}
            available_units = self._stored_balance_units(account_data, token_type)
            available_tokens = self._balance_in_tokens(available_units, token_type)
            if amount_units > available_units:
                raise InsufficientTokens(amount, available_tokens)

            balance_after_units = available_units - amount_units
            balance_after = self._balance_in_tokens(balance_after_units, token_type)
            created_at = datetime.now(timezone.utc)
            entry = TokenLedgerEntry(
                transaction_id=transaction_id,
                user_id=user_id,
                actor_user_id=user_id,
                token_type=token_type,
                amount=float(amount),
                direction="debit",
                reason=reason,
                reference_id=reference_id,
                created_at=created_at,
                balance_after=float(balance_after),
            )
            txn.set(
                account_ref,
                self._account_balance_fields(balance_after_units, token_type, created_at),
                merge=True,
            )
            txn.create(ledger_ref, self._ledger_document(entry))
            return TokenMutationResult(entry=entry)

        return debit_in_transaction(transaction)

    def grant_tokens(
        self,
        actor_user_id: str,
        target_user_id: str,
        token_type: TokenType,
        amount: int,
        reason: str,
        reference_id: str,
    ) -> TokenMutationResult:
        transaction_id = hashlib.sha256(
            f"{token_type.value}:{reference_id}".encode("utf-8")
        ).hexdigest()
        admin_ref = self._client.collection("_server_finance_admins").document(
            actor_user_id
        )
        account_ref = self._balance_ref(target_user_id, token_type)
        ledger_ref = self._ledger_collection(target_user_id).document(transaction_id)
        amount_units = self._amount_in_storage_units(amount, token_type)
        transaction = self._client.transaction(max_attempts=5)

        @firestore.transactional
        def grant_in_transaction(txn: firestore.Transaction) -> TokenMutationResult:
            admin_snapshot = admin_ref.get(transaction=txn)
            if not admin_snapshot.exists or not (admin_snapshot.to_dict() or {}).get(
                "enabled", False
            ):
                raise AuthorizationFailure("Token grant is not authorized.")

            prior_snapshot = ledger_ref.get(transaction=txn)
            if prior_snapshot.exists:
                prior = self._ledger_entry(prior_snapshot.to_dict() or {})
                if (
                    prior.user_id != target_user_id
                    or prior.actor_user_id != actor_user_id
                    or prior.token_type is not token_type
                    or prior.amount != amount
                    or prior.direction != "credit"
                    or prior.reason != reason
                    or prior.reference_id != reference_id
                ):
                    raise TokenOperationConflict()
                return TokenMutationResult(entry=prior, duplicate=True)

            account_snapshot = account_ref.get(transaction=txn)
            account_data = account_snapshot.to_dict() or {}
            current_units = self._stored_balance_units(account_data, token_type)
            balance_after_units = current_units + amount_units
            balance_after = self._balance_in_tokens(balance_after_units, token_type)
            created_at = datetime.now(timezone.utc)
            entry = TokenLedgerEntry(
                transaction_id=transaction_id,
                user_id=target_user_id,
                actor_user_id=actor_user_id,
                token_type=token_type,
                amount=float(amount),
                direction="credit",
                reason=reason,
                reference_id=reference_id,
                created_at=created_at,
                balance_after=float(balance_after),
            )
            txn.set(
                account_ref,
                self._account_balance_fields(balance_after_units, token_type, created_at),
                merge=True,
            )
            txn.create(ledger_ref, self._ledger_document(entry))
            return TokenMutationResult(entry=entry)

        return grant_in_transaction(transaction)

    def list_ledger(
        self,
        user_id: str,
        token_type: TokenType | None = None,
        limit: int = 100,
    ) -> list[TokenLedgerEntry]:
        query = self._ledger_collection(user_id)
        if token_type is None:
            snapshots = query.order_by(
                "createdAt", direction=firestore.Query.DESCENDING
            ).limit(limit).stream()
            return [
                self._ledger_entry(snapshot.to_dict() or {})
                for snapshot in snapshots
            ]

        entries = [
            self._ledger_entry(snapshot.to_dict() or {})
            for snapshot in query.where(
                "tokenType", "==", token_type.value
            ).stream()
        ]
        entries.sort(key=lambda entry: entry.created_at, reverse=True)
        return entries[:limit]

    def _balance_ref(self, user_id: str, token_type: TokenType):
        return (
            self._client.collection("_server_token_accounts")
            .document(user_id)
            .collection("balances")
            .document(token_type.value)
        )

    def _ledger_collection(self, user_id: str):
        return (
            self._client.collection("_server_token_accounts")
            .document(user_id)
            .collection("ledger")
        )

    @staticmethod
    def _ledger_document(entry: TokenLedgerEntry) -> dict[str, Any]:
        document: dict[str, Any] = {
            "transactionId": entry.transaction_id,
            "userId": entry.user_id,
            "actorUserId": entry.actor_user_id,
            "tokenType": entry.token_type.value,
            "amount": entry.amount,
            "direction": entry.direction,
            "reason": entry.reason,
            "referenceId": entry.reference_id,
            "createdAt": entry.created_at,
            "balanceAfter": entry.balance_after,
        }
        document["amountMinorUnits"] = round(entry.amount * 100)
        document["balanceAfterMinorUnits"] = round(entry.balance_after * 100)
        return document

    @staticmethod
    def _ledger_entry(data: dict[str, Any]) -> TokenLedgerEntry:
        token_type = TokenType(data["tokenType"])
        amount_minor = data.get("amountMinorUnits")
        balance_minor = data.get("balanceAfterMinorUnits")
        amount = (
            amount_minor / 100
            if isinstance(amount_minor, int) and not isinstance(amount_minor, bool)
            else data["amount"]
        )
        balance_after = (
            balance_minor / 100
            if isinstance(balance_minor, int) and not isinstance(balance_minor, bool)
            else data["balanceAfter"]
        )
        return TokenLedgerEntry(
            transaction_id=data["transactionId"],
            user_id=data["userId"],
            actor_user_id=data.get("actorUserId", data["userId"]),
            token_type=token_type,
            amount=float(amount),
            direction=data["direction"],
            reason=data["reason"],
            reference_id=data["referenceId"],
            created_at=data["createdAt"],
            balance_after=float(balance_after),
        )

@lru_cache(maxsize=1)
def get_finance_repository() -> FirestoreFinanceRepository:
    return FirestoreFinanceRepository()
