from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from core.entitlements import (
    EntitlementService,
    EntitlementSnapshot,
    EntitlementStatus,
    Feature,
    PlanId,
)
from core.errors import (
    AuthorizationFailure,
    AuthenticationFailure,
    InsufficientTokens,
    SubscriptionNotEligible,
    TokenOperationConflict,
)
from core.identity import JurisUser, RequestIdentity
from core.tokens import (
    TokenLedgerEntry,
    TokenMutationResult,
    TokenService,
    TokenType,
)


class FakeFinanceRepository:
    def __init__(self) -> None:
        self.entitlements: dict[str, dict] = {}
        self.balances: dict[tuple[str, TokenType], int] = {}
        self.ledger: dict[tuple[str, TokenType, str], TokenLedgerEntry] = {}
        self.finance_admins: set[str] = set()
        self.entitlement_reads: list[str] = []
        self._lock = threading.Lock()

    def get_entitlement(self, user_id: str) -> dict | None:
        self.entitlement_reads.append(user_id)
        return self.entitlements.get(user_id)

    def get_balance(self, user_id: str, token_type: TokenType) -> int:
        with self._lock:
            return self.balances.get((user_id, token_type), 0)

    def consume_tokens(
        self,
        user_id: str,
        token_type: TokenType,
        amount: int,
        reason: str,
        reference_id: str,
    ) -> TokenMutationResult:
        key = (user_id, token_type, reference_id)
        with self._lock:
            prior = self.ledger.get(key)
            if prior is not None:
                if (
                    prior.amount != amount
                    or prior.reason != reason
                    or prior.direction != "debit"
                    or prior.actor_user_id != user_id
                ):
                    raise TokenOperationConflict()
                return TokenMutationResult(entry=prior, duplicate=True)

            balance_key = (user_id, token_type)
            available = self.balances.get(balance_key, 0)
            if amount > available:
                raise InsufficientTokens(amount, available)

            remaining = available - amount
            self.balances[balance_key] = remaining
            entry = TokenLedgerEntry(
                transaction_id=f"{token_type.value}:{reference_id}",
                user_id=user_id,
                actor_user_id=user_id,
                token_type=token_type,
                amount=amount,
                direction="debit",
                reason=reason,
                reference_id=reference_id,
                created_at=datetime.now(timezone.utc),
                balance_after=remaining,
            )
            self.ledger[key] = entry
            return TokenMutationResult(entry=entry)

    def grant_tokens(
        self,
        actor_user_id: str,
        target_user_id: str,
        token_type: TokenType,
        amount: int,
        reason: str,
        reference_id: str,
    ) -> TokenMutationResult:
        key = (target_user_id, token_type, reference_id)
        with self._lock:
            if actor_user_id not in self.finance_admins:
                raise AuthorizationFailure("Token grant is not authorized.")
            prior = self.ledger.get(key)
            if prior is not None:
                if (
                    prior.actor_user_id != actor_user_id
                    or prior.amount != amount
                    or prior.reason != reason
                    or prior.direction != "credit"
                ):
                    raise TokenOperationConflict()
                return TokenMutationResult(entry=prior, duplicate=True)

            balance_key = (target_user_id, token_type)
            balance = self.balances.get(balance_key, 0)
            balance_after = balance + amount
            self.balances[balance_key] = balance_after
            entry = TokenLedgerEntry(
                transaction_id=f"{token_type.value}:{reference_id}",
                user_id=target_user_id,
                actor_user_id=actor_user_id,
                token_type=token_type,
                amount=amount,
                direction="credit",
                reason=reason,
                reference_id=reference_id,
                created_at=datetime.now(timezone.utc),
                balance_after=balance_after,
            )
            self.ledger[key] = entry
            return TokenMutationResult(entry=entry)

    def list_ledger(
        self,
        user_id: str,
        token_type: TokenType | None = None,
        limit: int = 100,
    ) -> list[TokenLedgerEntry]:
        with self._lock:
            entries = [
                entry
                for (entry_user, entry_type, _), entry in self.ledger.items()
                if entry_user == user_id
                and (token_type is None or entry_type is token_type)
            ]
        return sorted(entries, key=lambda entry: entry.created_at, reverse=True)[:limit]


class EntitlementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = FakeFinanceRepository()
        self.service = EntitlementService(self.repository)
        self.user_a = RequestIdentity(
            user=JurisUser(user_id="user-a", is_verified=True), session_id="session-a"
        )
        self.user_b = RequestIdentity(
            user=JurisUser(user_id="user-b", is_verified=True), session_id="session-b"
        )

    def test_free_user_is_denied_paid_feature(self) -> None:
        self.assertFalse(self.service.has_entitlement(self.user_a, Feature.JURIS_ACCESS))
        with self.assertRaises(SubscriptionNotEligible):
            self.service.require_entitlement(self.user_a, Feature.JURIS_ACCESS)

    def test_entitled_user_is_allowed(self) -> None:
        self.repository.entitlements["user-a"] = {
            "planId": PlanId.PREMIUM_CLOUD.value,
            "status": EntitlementStatus.ACTIVE.value,
            "enabledFeatures": [Feature.JURIS_ACCESS.value],
        }
        self.assertTrue(self.service.has_entitlement(self.user_a, Feature.JURIS_ACCESS))
        self.assertEqual(
            self.service.require_entitlement(self.user_a, Feature.JURIS_ACCESS).plan_id,
            PlanId.PREMIUM_CLOUD,
        )

    def test_disabled_unknown_or_invalid_entitlement_is_denied(self) -> None:
        self.repository.entitlements["user-a"] = {
            "planId": PlanId.PREMIUM_CLOUD.value,
            "status": EntitlementStatus.ACTIVE.value,
            "enabledFeatures": [],
        }
        self.assertFalse(self.service.has_entitlement(self.user_a, Feature.JURIS_ACCESS))
        self.assertFalse(self.service.has_entitlement(self.user_a, "unknown_feature"))
        self.repository.entitlements["user-a"] = {
            "planId": "unrecognized_plan",
            "status": "active",
            "enabledFeatures": [Feature.JURIS_ACCESS.value],
        }
        self.assertFalse(self.service.has_entitlement(self.user_a, Feature.JURIS_ACCESS))

    def test_entitlement_uses_authenticated_identity_and_rejects_unverified_user(self) -> None:
        self.repository.entitlements["user-a"] = {
            "planId": PlanId.PREMIUM_CLOUD.value,
            "status": EntitlementStatus.ACTIVE.value,
            "enabledFeatures": [Feature.JURIS_ACCESS.value],
        }
        self.service.get_current(self.user_a)
        self.assertEqual(self.repository.entitlement_reads, ["user-a"])

        unverified = RequestIdentity(
            user=JurisUser(user_id="user-b", is_verified=False), session_id=""
        )
        with self.assertRaises(AuthenticationFailure):
            self.service.get_current(unverified)
        self.assertEqual(self.repository.entitlement_reads, ["user-a"])

    def test_user_a_cannot_read_user_b_entitlement(self) -> None:
        self.repository.entitlements["user-b"] = {
            "planId": PlanId.CHAMBER_ENTERPRISE.value,
            "status": EntitlementStatus.ACTIVE.value,
            "enabledFeatures": [Feature.TEAM_COLLABORATION.value],
        }
        result = self.service.get_current(self.user_a)
        self.assertEqual(result.plan_id, PlanId.FREE)
        self.assertEqual(self.repository.entitlement_reads, ["user-a"])

    def test_effective_timestamps_and_status_gate_features(self) -> None:
        future = datetime(2030, 1, 1, tzinfo=timezone.utc)
        snapshot = EntitlementSnapshot(
            plan_id=PlanId.PREMIUM_CLOUD,
            status=EntitlementStatus.ACTIVE,
            enabled_features=frozenset({Feature.CLOUD_SYNC}),
            effective_at=future,
        )
        self.assertFalse(snapshot.allows(Feature.CLOUD_SYNC))
        expired_status = snapshot.model_copy(update={"status": EntitlementStatus.EXPIRED})
        self.assertFalse(expired_status.allows(Feature.CLOUD_SYNC))


class TokenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = FakeFinanceRepository()
        self.service = TokenService(self.repository)
        self.user_a = RequestIdentity(
            user=JurisUser(user_id="user-a", is_verified=True), session_id="session-a"
        )
        self.user_b = RequestIdentity(
            user=JurisUser(user_id="user-b", is_verified=True), session_id="session-b"
        )

    def test_token_types_are_separate_and_never_expire(self) -> None:
        self.repository.balances[("user-a", TokenType.JURIS)] = 8
        self.repository.balances[("user-a", TokenType.CASE)] = 3
        balances = self.service.get_balances(self.user_a)
        self.assertEqual((balances.juris_tokens, balances.case_tokens), (8, 3))

        result = self.service.consume_tokens(
            self.user_a,
            TokenType.JURIS,
            1,
            reason="legal_research",
            reference_id="operation-1",
        )
        self.assertEqual(result.entry.direction, "debit")
        self.assertFalse(hasattr(result.entry, "expires_at"))

    def test_exact_balance_is_allowed_and_debit_is_auditable(self) -> None:
        self.repository.balances[("user-a", TokenType.CASE)] = 5
        self.assertEqual(self.service.require_tokens(self.user_a, TokenType.CASE, 5), 5)
        result = self.service.consume_tokens(
            self.user_a,
            TokenType.CASE,
            5,
            reason="case_import",
            reference_id="case-op-1",
        )
        self.assertEqual(self.service.get_balance(self.user_a, TokenType.CASE), 0)
        self.assertEqual(result.entry.balance_after, 0)
        self.assertEqual(result.entry.reference_id, "case-op-1")
        self.assertEqual(self.service.get_ledger(self.user_a)[0], result.entry)

    def test_insufficient_or_failed_charge_does_not_corrupt_balance(self) -> None:
        self.repository.balances[("user-a", TokenType.JURIS)] = 2
        with self.assertRaises(InsufficientTokens):
            self.service.consume_tokens(
                self.user_a,
                TokenType.JURIS,
                3,
                reason="draft",
                reference_id="too-expensive",
            )
        self.assertEqual(self.service.get_balance(self.user_a, TokenType.JURIS), 2)
        self.assertEqual(self.service.get_ledger(self.user_a), [])

    def test_repeated_reference_is_idempotent_and_mismatched_replay_rejected(self) -> None:
        self.repository.balances[("user-a", TokenType.JURIS)] = 10
        first = self.service.consume_tokens(
            self.user_a,
            TokenType.JURIS,
            4,
            reason="draft",
            reference_id="same-op",
        )
        replay = self.service.consume_tokens(
            self.user_a,
            TokenType.JURIS,
            4,
            reason="draft",
            reference_id="same-op",
        )
        self.assertFalse(first.duplicate)
        self.assertTrue(replay.duplicate)
        self.assertEqual(self.service.get_balance(self.user_a, TokenType.JURIS), 6)
        self.assertEqual(len(self.service.get_ledger(self.user_a)), 1)
        with self.assertRaises(TokenOperationConflict):
            self.service.consume_tokens(
                self.user_a,
                TokenType.JURIS,
                3,
                reason="draft",
                reference_id="same-op",
            )

    def test_concurrent_charges_cannot_overspend(self) -> None:
        self.repository.balances[("user-a", TokenType.CASE)] = 5

        def charge(index: int) -> bool:
            try:
                self.service.consume_tokens(
                    self.user_a,
                    TokenType.CASE,
                    1,
                    reason="concurrent_operation",
                    reference_id=f"op-{index}",
                )
                return True
            except InsufficientTokens:
                return False

        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(charge, range(10)))
        self.assertEqual(sum(results), 5)
        self.assertEqual(self.service.get_balance(self.user_a, TokenType.CASE), 0)
        self.assertEqual(len(self.service.get_ledger(self.user_a)), 5)

    def test_user_a_cannot_change_or_read_user_b_balance(self) -> None:
        self.repository.balances[("user-a", TokenType.JURIS)] = 4
        self.repository.balances[("user-b", TokenType.JURIS)] = 9
        result = self.service.consume_tokens(
            self.user_a,
            TokenType.JURIS,
            2,
            reason="authorized_operation",
            reference_id="user-a-op",
        )
        self.assertEqual(result.entry.user_id, "user-a")
        self.assertEqual(self.service.get_balance(self.user_b, TokenType.JURIS), 9)
        self.assertEqual(self.service.get_ledger(self.user_a)[0].user_id, "user-a")
        self.assertEqual(self.service.get_ledger(self.user_b), [])

    def test_admin_grant_is_auditable_and_idempotent(self) -> None:
        self.repository.finance_admins.add("user-a")
        first = self.service.grant_tokens(
            self.user_a,
            "user-b",
            TokenType.CASE,
            6,
            reason="manual_allowance",
            reference_id="grant-op-1",
        )
        repeated = self.service.grant_tokens(
            self.user_a,
            "user-b",
            TokenType.CASE,
            6,
            reason="manual_allowance",
            reference_id="grant-op-1",
        )
        self.assertEqual(first.entry.direction, "credit")
        self.assertEqual(first.entry.actor_user_id, "user-a")
        self.assertEqual(first.entry.user_id, "user-b")
        self.assertTrue(repeated.duplicate)
        self.assertEqual(self.service.get_balance(self.user_b, TokenType.CASE), 6)

        self.repository.finance_admins.clear()
        with self.assertRaises(AuthorizationFailure):
            self.service.grant_tokens(
                self.user_a,
                "user-b",
                TokenType.CASE,
                4,
                reason="manual_allowance",
                reference_id="grant-op-2",
            )
        self.assertEqual(self.service.get_balance(self.user_b, TokenType.CASE), 6)

    def test_credit_reference_cannot_be_replayed_as_a_debit(self) -> None:
        self.repository.finance_admins.add("user-a")
        self.service.grant_tokens(
            self.user_a,
            "user-a",
            TokenType.JURIS,
            5,
            reason="manual_allowance",
            reference_id="shared-reference",
        )
        with self.assertRaises(TokenOperationConflict):
            self.service.consume_tokens(
                self.user_a,
                TokenType.JURIS,
                5,
                reason="manual_allowance",
                reference_id="shared-reference",
            )
        self.assertEqual(self.service.get_balance(self.user_a, TokenType.JURIS), 5)

    def test_unverified_caller_cannot_consume_tokens(self) -> None:
        self.repository.balances[("user-a", TokenType.JURIS)] = 4
        unverified = RequestIdentity(
            user=JurisUser(user_id="user-a", is_verified=False), session_id=""
        )
        with self.assertRaises(AuthenticationFailure):
            self.service.consume_tokens(
                unverified,
                TokenType.JURIS,
                1,
                reason="test",
                reference_id="unverified-op",
            )
        self.assertEqual(self.service.get_balance(self.user_a, TokenType.JURIS), 4)


if __name__ == "__main__":
    unittest.main()
