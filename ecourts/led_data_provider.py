"""
ecourts/led_data_provider.py — JURIS eCourts read via LED Firestore data only.

ARCHITECTURE BOUNDARY:
    JURIS is NOT permitted to call eCourts directly.
    JURIS does NOT hold an eCourts API key.

    The correct data flow is:
        eCourtsIndia
            ↓
        LED authorized server-side integration (holds the credential)
            ↓
        Firestore (stored in user's case record fields)
            ↓
        JURIS reads the already-stored LED case data

    This provider implements that boundary. It reads eCourts-sourced data
    from the LED case record in Firestore (the 'ecourts_data' field that
    LED's own integration populates), but NEVER calls eCourts directly.

WHAT IS_CONFIGURED MEANS HERE:
    is_configured = True if Firestore is accessible (JURIS already uses it
    for auth/case access). There is no separate credential required.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from ecourts.base import ECourtsProvider, ECourtsSnapshot
from core.errors import ProviderUnavailableError

logger = logging.getLogger("juris.ecourts.led_data")


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None


def _list_of_str(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if v]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


class LedDataECourtsProvider(ECourtsProvider):
    """
    Reads eCourts-sourced data from what LED has already stored in Firestore.

    JURIS cannot call eCourts. This provider reads the 'ecourts_data'
    sub-document that LED's authorized integration writes onto the case record.

    If the field does not exist, the case has never been synced from eCourts
    via LED — a clear, accurate message is returned (not invented data).
    """

    def __init__(self, firestore_client=None) -> None:
        self._client = firestore_client

    @property
    def provider_name(self) -> str:
        return "led_firestore"

    @property
    def is_configured(self) -> bool:
        from core.config import settings
        return bool(settings.firebase_project_id)

    def _get_client(self):
        if self._client is not None:
            return self._client
        from core.firestore_client import get_firestore_client
        self._client = get_firestore_client()
        return self._client

    async def get_case_by_cnr(self, cnr: str) -> ECourtsSnapshot:
        """
        Retrieve eCourts data from what LED has already stored.

        Looks for a Firestore case document with matching cnrNumber that has
        an 'ecourts_data' field populated by LED's authorized eCourts integration.

        NEVER calls eCourts directly. NEVER returns invented data.
        """
        import asyncio

        try:
            client = self._get_client()
            cases_ref = client.collection("cases")
            query = cases_ref.where("cnrNumber", "==", cnr).limit(1)

            docs = await asyncio.get_event_loop().run_in_executor(
                None, lambda: list(query.stream())
            )
        except Exception as exc:
            logger.error("Firestore query failed for CNR %s: %s", cnr, exc)
            raise ProviderUnavailableError(
                "eCourts data lookup failed: cannot reach the case database."
            ) from exc

        if not docs:
            return ECourtsSnapshot(
                cnr=cnr,
                status="Case not found in LED. "
                       "Add this case to LED first, then use LED's eCourts sync to fetch court data.",
                retrieved_at=datetime.now(timezone.utc),
                provider=self.provider_name,
            )

        doc_data = docs[0].to_dict() or {}
        ecourts_data: dict = doc_data.get("ecourts_data") or {}

        if not ecourts_data:
            case_title = doc_data.get("caseTitle") or doc_data.get("title") or "this case"
            return ECourtsSnapshot(
                cnr=cnr,
                status=(
                    f"eCourts data has not been fetched for {case_title}. "
                    "Use the 'Refresh from eCourts' option in the LED case detail screen "
                    "to fetch the latest court data."
                ),
                court_name=_str_or_none(doc_data.get("courtName")),
                retrieved_at=datetime.now(timezone.utc),
                provider=self.provider_name,
            )

        return ECourtsSnapshot(
            cnr=cnr,
            case_number=_str_or_none(ecourts_data.get("case_number") or doc_data.get("caseNumber")),
            case_type=_str_or_none(ecourts_data.get("case_type") or doc_data.get("caseType")),
            filing_date=_str_or_none(ecourts_data.get("filing_date")),
            registration_date=_str_or_none(ecourts_data.get("registration_date")),
            status=_str_or_none(ecourts_data.get("status") or doc_data.get("status")),
            stage_of_case=_str_or_none(ecourts_data.get("stage_of_case")),
            court_name=_str_or_none(ecourts_data.get("court_name") or doc_data.get("courtName")),
            court_number=_str_or_none(ecourts_data.get("court_number")),
            state=_str_or_none(ecourts_data.get("state")),
            district=_str_or_none(ecourts_data.get("district")),
            petitioner_names=_list_of_str(ecourts_data.get("petitioner_names")),
            respondent_names=_list_of_str(ecourts_data.get("respondent_names")),
            petitioner_advocate=_str_or_none(ecourts_data.get("petitioner_advocate")),
            respondent_advocate=_str_or_none(ecourts_data.get("respondent_advocate")),
            next_hearing_date=_str_or_none(
                ecourts_data.get("next_hearing_date") or str(doc_data.get("nextHearingDate") or "")
            ),
            last_hearing_date=_str_or_none(ecourts_data.get("last_hearing_date")),
            orders=ecourts_data.get("orders") if isinstance(ecourts_data.get("orders"), list) else [],
            acts=_list_of_str(ecourts_data.get("acts")),
            retrieved_at=datetime.now(timezone.utc),
            provider=self.provider_name,
        )
