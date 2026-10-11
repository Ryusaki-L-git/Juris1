"""Regression tests for authorization and write-confirmation boundaries."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from core.action_confirmations import ActionConfirmationStore
from core.identity import JurisUser
from core.led_access import CaseAccess, CaseRelation, LedCase
from core.permission_engine import LedPermissionEngine
from core.permissions import Action, PermissionContext, ResourceType


class TestPermissionEngineRegressions(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.user = JurisUser(user_id="user-a", is_verified=True)
        self.data = MagicMock()
        self.data.get_case_access = AsyncMock(return_value=CaseAccess(
            case=LedCase(id="case-a", owner_uid="user-a"),
            relation=CaseRelation.OWNER,
        ))
        from core.led_access import LedClient
        self.data.get_client = AsyncMock(return_value=LedClient(
            id="client-a", owner_uid="user-a", name="A Client", is_deleted=True
        ))
        self.engine = LedPermissionEngine(self.data)

    async def test_missing_case_id_does_not_authorize_update(self):
        result = await self.engine.check(PermissionContext(
            user=self.user, resource_type=ResourceType.CASE,
            action=Action.UPDATE,
        ))
        self.assertFalse(result.allowed)
        self.data.get_case_access.assert_not_awaited()

    async def test_missing_case_id_does_not_authorize_delete(self):
        result = await self.engine.check(PermissionContext(
            user=self.user, resource_type=ResourceType.CASE,
            action=Action.DELETE,
        ))
        self.assertFalse(result.allowed)

    async def test_document_draft_requires_paid_entitlement(self):
        engine = LedPermissionEngine(self.data, entitlements=None)
        result = await engine.check(PermissionContext(
            user=self.user, resource_type=ResourceType.DOCUMENT,
            resource_id=None, action=Action.DRAFT,
            extra={"case_id": "case-a", "tool_name": "draft_document"},
        ))
        self.assertFalse(result.allowed)

    async def test_deleted_client_is_not_authorized_for_direct_read(self):
        result = await self.engine.check(PermissionContext(
            user=self.user, resource_type=ResourceType.CLIENT,
            resource_id="client-a", action=Action.READ,
        ))
        self.assertFalse(result.allowed)

    async def test_unverified_identity_cannot_access_case(self):
        user = JurisUser(user_id="user-a", is_verified=False)
        result = await self.engine.check(PermissionContext(
            user=user, resource_type=ResourceType.CASE,
            resource_id="case-a", action=Action.READ,
        ))
        self.assertFalse(result.allowed)
        self.data.get_case_access.assert_not_awaited()


class TestActionConfirmationRegressions(unittest.IsolatedAsyncioTestCase):
    async def test_confirmation_is_single_use(self):
        store = ActionConfirmationStore()
        token = await store.issue("user-a", "session-1", "create_reminder", {"title": "Call"})
        self.assertTrue(await store.consume(
            token, "user-a", "session-1", "create_reminder", {"title": "Call"}
        ))
        self.assertFalse(await store.consume(
            token, "user-a", "session-1", "create_reminder", {"title": "Call"}
        ))

    async def test_confirmation_is_bound_to_user_session_tool_and_arguments(self):
        store = ActionConfirmationStore()
        token = await store.issue("user-a", "session-1", "create_reminder", {"title": "Call"})
        self.assertFalse(await store.consume(
            token, "user-b", "session-1", "create_reminder", {"title": "Call"}
        ))
        self.assertFalse(await store.consume(
            token, "user-a", "session-2", "create_reminder", {"title": "Call"}
        ))
        self.assertFalse(await store.consume(
            token, "user-a", "session-1", "create_reminder", {"title": "Different"}
        ))
        # A mismatched attempt must not consume the legitimate pending action.
        self.assertTrue(await store.consume(
            token, "user-a", "session-1", "create_reminder", {"title": "Call"}
        ))


class TestMonitoringAccessRegression(unittest.IsolatedAsyncioTestCase):
    async def test_missing_case_access_pauses_monitor(self):
        from monitoring.models import CaseMonitorRecord
        from monitoring.scheduler import _validate_monitor_record
        from datetime import datetime, timezone

        record = CaseMonitorRecord(
            id="m-1", user_id="user-a", case_id="case-a",
            cnr="MHCC010012342024", created_at=datetime.now(timezone.utc),
        )
        data_access = MagicMock()
        data_access.get_case_access = AsyncMock(return_value=None)
        with patch("monitoring.scheduler.get_led_data_access", return_value=data_access):
            reason = await _validate_monitor_record(record)
        self.assertIsNotNone(reason)
        self.assertIn("access is no longer valid", reason)

    async def test_monitor_creation_rejects_cnr_not_matching_case(self):
        from core.permissions import PermissionResult
        from main import app
        from fastapi.testclient import TestClient

        access = MagicMock()
        access.get_case_access = AsyncMock(return_value=SimpleNamespace(
            case=SimpleNamespace(cnr="DLCT010056782024")
        ))
        with (
            patch("api.monitoring._check_monitoring_entitlement", new_callable=AsyncMock, return_value=None),
            patch("api.monitoring.get_led_data_access", return_value=access),
            patch("api.monitoring.LedPermissionEngine.check", new_callable=AsyncMock,
                  return_value=PermissionResult(allowed=True, reason="test")),
        ):
            response = TestClient(app).post(
                "/monitor", json={"case_id": "case-a", "cnr": "MHCC010012342024"}
            )
        self.assertEqual(response.status_code, 422)
        self.assertIn("does not match", response.json()["detail"])


class TestMonitoringStoreOwnership(unittest.IsolatedAsyncioTestCase):
    async def test_get_and_delete_require_matching_user_when_supplied(self):
        from datetime import datetime, timezone
        from monitoring.models import CaseMonitorRecord
        from monitoring.store import MonitoringStore

        store = MonitoringStore()
        record = CaseMonitorRecord(
            id="monitor-a", user_id="user-a", case_id="case-a",
            cnr="MHCC010012342024", created_at=datetime.now(timezone.utc),
        )
        await store.upsert(record)
        self.assertIsNone(await store.get("monitor-a", "user-b"))
        self.assertFalse(await store.delete("monitor-a", "user-b"))
        self.assertIsNotNone(await store.get("monitor-a", "user-a"))
        self.assertTrue(await store.delete("monitor-a", "user-a"))
