from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from api.deps import get_request_identity
from core.entitlements import EntitlementService, EntitlementSnapshot
from core.finance_store import FirestoreFinanceRepository, get_finance_repository
from core.identity import RequestIdentity
from core.tokens import TokenBalance, TokenService, TokenType

router = APIRouter(prefix="/v1", tags=["entitlements-and-tokens"])


FinanceRepositoryDependency = Annotated[
    FirestoreFinanceRepository,
    Depends(get_finance_repository),
]
IdentityDependency = Annotated[RequestIdentity, Depends(get_request_identity)]


@router.get("/entitlements/me", response_model=EntitlementSnapshot)
def get_my_entitlement(
    identity: IdentityDependency,
    repository: FinanceRepositoryDependency,
) -> EntitlementSnapshot:
    return EntitlementService(repository).get_current(identity)


@router.get("/tokens/me", response_model=TokenBalance)
def get_my_token_balances(
    identity: IdentityDependency,
    repository: FinanceRepositoryDependency,
) -> TokenBalance:
    return TokenService(repository).get_balances(identity)


@router.get("/tokens/me/ledger")
def get_my_token_ledger(
    identity: IdentityDependency,
    repository: FinanceRepositoryDependency,
    token_type: TokenType | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
):
    return TokenService(repository).get_ledger(
        identity,
        token_type=token_type,
        limit=limit,
    )
