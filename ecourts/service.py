"""
ecourts/service.py — JURIS eCourts read-only service (LED-data layer).

ARCHITECTURE:
    JURIS does NOT hold an eCourts API key.
    JURIS does NOT call eCourts directly.

    This service wraps the provider, which reads only data that LED has
    already authorized and stored in the user's Firestore case record.

    Error message in ProviderUnavailableError now correctly refers to
    LED's eCourts integration, not to JURIS credentials.

WRITE PROHIBITION: This service has NO write methods.
"""

from __future__ import annotations

from ecourts.base import ECourtsProvider, ECourtsSnapshot
from core.cnr import normalize_cnr
from core.errors import ProviderUnavailableError


class ECourtsService:
    """
    Read-only eCourts data service (via LED Firestore data).

    Takes an ECourtsProvider and adds CNR normalization + availability check.
    The provider is LedDataECourtsProvider — reads Firestore, NOT eCourts API.
    """

    def __init__(self, provider: ECourtsProvider) -> None:
        self._provider = provider

    async def get_case(self, cnr_raw: str) -> ECourtsSnapshot:
        """
        Normalize CNR and retrieve the LED-stored eCourts snapshot.

        Args:
            cnr_raw: Raw CNR string from user input (may have whitespace/case).

        Returns:
            ECourtsSnapshot — built from data LED has already stored.
            May contain a status message explaining data is not yet available
            if LED has not yet synced this case from eCourts.

        Raises:
            InvalidCnrError         — if CNR format is invalid
            ProviderUnavailableError — if Firestore is unreachable
        """
        # 1. Normalize first — raises InvalidCnrError on bad format
        cnr = normalize_cnr(cnr_raw)

        # 2. LedDataECourtsProvider.is_configured always True if Firestore accessible
        if not self._provider.is_configured:
            raise ProviderUnavailableError(
                "Cannot access LED case data right now. "
                "Please try again in a moment."
            )

        # 3. Call provider (read-only — reads Firestore, NOT eCourts directly)
        return await self._provider.get_case_by_cnr(cnr)
