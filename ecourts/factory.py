"""
ecourts/factory.py — eCourts provider factory.

ARCHITECTURE:
    JURIS does NOT hold an eCourts API key.
    JURIS does NOT call eCourts directly.

    The factory returns the LedDataECourtsProvider, which reads only
    eCourts-sourced data already stored in the user's LED Firestore case record
    by LED's own authorized server-side integration.

    ECourtsIndiaProvider (which calls eCourts directly) still exists as
    dead code for the LED server integration layer (in a future split), but
    is NOT used by JURIS in production.
"""

from __future__ import annotations

from functools import lru_cache

from ecourts.base import ECourtsProvider


@lru_cache(maxsize=1)
def get_ecourts_provider() -> ECourtsProvider:
    """
    Return the singleton eCourts provider for JURIS.

    Returns LedDataECourtsProvider — reads from Firestore, NOT from eCourts API.

    JURIS cannot call eCourts directly.
    JURIS does not have an eCourts API credential.
    """
    from ecourts.led_data_provider import LedDataECourtsProvider
    return LedDataECourtsProvider()
