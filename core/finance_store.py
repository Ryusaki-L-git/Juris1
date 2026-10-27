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
