from __future__ import annotations

from datetime import datetime, timezone

from core.finance_store import FirestoreFinanceRepository
from core.tokens import TokenBalance, TokenLedgerEntry, TokenType


def test_legacy_case_balance_is_interpreted_as_whole_tokens() -> None:
    units = FirestoreFinanceRepository._stored_balance_units(
        {"balance": 199}, TokenType.CASE
    )
    assert units == 19900
    assert FirestoreFinanceRepository._balance_in_tokens(units, TokenType.CASE) == 199.0


def test_fractional_case_balance_uses_canonical_hundredths() -> None:
    units = FirestoreFinanceRepository._stored_balance_units(
        {"balance": 199.5, "balanceMinorUnits": 19950, "balanceUnit": "hundredths"},
        TokenType.CASE,
    )
    assert units == 19950
    assert FirestoreFinanceRepository._balance_in_tokens(units, TokenType.CASE) == 199.5


def test_case_ledger_minor_units_round_trip_as_fractional_tokens() -> None:
    now = datetime.now(timezone.utc)
    entry = FirestoreFinanceRepository._ledger_entry({
        "transactionId": "case:refresh-1",
        "userId": "user-a",
        "actorUserId": "user-a",
        "tokenType": "case",
        "amount": 0.5,
        "amountMinorUnits": 50,
        "direction": "debit",
        "reason": "Smart Refresh",
        "referenceId": "refresh-1",
        "createdAt": now,
        "balanceAfter": 199.5,
        "balanceAfterMinorUnits": 19950,
    })

    assert entry.amount == 0.5
    assert entry.balance_after == 199.5
    assert entry.token_type is TokenType.CASE


def test_public_balance_model_preserves_separate_credits_and_case_tokens() -> None:
    balance = TokenBalance(credits=4.25, juris_tokens=4.25, case_tokens=199.5)
    assert balance.model_dump() == {
        "credits": 4.25,
        "juris_tokens": 4.25,
        "case_tokens": 199.5,
    }


def test_legacy_whole_juris_balance_is_normalized_to_hundredths() -> None:
    units = FirestoreFinanceRepository._stored_balance_units(
        {"balance": 8}, TokenType.JURIS
    )
    assert units == 800
    assert FirestoreFinanceRepository._balance_in_tokens(units, TokenType.JURIS) == 8.0


def test_fractional_juris_credit_balance_uses_canonical_hundredths() -> None:
    units = FirestoreFinanceRepository._stored_balance_units(
        {"balance": 1.23, "balanceMinorUnits": 123, "balanceUnit": "hundredths"},
        TokenType.JURIS,
    )
    assert units == 123
    assert FirestoreFinanceRepository._balance_in_tokens(units, TokenType.JURIS) == 1.23


def test_juris_credit_ledger_minor_units_round_trip() -> None:
    now = datetime.now(timezone.utc)
    entry = FirestoreFinanceRepository._ledger_entry({
        "transactionId": "juris:ai-call-1",
        "userId": "user-a",
        "actorUserId": "user-a",
        "tokenType": "juris",
        "amount": 0.42,
        "amountMinorUnits": 42,
        "direction": "debit",
        "reason": "JURIS AI usage",
        "referenceId": "ai-call-1",
        "createdAt": now,
        "balanceAfter": 3.58,
        "balanceAfterMinorUnits": 358,
    })

    assert entry.amount == 0.42
    assert entry.balance_after == 3.58
    assert entry.token_type is TokenType.JURIS
