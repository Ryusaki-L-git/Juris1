"""
core/cnr.py — Case Number Record (CNR) validation.

A CNR is the eCourts unique case identifier (e.g. "MHCC010012342024").
It is validated here, in one place, so every entry point (tools, API
routes, monitors) rejects malformed identifiers identically.

JURIS never guesses or repairs a CNR: an invalid identifier is an error,
not something to "fix".
"""

from __future__ import annotations

import re

from core.errors import InvalidCnrError

# eCourts CNRs are 16 characters, letters and digits, no separators.
_CNR_PATTERN = re.compile(r"^[A-Z0-9]{16}$")


def normalize_cnr(value: str | None) -> str:
    """
    Return the canonical CNR, or raise InvalidCnrError.

    Whitespace is stripped and case upper-cased because those are
    presentation differences rather than different identifiers.
    """
    cleaned = (value or "").strip().upper()
    if not cleaned:
        raise InvalidCnrError("A CNR number is required.")
    if not _CNR_PATTERN.match(cleaned):
        raise InvalidCnrError(
            "That CNR number is not valid. A CNR is 16 letters/digits, "
            "for example MHCC010012342024."
        )
    return cleaned


def is_valid_cnr(value: str | None) -> bool:
    """Non-raising validity check for storage/backfill paths."""
    try:
        normalize_cnr(value)
    except InvalidCnrError:
        return False
    return True
