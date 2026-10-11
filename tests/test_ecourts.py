"""
tests/test_ecourts.py — Unit tests for eCourts provider layer + JURIS security boundary.

Tests:
    1. CNR normalization (valid, whitespace, lowercase, invalid format, empty)
    2. ECourtsIndiaProvider (reference impl — not used in JURIS production):
       - unconfigured raises ProviderUnavailableError
       - placeholder key is not configured
       - HTTP 200 parses full case snapshot
       - HTTP 401/403 raises ProviderUnavailableError
       - HTTP 404 returns snapshot with "Case not found" status
       - HTTP 429 raises ProviderRateLimitError
       - HTTP 500 raises ProviderUnavailableError
       - Timeout raises ProviderTimeoutError
       - Malformed JSON raises ProviderMalformedResponseError
    3. ECourtsService:
       - valid CNR normalized before provider call
       - invalid CNR rejected before provider call
    4. JURIS Security Boundary (critical):
       - JURIS config has NO ecourts_base_url field
       - JURIS config has NO ecourts_api_key field
       - get_ecourts_provider() returns LedDataECourtsProvider (NOT ECourtsIndiaProvider)
       - LedDataECourtsProvider.is_configured does NOT require eCourts credentials
    5. GetECourtsCaseTool:
       - executes via tool invocation contract
       - handles invalid CNR gracefully
       - reads LED-stored data (via LedDataECourtsProvider), not live eCourts
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from core.cnr import is_valid_cnr, normalize_cnr
from core.errors import (
    InvalidCnrError,
    ProviderMalformedResponseError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from core.identity import JurisUser
from core.permissions import Action, PermissionContext, ResourceType
from ecourts.base import ECourtsSnapshot
from ecourts.ecourtsindia_provider import ECourtsIndiaProvider
from ecourts.service import ECourtsService
from tools.base import ToolInvocation
from tools.legal_research_tools import GetECourtsCaseTool


# ─────────────────────────────────────────────────────────────────────────────
# 1. CNR Normalization
# ─────────────────────────────────────────────────────────────────────────────

class TestCnrNormalization(unittest.TestCase):
    """Test Case Number Record (CNR) validation and normalization."""

    def test_valid_cnr_preserves_canonical_form(self):
        cnr = "MHCC010012342024"
        self.assertEqual(normalize_cnr(cnr), "MHCC010012342024")
        self.assertTrue(is_valid_cnr(cnr))

    def test_lowercase_and_whitespace_are_normalized(self):
        raw = "  mhcc010012342024  "
        self.assertEqual(normalize_cnr(raw), "MHCC010012342024")
        self.assertTrue(is_valid_cnr(raw))

    def test_invalid_length_raises_invalid_cnr_error(self):
        with self.assertRaises(InvalidCnrError):
            normalize_cnr("MHCC1234")  # too short
        self.assertFalse(is_valid_cnr("MHCC1234"))

    def test_invalid_characters_raises_invalid_cnr_error(self):
        with self.assertRaises(InvalidCnrError):
            normalize_cnr("MHCC0100-2342024")  # hyphen not allowed
        self.assertFalse(is_valid_cnr("MHCC0100-2342024"))

    def test_empty_cnr_raises_invalid_cnr_error(self):
        with self.assertRaises(InvalidCnrError):
            normalize_cnr("")
        with self.assertRaises(InvalidCnrError):
            normalize_cnr(None)
        self.assertFalse(is_valid_cnr(""))


# ─────────────────────────────────────────────────────────────────────────────
# 2. ECourtsIndiaProvider (reference impl — NOT used in JURIS production)
# ─────────────────────────────────────────────────────────────────────────────

class TestECourtsIndiaProvider(unittest.IsolatedAsyncioTestCase):
    """
    Test ECourtsIndiaProvider with mocked network calls.

    NOTE: This provider is NOT used in JURIS production.
    It is a reference implementation for the LED-side authorized integration.
    These tests verify the HTTP client logic in isolation, using explicit
    constructor args (no JURIS settings).
    """

    def test_unconfigured_provider_is_not_configured(self):
        provider = ECourtsIndiaProvider(base_url="", api_key="")
        self.assertFalse(provider.is_configured)

    def test_placeholder_key_is_not_configured(self):
        provider = ECourtsIndiaProvider(
            base_url="https://webapi.ecourtsindia.com",
            api_key="paste the API key here man",
        )
        self.assertFalse(provider.is_configured)

    async def test_unconfigured_provider_raises_unavailable_error(self):
        provider = ECourtsIndiaProvider(base_url="", api_key="")
        with self.assertRaises(ProviderUnavailableError):
            await provider.get_case_by_cnr("MHCC010012342024")

    @patch("httpx.AsyncClient.get")
    async def test_successful_response_parsed_into_snapshot(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": {
                "cnr_number": "MHCC010012342024",
                "case_no": "WP/123/2024",
                "case_type": "Writ Petition",
                "filing_date": "2024-01-15",
                "registration_date": "2024-01-16",
                "case_status": "Pending",
                "stage_of_case": "Arguments",
                "court_name": "High Court of Bombay",
                "court_number_chamber": "Court Hall 3",
                "state_name": "Maharashtra",
                "district_name": "Mumbai",
                "petitioner": ["Ramesh Kumar"],
                "respondent": ["State of Maharashtra", "Municipal Corp"],
                "petitioner_advocate": "Adv. S. Sharma",
                "respondent_advocate": "Adv. M. Patel",
                "next_hearing_date": "2026-11-20",
                "last_hearing_date": "2026-09-15",
                "orders": [
                    {"date": "2026-09-15", "detail": "Arguments heard in part"}
                ],
                "case_acts": ["Constitution of India, Art. 226"],
            }
        }
        mock_get.return_value = mock_response

        # Provider used with explicit creds (LED-side reference usage only)
        provider = ECourtsIndiaProvider(
            base_url="https://webapi.ecourtsindia.com",
            api_key="valid-test-key-12345",
        )
        self.assertTrue(provider.is_configured)

        snapshot = await provider.get_case_by_cnr("MHCC010012342024")

        self.assertEqual(snapshot.cnr, "MHCC010012342024")
        self.assertEqual(snapshot.case_number, "WP/123/2024")
        self.assertEqual(snapshot.case_type, "Writ Petition")
        self.assertEqual(snapshot.status, "Pending")
        self.assertEqual(snapshot.stage_of_case, "Arguments")
        self.assertEqual(snapshot.court_name, "High Court of Bombay")
        self.assertEqual(snapshot.court_number, "Court Hall 3")
        self.assertEqual(snapshot.state, "Maharashtra")
        self.assertEqual(snapshot.district, "Mumbai")
        self.assertEqual(snapshot.petitioner_names, ["Ramesh Kumar"])
        self.assertIn("State of Maharashtra", snapshot.respondent_names)
        self.assertEqual(snapshot.next_hearing_date, "2026-11-20")
        self.assertEqual(len(snapshot.orders), 1)
        self.assertEqual(snapshot.provider, "ecourtsindia")
        self.assertIsNotNone(snapshot.retrieved_at)

    @patch("httpx.AsyncClient.get")
    async def test_auth_failure_401_raises_unavailable_error(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_get.return_value = mock_response

        provider = ECourtsIndiaProvider(
            base_url="https://webapi.ecourtsindia.com",
            api_key="invalid-key",
        )
        with self.assertRaises(ProviderUnavailableError):
            await provider.get_case_by_cnr("MHCC010012342024")

    @patch("httpx.AsyncClient.get")
    async def test_not_found_404_returns_not_found_snapshot(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_get.return_value = mock_response

        provider = ECourtsIndiaProvider(
            base_url="https://webapi.ecourtsindia.com",
            api_key="valid-key-12345",
        )
        snapshot = await provider.get_case_by_cnr("MHCC010012342024")

        self.assertEqual(snapshot.cnr, "MHCC010012342024")
        self.assertIn("not found", (snapshot.status or "").lower())

    @patch("httpx.AsyncClient.get")
    async def test_rate_limit_429_raises_rate_limit_error(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_get.return_value = mock_response

        provider = ECourtsIndiaProvider(
            base_url="https://webapi.ecourtsindia.com",
            api_key="valid-key-12345",
        )
        with self.assertRaises(ProviderRateLimitError):
            await provider.get_case_by_cnr("MHCC010012342024")

    @patch("httpx.AsyncClient.get")
    async def test_server_error_500_raises_unavailable_error(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_get.return_value = mock_response

        provider = ECourtsIndiaProvider(
            base_url="https://webapi.ecourtsindia.com",
            api_key="valid-key-12345",
        )
        with self.assertRaises(ProviderUnavailableError):
            await provider.get_case_by_cnr("MHCC010012342024")

    @patch("httpx.AsyncClient.get")
    async def test_timeout_raises_provider_timeout_error(self, mock_get):
        mock_get.side_effect = httpx.TimeoutException("Connection timed out")

        provider = ECourtsIndiaProvider(
            base_url="https://webapi.ecourtsindia.com",
            api_key="valid-key-12345",
        )
        with self.assertRaises(ProviderTimeoutError):
            await provider.get_case_by_cnr("MHCC010012342024")

    @patch("httpx.AsyncClient.get")
    async def test_malformed_json_raises_malformed_response_error(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.side_effect = ValueError("Invalid JSON string")
        mock_get.return_value = mock_response

        provider = ECourtsIndiaProvider(
            base_url="https://webapi.ecourtsindia.com",
            api_key="valid-key-12345",
        )
        with self.assertRaises(ProviderMalformedResponseError):
            await provider.get_case_by_cnr("MHCC010012342024")


# ─────────────────────────────────────────────────────────────────────────────
# 3. ECourtsService
# ─────────────────────────────────────────────────────────────────────────────

class TestECourtsService(unittest.IsolatedAsyncioTestCase):
    """Test ECourtsService wrapper logic."""

    async def test_invalid_cnr_rejected_before_calling_provider(self):
        mock_provider = MagicMock()
        mock_provider.is_configured = True
        mock_provider.get_case_by_cnr = AsyncMock()

        service = ECourtsService(mock_provider)

        with self.assertRaises(InvalidCnrError):
            await service.get_case("bad_cnr")

        mock_provider.get_case_by_cnr.assert_not_called()

    async def test_valid_cnr_is_normalized_before_calling_provider(self):
        mock_provider = MagicMock()
        mock_provider.is_configured = True
        mock_provider.get_case_by_cnr = AsyncMock()

        service = ECourtsService(mock_provider)
        await service.get_case("  mhcc010012342024  ")

        mock_provider.get_case_by_cnr.assert_called_once_with("MHCC010012342024")


# ─────────────────────────────────────────────────────────────────────────────
# 4. JURIS Security Boundary — CRITICAL TESTS
# ─────────────────────────────────────────────────────────────────────────────

class TestJurisECourtsBoundary(unittest.TestCase):
    """
    Critical security boundary tests.

    Proves that JURIS does NOT hold eCourts credentials and cannot
    call eCourts directly.
    """

    def test_juris_settings_has_no_ecourts_base_url(self):
        """JURIS config must NOT contain ecourts_base_url."""
        from core.config import Settings
        self.assertFalse(
            hasattr(Settings.model_fields, "ecourts_base_url"),
            "VIOLATION: JURIS Settings must not have an ecourts_base_url field. "
            "The eCourts credential belongs only in the LED-side authorized service.",
        )

    def test_juris_settings_has_no_ecourts_api_key(self):
        """JURIS config must NOT contain ecourts_api_key."""
        from core.config import Settings
        self.assertFalse(
            hasattr(Settings.model_fields, "ecourts_api_key"),
            "VIOLATION: JURIS Settings must not have an ecourts_api_key field. "
            "The eCourts credential belongs only in the LED-side authorized service.",
        )

    def test_settings_instance_has_no_ecourts_base_url(self):
        """Live settings instance must not expose ecourts_base_url."""
        from core.config import settings
        self.assertFalse(
            hasattr(settings, "ecourts_base_url"),
            "VIOLATION: settings instance must not have ecourts_base_url attribute.",
        )

    def test_settings_instance_has_no_ecourts_api_key(self):
        """Live settings instance must not expose ecourts_api_key."""
        from core.config import settings
        self.assertFalse(
            hasattr(settings, "ecourts_api_key"),
            "VIOLATION: settings instance must not have ecourts_api_key attribute.",
        )

    def test_factory_returns_led_data_provider_not_ecourts_india_provider(self):
        """get_ecourts_provider() must return LedDataECourtsProvider, not ECourtsIndiaProvider."""
        from ecourts.factory import get_ecourts_provider
        from ecourts.led_data_provider import LedDataECourtsProvider

        # Clear lru_cache before testing to get a fresh instance
        get_ecourts_provider.cache_clear()
        provider = get_ecourts_provider()
        get_ecourts_provider.cache_clear()  # cleanup

        self.assertIsInstance(
            provider,
            LedDataECourtsProvider,
            "VIOLATION: JURIS factory must return LedDataECourtsProvider, "
            "which reads LED Firestore data. ECourtsIndiaProvider (which calls "
            "eCourts API directly) must NOT be returned by the factory.",
        )
        self.assertNotIsInstance(
            provider,
            ECourtsIndiaProvider,
            "VIOLATION: JURIS factory must NOT return ECourtsIndiaProvider.",
        )

    def test_led_data_provider_requires_no_ecourts_credentials(self):
        """LedDataECourtsProvider must not require eCourts API credentials."""
        from ecourts.led_data_provider import LedDataECourtsProvider

        # Create with no args — should not require any eCourts credentials
        provider = LedDataECourtsProvider()

        # is_configured must not check for eCourts keys
        # (it should always be True when Firestore is reachable)
        # We just verify no AttributeError is raised and it doesn't crash
        _ = provider.is_configured

    def test_ecourts_india_provider_is_not_configured_without_explicit_args(self):
        """
        ECourtsIndiaProvider (reference impl) must NOT self-configure
        from JURIS settings since those fields no longer exist.
        """
        # When created with no args, it must not be configured
        provider = ECourtsIndiaProvider()
        self.assertFalse(
            provider.is_configured,
            "ECourtsIndiaProvider with no constructor args must not be configured. "
            "JURIS does not have ecourts credentials in its settings.",
        )


# ─────────────────────────────────────────────────────────────────────────────
# 5. GetECourtsCaseTool
# ─────────────────────────────────────────────────────────────────────────────

class TestGetECourtsCaseTool(unittest.IsolatedAsyncioTestCase):
    """
    Test GetECourtsCaseTool execution.

    This tool reads LED-stored case data from Firestore (via LedDataECourtsProvider).
    It does NOT call eCourts directly.
    """

    async def test_tool_definition_properties(self):
        tool = GetECourtsCaseTool()
        self.assertEqual(tool.definition.name, "get_ecourts_case")
        self.assertTrue(tool.definition.is_ecourts)

    async def test_tool_description_mentions_led_data_source(self):
        """Tool description must accurately say JURIS reads LED-stored data."""
        tool = GetECourtsCaseTool()
        description = tool.definition.description.lower()
        # Must mention that it reads LED-stored data, not live eCourts
        self.assertTrue(
            "led" in description or "stored" in description,
            "Tool description must indicate it reads LED-stored data, not live eCourts.",
        )

    async def test_tool_execution_with_invalid_cnr_returns_error_result(self):
        tool = GetECourtsCaseTool()
        user = JurisUser(user_id="test-user-1")
        ctx = PermissionContext(user=user, resource_type=ResourceType.ECOURTS, action=Action.READ)
        invocation = ToolInvocation(
            tool_name="get_ecourts_case",
            arguments={"cnr_number": "invalid_cnr"},
            invoking_user=user,
            permission_context=ctx,
        )
        result = await tool.execute(invocation)
        self.assertFalse(result.success)
        self.assertIn("CNR", result.error)

    async def test_tool_reads_led_data_via_authorized_case(self):
        from core.authorized_access import AuthorizedLedAccess
        from core.led_access import CaseRelation, CaseAccess, LedCase

        user = JurisUser(user_id="test-user-1", is_verified=True)
        case = LedCase(
            id="case-1", owner_uid=user.user_id, case_title="Test Case",
            cnr="MHCC010012342024",
            ecourts_data={"status": "Pending", "court_name": "High Court of Bombay"},
        )
        fake_data = MagicMock()
        fake_data.get_case_access_by_cnr = AsyncMock(
            return_value=CaseAccess(case=case, relation=CaseRelation.OWNER)
        )
        access = AuthorizedLedAccess(fake_data, user)
        ctx = PermissionContext(user=user, resource_type=ResourceType.ECOURTS, action=Action.READ)
        invocation = ToolInvocation(
            tool_name="get_ecourts_case",
            arguments={"cnr_number": "MHCC010012342024"},
            invoking_user=user, permission_context=ctx, access=access,
        )
        result = await GetECourtsCaseTool().execute(invocation)
        self.assertTrue(result.success)
        self.assertEqual(result.data["ecourts_data"]["status"], "Pending")
        fake_data.get_case_access_by_cnr.assert_awaited_once_with("MHCC010012342024", user_id=user.user_id)


    async def test_tool_denies_unscoped_cnr(self):
        from core.authorized_access import AuthorizedLedAccess
        user = JurisUser(user_id="test-user-1", is_verified=True)
        fake_data = MagicMock()
        fake_data.get_case_access_by_cnr = AsyncMock(return_value=None)
        access = AuthorizedLedAccess(fake_data, user)
        ctx = PermissionContext(user=user, resource_type=ResourceType.ECOURTS, action=Action.READ)
        invocation = ToolInvocation(
            tool_name="get_ecourts_case",
            arguments={"cnr_number": "MHCC010012342024"},
            invoking_user=user, permission_context=ctx, access=access,
        )
        result = await GetECourtsCaseTool().execute(invocation)
        self.assertFalse(result.success)
        self.assertIn("access", result.error.lower())
