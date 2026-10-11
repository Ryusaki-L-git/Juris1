"""
core/pricing.py — CENTRAL Case Token cost registry.

WHY THIS FILE EXISTS
    Token costs must not be scattered as magic numbers through the codebase,
    and JURIS must not invent a business price. Every Case Token cost is
    declared exactly once, here, and read from configuration.

UNRESOLVED COSTS
    The repository does not define the commercial price for eCourts
    operations. A configured value of 0 therefore means "cost not yet
    agreed", and `is_resolved` is False. When unresolved, JURIS skips
    charging entirely and reports the cost as unresolved through the API,
    so the product decision is visible instead of silently guessed.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict

from core.config import settings
from core.errors import InvalidInputError


class CaseTokenOperation(str, Enum):
    """Billable Case Token operations."""

    ECOURTS_LOOKUP = "ecourts_lookup"
    ECOURTS_REFRESH = "ecourts_refresh"
    MONITOR_CREATE = "monitor_create"


class TokenCost(BaseModel):
    model_config = ConfigDict(frozen=True)

    operation: CaseTokenOperation
    tokens: int
    is_resolved: bool
    note: str = ""


_UNRESOLVED_NOTE = (
    "Cost not yet defined in the product specification; charging is skipped."
)


def _configured_costs() -> dict[CaseTokenOperation, int]:
    return {
        CaseTokenOperation.ECOURTS_LOOKUP: settings.case_token_cost_ecourts_lookup,
        CaseTokenOperation.ECOURTS_REFRESH: settings.case_token_cost_ecourts_refresh,
        CaseTokenOperation.MONITOR_CREATE: settings.case_token_cost_monitor_create,
    }


def case_token_cost(operation: CaseTokenOperation | str) -> TokenCost:
    """Return the declared cost for a Case Token operation."""
    try:
        kind = CaseTokenOperation(operation)
    except (TypeError, ValueError):
        raise InvalidInputError("Unknown Case Token operation.") from None

    tokens = _configured_costs()[kind]
    if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
        raise InvalidInputError("Configured Case Token cost is invalid.")
    resolved = tokens > 0
    return TokenCost(
        operation=kind,
        tokens=tokens,
        is_resolved=resolved,
        note="" if resolved else _UNRESOLVED_NOTE,
    )


def all_case_token_costs() -> list[TokenCost]:
    """Every declared cost, for the LED pricing surface."""
    return [case_token_cost(operation) for operation in CaseTokenOperation]
