"""Centralized Firestore client creation for JURIS."""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from core.config import settings
from core.errors import ConfigurationError


@lru_cache(maxsize=1)
def get_firestore_client() -> Any:
    """Use hosting ADC or an optional service-account JSON secret."""
    if not settings.firebase_project_id:
        raise ConfigurationError("JURIS Firebase project ID is not configured.")

    from google.cloud import firestore

    raw = settings.firebase_service_account_json.strip()
    if not raw:
        # The hosting platform must provide valid Application Default Credentials.
        return firestore.Client(project=settings.firebase_project_id)

    try:
        info = json.loads(raw)
        if not isinstance(info, dict) or info.get("type") != "service_account":
            raise ValueError("Expected service-account JSON.")
        from google.oauth2 import service_account
        credentials = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        return firestore.Client(
            project=settings.firebase_project_id, credentials=credentials
        )
    except Exception as exc:
        raise ConfigurationError(
            "JURIS_FIREBASE_SERVICE_ACCOUNT_JSON is invalid or could not initialize Firestore credentials."
        ) from exc
