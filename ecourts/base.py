"""
ecourts/base.py — eCourts provider abstraction contract.

JURIS depends only on this interface. No tool, orchestrator, or
scheduler imports a provider-specific class directly.

CRITICAL BOUNDARY:
    eCourts data is READ-ONLY. JURIS must never attempt to write,
    file, or modify anything in eCourts. This is enforced by:
    1. This interface has no write methods.
    2. HTTP clients in providers use GET only.
    3. The orchestrator enforces is_ecourts=True read-only at tool level.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ECourtsSnapshot(BaseModel):
    """
    Normalized, provider-agnostic representation of eCourts case data.

    Fields are Optional because different providers may not return
    all fields, and JURIS must not invent missing values.

    retrieved_at and provider are excluded from fingerprinting (see
    monitoring/fingerprint.py) — they are metadata, not case state.
    """

    model_config = ConfigDict(frozen=True)

    cnr: str
    case_number: str | None = None
    case_type: str | None = None
    filing_date: str | None = None
    registration_date: str | None = None
    status: str | None = None
    stage_of_case: str | None = None
    court_name: str | None = None
    court_number: str | None = None
    state: str | None = None
    district: str | None = None
    petitioner_names: list[str] = Field(default_factory=list)
    respondent_names: list[str] = Field(default_factory=list)
    petitioner_advocate: str | None = None
    respondent_advocate: str | None = None
    next_hearing_date: str | None = None   # ISO date string or display string
    last_hearing_date: str | None = None
    orders: list[dict[str, Any]] = Field(default_factory=list)  # [{date, detail}]
    acts: list[str] = Field(default_factory=list)  # applicable acts/sections

    # Metadata — excluded from fingerprinting
    retrieved_at: datetime    # UTC timestamp of when we fetched this
    provider: str             # provider_name string


class ECourtsProvider(ABC):
    """
    Abstract base class for all eCourts provider implementations.

    JURIS tools and monitoring code depend only on this interface.
    Swapping providers = implementing a new subclass + updating factory.

    READ-ONLY CONTRACT:
        Implementations must NEVER attempt to write, submit, or
        modify any data in an eCourts system.
    """

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Human-readable identifier for this provider, e.g. 'ecourtsindia'."""
        ...

    @property
    @abstractmethod
    def is_configured(self) -> bool:
        """
        Return True if this provider has the credentials/config it needs.
        A provider that returns False must raise ProviderUnavailableError
        in get_case_by_cnr rather than making a network call.
        """
        ...

    @abstractmethod
    async def get_case_by_cnr(self, cnr: str) -> ECourtsSnapshot:
        """
        Fetch case data for a normalized CNR number.

        Args:
            cnr: A normalized (uppercase, no whitespace) CNR.

        Returns:
            ECourtsSnapshot with whatever fields the provider returns.
            Fields not available from the provider must be left None.

        Raises:
            ProviderUnavailableError — provider unreachable or not configured
            ProviderTimeoutError     — request timed out
            ProviderRateLimitError   — rate limit hit (caller should back off)
            ProviderMalformedResponseError — provider returned unexpected format

        READ-ONLY: This method must never modify eCourts data.
        """
        ...
