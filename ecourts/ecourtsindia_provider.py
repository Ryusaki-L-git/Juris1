"""
ecourts/ecourtsindia_provider.py — eCourtsIndia.com HTTP provider (DISABLED IN JURIS).

ARCHITECTURE BOUNDARY — THIS CLASS IS NOT USED BY JURIS IN PRODUCTION:
    JURIS is NOT permitted to access eCourts directly.
    JURIS does NOT hold an eCourts API key.
    JURIS does NOT import or use this class anywhere in the request path.

    The JURIS factory (ecourts/factory.py) returns LedDataECourtsProvider,
    which reads only eCourts-sourced data that LED has already stored in
    the user's Firestore case record.

    This file is preserved as a reference implementation for the LED-side
    authorized eCourts server integration (a separate service/Cloud Function).
    If the LED backend is ever extracted into its own Python service, this
    provider can be imported there (with its own credential, separate from JURIS).

    NEVER import get_ecourts_provider() in JURIS tool or monitoring code
    and expect to get this class — the factory returns LedDataECourtsProvider.

STATUS:
    The Government of India's ecourts.gov.in does NOT provide a public
    developer API. This provider uses eCourtsIndia.com, a commercial
    third-party service, as the data source.

READ-ONLY CONTRACT:
    This provider only issues GET requests.
    It NEVER writes, submits, or modifies eCourts data.

API REFERENCE (eCourtsIndia.com):
    CNR lookup: GET {base_url}/v1/cases/cnr/{cnr}
    Header: x-api-key: {api_key}
    Response: JSON with case details
"""


from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

