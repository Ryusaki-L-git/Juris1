from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from core.authorization import require_verified_user
from core.errors import InsufficientTokens, InvalidInputError
from core.identity import RequestIdentity


class TokenType(str, Enum):
    JURIS = "juris"
    CASE = "case"


class TokenBalance(BaseModel):
    model_config = ConfigDict(frozen=True)

    juris_tokens: int = 0
    case_tokens: int = 0

    def for_type(self, token_type: TokenType) -> int:
        return self.juris_tokens if token_type is TokenType.JURIS else self.case_tokens


class TokenLedgerEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    transaction_id: str
    user_id: str
    actor_user_id: str
    token_type: TokenType
    amount: int
    direction: str
    reason: str
    reference_id: str
    created_at: datetime
    balance_after: int


class TokenMutationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    entry: TokenLedgerEntry
    duplicate: bool = False


class TokenRepository(Protocol):
    def get_balance(self, user_id: str, token_type: TokenType) -> int: ...

    def consume_tokens(
        self,
        user_id: str,
        token_type: TokenType,
        amount: int,
        reason: str,
        reference_id: str,
    ) -> TokenMutationResult: ...

    def grant_tokens(
        self,
        actor_user_id: str,
        target_user_id: str,
        token_type: TokenType,
        amount: int,
        reason: str,
        reference_id: str,
    ) -> TokenMutationResult: ...

    def list_ledger(
        self,
        user_id: str,
        token_type: TokenType | None = None,
        limit: int = 100,
    ) -> list[TokenLedgerEntry]: ...


class TokenService:
    """Authenticated token reads and atomic debits for trusted backend workflows."""

    def __init__(self, repository: TokenRepository) -> None:
        self._repository = repository

    @staticmethod
    def _validate_amount(required_tokens: int, *, allow_zero: bool) -> None:
        if isinstance(required_tokens, bool) or not isinstance(required_tokens, int):
            raise InvalidInputError("Token amount must be an integer.")
        if required_tokens < 0 or (required_tokens == 0 and not allow_zero):
            raise InvalidInputError("Token amount must be positive.")

    @staticmethod
    def _validate_token_type(token_type: TokenType | str) -> TokenType:
        try:
            return TokenType(token_type)
        except (TypeError, ValueError):
            raise InvalidInputError("Unknown token type.") from None

    def get_balance(
        self, identity: RequestIdentity, token_type: TokenType | str
    ) -> int:
        user = require_verified_user(identity)
        kind = self._validate_token_type(token_type)
        balance = self._repository.get_balance(user.user_id, kind)
        if balance < 0:
            raise RuntimeError("Stored token balance is invalid.")
        return balance

    def get_balances(self, identity: RequestIdentity) -> TokenBalance:
        return TokenBalance(
            juris_tokens=self.get_balance(identity, TokenType.JURIS),
            case_tokens=self.get_balance(identity, TokenType.CASE),
        )

    def require_tokens(
        self,
        identity: RequestIdentity,
        token_type: TokenType | str,
        required_tokens: int,
    ) -> int:
        self._validate_amount(required_tokens, allow_zero=True)
        available = self.get_balance(identity, token_type)
        if required_tokens > available:
            raise InsufficientTokens(required_tokens, available)
        return available

    def consume_tokens(
        self,
        identity: RequestIdentity,
        token_type: TokenType | str,
        required_tokens: int,
        *,
        reason: str,
        reference_id: str,
    ) -> TokenMutationResult:
        self._validate_amount(required_tokens, allow_zero=False)
        kind = self._validate_token_type(token_type)
        clean_reason = reason.strip()
        clean_reference = reference_id.strip()
        if not clean_reason or not clean_reference:
            raise InvalidInputError("Token reason and operation reference are required.")
        user = require_verified_user(identity)
        return self._repository.consume_tokens(
            user.user_id,
            kind,
            required_tokens,
            clean_reason,
            clean_reference,
        )

    def grant_tokens(
        self,
        identity: RequestIdentity,
        target_user_id: str,
        token_type: TokenType | str,
        amount: int,
        *,
        reason: str,
        reference_id: str,
    ) -> TokenMutationResult:
        """Internal trusted-backend grant; deliberately not exposed as an API route."""
        self._validate_amount(amount, allow_zero=False)
        kind = self._validate_token_type(token_type)
        clean_reason = reason.strip()
        clean_reference = reference_id.strip()
        target_uid = target_user_id.strip()
        if not clean_reason or not clean_reference or not target_uid:
            raise InvalidInputError("Grant target, reason and reference are required.")
        actor = require_verified_user(identity)
        return self._repository.grant_tokens(
            actor.user_id,
            target_uid,
            kind,
            amount,
            clean_reason,
            clean_reference,
        )

    def get_ledger(
        self,
        identity: RequestIdentity,
        *,
        token_type: TokenType | str | None = None,
        limit: int = 100,
    ) -> list[TokenLedgerEntry]:
        user = require_verified_user(identity)
        if not 1 <= limit <= 500:
            raise InvalidInputError("Ledger page size must be between 1 and 500.")
        kind = None if token_type is None else self._validate_token_type(token_type)
        return self._repository.list_ledger(user.user_id, kind, limit)