# NOTE: This provider is NOT used by JURIS in production.
# It is preserved as a reference for the LED-side authorized eCourts integration.
# settings.ecourts_base_url and settings.ecourts_api_key do NOT exist in JURIS config.
from core.errors import (
    ProviderMalformedResponseError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from ecourts.base import ECourtsProvider, ECourtsSnapshot

logger = logging.getLogger("juris.ecourts.ecourtsindia")

_ECOURTS_TIMEOUT_SECONDS = 20.0
_PLACEHOLDER_KEY = "paste the"  # detect unconfigured keys


def _is_real_key(value: str | None) -> bool:
    if not value or not value.strip():
        return False
    clean = value.strip()
    if clean.lower().startswith(_PLACEHOLDER_KEY):
        return False
    return True


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None


def _list_of_str(value: Any) -> list[str]:
    """Safely coerce a value to list[str]."""
    if isinstance(value, list):
        return [str(v).strip() for v in value if v]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _parse_orders(raw: Any) -> list[dict[str, Any]]:
    """Parse the orders/history list from the API response."""
    if not isinstance(raw, list):
        return []
    result = []
    for item in raw:
        if isinstance(item, dict):
            result.append({
                "date": _str_or_none(item.get("date") or item.get("order_date") or item.get("hearing_date")),
                "detail": _str_or_none(item.get("detail") or item.get("business") or item.get("description")),
            })
    return result


def _build_snapshot(cnr: str, data: dict[str, Any]) -> ECourtsSnapshot:
    """
    Map the eCourtsIndia API response to our normalized ECourtsSnapshot.

    Field mapping (eCourtsIndia API → ECourtsSnapshot):
        cnr_number / cnr          → cnr (use our validated cnr)
        case_no / case_number     → case_number
        case_type                 → case_type
        filing_date               → filing_date
        registration_date         → registration_date
        case_status               → status
        stage_of_case / stage     → stage_of_case
        court_name                → court_name
        court_number_chamber      → court_number
        state_name / state        → state
        district_name / district  → district
        petitioner (str/list)     → petitioner_names
        respondent (str/list)     → respondent_names
        petitioner_advocate       → petitioner_advocate
        respondent_advocate       → respondent_advocate
        next_hearing_date         → next_hearing_date
        last_hearing_date         → last_hearing_date
        case_acts / acts          → acts
        orders / history          → orders
    """
    # Handle petitioner/respondent as string or list
    pet_raw = data.get("petitioner") or data.get("petitioners") or []
    res_raw = data.get("respondent") or data.get("respondents") or []

    return ECourtsSnapshot(
        cnr=cnr,
        case_number=_str_or_none(data.get("case_no") or data.get("case_number")),
        case_type=_str_or_none(data.get("case_type")),
        filing_date=_str_or_none(data.get("filing_date")),
        registration_date=_str_or_none(data.get("registration_date")),
        status=_str_or_none(data.get("case_status") or data.get("status")),
        stage_of_case=_str_or_none(data.get("stage_of_case") or data.get("stage")),
        court_name=_str_or_none(data.get("court_name")),
        court_number=_str_or_none(data.get("court_number_chamber") or data.get("court_number")),
        state=_str_or_none(data.get("state_name") or data.get("state")),
        district=_str_or_none(data.get("district_name") or data.get("district")),
        petitioner_names=_list_of_str(pet_raw),
        respondent_names=_list_of_str(res_raw),
        petitioner_advocate=_str_or_none(data.get("petitioner_advocate")),
        respondent_advocate=_str_or_none(data.get("respondent_advocate")),
        next_hearing_date=_str_or_none(data.get("next_hearing_date")),
        last_hearing_date=_str_or_none(data.get("last_hearing_date")),
        orders=_parse_orders(data.get("orders") or data.get("history") or []),
        acts=_list_of_str(data.get("case_acts") or data.get("acts") or []),
        retrieved_at=datetime.now(timezone.utc),
        provider="ecourtsindia",
    )


class ECourtsIndiaProvider(ECourtsProvider):
    """
    eCourtsIndia.com HTTP provider (read-only).

    Requires:
        JURIS_ECOURTS_BASE_URL = https://webapi.ecourtsindia.com (or configured base)
        JURIS_ECOURTS_API_KEY  = your eCourtsIndia API key

    Without these, all calls raise ProviderUnavailableError.
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self._base_url = base_url
        self._api_key = api_key

    @property
    def provider_name(self) -> str:
        return "ecourtsindia"

    @property
    def _resolved_base_url(self) -> str:
        # NOTE: settings.ecourts_base_url does not exist in JURIS config.
        # This provider can only be used with explicit constructor args
        # (e.g., from the LED-side authorized service, not from JURIS itself).
        return (self._base_url or "").rstrip("/")

    @property
    def _resolved_api_key(self) -> str:
        # NOTE: settings.ecourts_api_key does not exist in JURIS config.
        # This provider can only be used with explicit constructor args.
        return self._api_key or ""

    @property
    def is_configured(self) -> bool:
        return bool(self._resolved_base_url) and _is_real_key(self._resolved_api_key)

    async def get_case_by_cnr(self, cnr: str) -> ECourtsSnapshot:
        """
        Fetch case data for a normalized CNR. READ-ONLY: uses GET only.

        Raises:
            ProviderUnavailableError — not configured, auth failure, or 5xx
            ProviderTimeoutError     — request timed out
            ProviderRateLimitError   — HTTP 429
            ProviderMalformedResponseError — non-JSON or unexpected structure
        """
        if not self.is_configured:
            raise ProviderUnavailableError(
                "eCourts provider is not configured. "
                "Set JURIS_ECOURTS_BASE_URL and JURIS_ECOURTS_API_KEY in .env."
            )

        url = f"{self._resolved_base_url}/v1/cases/cnr/{cnr}"
        headers = {
            "x-api-key": self._resolved_api_key,
            "Accept": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=_ECOURTS_TIMEOUT_SECONDS) as client:
                # READ-ONLY: GET only
                response = await client.get(url, headers=headers)
        except httpx.TimeoutException as exc:
            logger.warning("eCourts CNR lookup timed out for %s: %s", cnr, exc)
            raise ProviderTimeoutError(
                f"eCourts lookup timed out after {_ECOURTS_TIMEOUT_SECONDS}s."
            ) from exc
        except httpx.HTTPError as exc:
            logger.error("eCourts HTTP error for %s: %s", cnr, exc)
            raise ProviderUnavailableError(
                f"eCourts provider connection error: {exc}"
            ) from exc

        status_code = response.status_code

        if status_code in (401, 403):
            logger.error("eCourts API authentication failed (HTTP %d) for CNR %s", status_code, cnr)
            raise ProviderUnavailableError(
                "eCourts API authentication failed. Check JURIS_ECOURTS_API_KEY."
            )

        if status_code == 404:
            # Case not found in eCourts system — return a snapshot indicating so
            logger.info("CNR %s not found in eCourts system (HTTP 404).", cnr)
            return ECourtsSnapshot(
                cnr=cnr,
                status="Case not found in eCourts system",
                retrieved_at=datetime.now(timezone.utc),
                provider=self.provider_name,
            )

        if status_code == 429:
            logger.warning("eCourts rate limit reached for CNR %s.", cnr)
            raise ProviderRateLimitError(
                "eCourts rate limit reached. Please wait before retrying."
            )

        if status_code >= 500:
            logger.error("eCourts server error (HTTP %d) for CNR %s.", status_code, cnr)
            raise ProviderUnavailableError(
                f"eCourts service returned server error (HTTP {status_code})."
            )

        if status_code != 200:
            raise ProviderUnavailableError(
                f"eCourts returned unexpected status HTTP {status_code}."
            )

        # Parse the response
        try:
            data = response.json()
        except Exception as exc:
            logger.error("eCourts malformed JSON for CNR %s: %s", cnr, exc)
            raise ProviderMalformedResponseError(
                "eCourts returned a non-JSON response."
            ) from exc

        if not isinstance(data, dict):
            raise ProviderMalformedResponseError(
                "eCourts response was not a JSON object."
            )

        # The API may wrap data under a 'data' key
        case_data = data.get("data") or data
        if not isinstance(case_data, dict):
            raise ProviderMalformedResponseError(
                "eCourts response did not contain expected case data."
            )

        try:
            snapshot = _build_snapshot(cnr, case_data)
        except Exception as exc:
            logger.error("Failed to parse eCourts response for CNR %s: %s", cnr, exc)
            raise ProviderMalformedResponseError(
                "eCourts response could not be parsed into a case snapshot."
            ) from exc

        logger.info("eCourts CNR lookup successful: %s (status=%s)", cnr, snapshot.status)
        return snapshot
