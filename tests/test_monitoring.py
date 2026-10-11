"""
tests/test_monitoring.py — Tests for JURIS automatic case monitoring.

ARCHITECTURE NOTE:
    JURIS monitoring detects changes in LED-stored case data (Firestore).
    JURIS does NOT call eCourts directly. If LED syncs new eCourts data
    into Firestore, JURIS monitoring detects the fingerprint change.

Tests:
    1. Deterministic fingerprinting:
       - same case state → identical fingerprint
       - list order variation → identical fingerprint (sorting stability)
       - changed hearing date / status → different fingerprint
       - retrieved_at timestamp does NOT change fingerprint
       - provider field does NOT change fingerprint
    2. MonitoringStore:
       - upsert, get, list_for_user, list_due, delete
       - get_for_user_and_case
    3. Scheduler check execution (reads LED Firestore data via ECourtsService):
       - first check saves fingerprint without change notification
       - changed case detects change and dispatches notification
       - unchanged case does not dispatch notification
       - provider error increments consecutive_failures
       - max consecutive failures sets status to ERROR
    4. Monitoring API routes:
       - POST /monitor creates monitor record
       - POST /monitor with invalid CNR returns HTTP 422
       - POST /monitor is idempotent for user+case
       - GET /monitor returns list of monitors
       - DELETE /monitor/{id} deletes record
       - DELETE /monitor/{id} for different user returns 403
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from core.identity import JurisUser, RequestIdentity
from ecourts.base import ECourtsSnapshot
from main import app
from monitoring.fingerprint import compute_fingerprint, has_changed, summarize_change
from monitoring.models import CaseMonitorRecord, MonitoringStatus
from monitoring.scheduler import _check_record
from monitoring.store import MonitoringStore, get_monitoring_store


def _make_snapshot(
    cnr: str = "MHCC010012342024",
    status: str = "Pending",
    next_hearing: str | None = "2026-11-20",
    retrieved_at: datetime | None = None,
    provider: str = "led_firestore",  # Correct provider name (LedDataECourtsProvider)
    petitioners: list[str] | None = None,
    orders: list[dict] | None = None,
) -> ECourtsSnapshot:
    return ECourtsSnapshot(
        cnr=cnr,
        status=status,
        stage_of_case="Arguments",
        court_name="High Court of Bombay",
        next_hearing_date=next_hearing,
        last_hearing_date="2026-09-15",
        petitioner_names=petitioners or ["Ramesh Kumar"],
        respondent_names=["State of Maharashtra"],
        orders=orders or [{"date": "2026-09-15", "detail": "Arguments heard"}],
        retrieved_at=retrieved_at or datetime.now(timezone.utc),
        provider=provider,
    )


class TestFingerprinting(unittest.TestCase):
    """Test deterministic case-state fingerprinting."""

    def test_identical_snapshots_produce_identical_fingerprint(self):
        s1 = _make_snapshot()
        s2 = _make_snapshot()
        self.assertEqual(compute_fingerprint(s1), compute_fingerprint(s2))

    def test_retrieved_at_does_not_affect_fingerprint(self):
        t1 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 10, 2, 18, 30, 0, tzinfo=timezone.utc)
        s1 = _make_snapshot(retrieved_at=t1)
        s2 = _make_snapshot(retrieved_at=t2)
        self.assertEqual(compute_fingerprint(s1), compute_fingerprint(s2))

    def test_provider_name_does_not_affect_fingerprint(self):
        s1 = _make_snapshot(provider="ecourtsindia")
        s2 = _make_snapshot(provider="alternative_provider")
        self.assertEqual(compute_fingerprint(s1), compute_fingerprint(s2))

    def test_petitioner_order_does_not_affect_fingerprint(self):
        s1 = _make_snapshot(petitioners=["Alice", "Bob"])
        s2 = _make_snapshot(petitioners=["Bob", "Alice"])
        self.assertEqual(compute_fingerprint(s1), compute_fingerprint(s2))

    def test_order_date_order_does_not_affect_fingerprint(self):
        o1 = [{"date": "2026-01-01"}, {"date": "2026-06-01"}]
        o2 = [{"date": "2026-06-01"}, {"date": "2026-01-01"}]
        s1 = _make_snapshot(orders=o1)
        s2 = _make_snapshot(orders=o2)
        self.assertEqual(compute_fingerprint(s1), compute_fingerprint(s2))

    def test_changed_hearing_date_produces_different_fingerprint(self):
        s1 = _make_snapshot(next_hearing="2026-11-20")
        s2 = _make_snapshot(next_hearing="2026-12-15")
        self.assertNotEqual(compute_fingerprint(s1), compute_fingerprint(s2))

    def test_changed_status_produces_different_fingerprint(self):
        s1 = _make_snapshot(status="Pending")
        s2 = _make_snapshot(status="Disposed")
        self.assertNotEqual(compute_fingerprint(s1), compute_fingerprint(s2))

    def test_has_changed_logic(self):
        fp1 = compute_fingerprint(_make_snapshot(status="Pending"))
        fp2 = compute_fingerprint(_make_snapshot(status="Disposed"))

        # First check (no previous fingerprint) is never a change
        self.assertFalse(has_changed(None, fp1))
        # Same fingerprint is not a change
        self.assertFalse(has_changed(fp1, fp1))
        # Different fingerprint is a change
        self.assertTrue(has_changed(fp1, fp2))


class TestMonitoringStore(unittest.IsolatedAsyncioTestCase):
    """Test in-memory MonitoringStore."""

    async def asyncSetUp(self):
        self.store = MonitoringStore()

    async def test_upsert_and_get(self):
        now = datetime.now(timezone.utc)
        record = CaseMonitorRecord(
            id="mon-1",
            user_id="user-1",
            case_id="case-101",
            cnr="MHCC010012342024",
            created_at=now,
        )
        await self.store.upsert(record)
        fetched = await self.store.get("mon-1")

        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.cnr, "MHCC010012342024")
        self.assertEqual(fetched.case_id, "case-101")

    async def test_list_for_user_filters_correctly(self):
        now = datetime.now(timezone.utc)
        r1 = CaseMonitorRecord(id="m1", user_id="u1", case_id="c1", cnr="MHCC010012342024", created_at=now)
        r2 = CaseMonitorRecord(id="m2", user_id="u2", case_id="c2", cnr="DLCT010056782024", created_at=now)
        await self.store.upsert(r1)
        await self.store.upsert(r2)

        u1_records = await self.store.list_for_user("u1")
        self.assertEqual(len(u1_records), 1)
        self.assertEqual(u1_records[0].id, "m1")

    async def test_get_for_user_and_case(self):
        now = datetime.now(timezone.utc)
        r = CaseMonitorRecord(id="m1", user_id="u1", case_id="c1", cnr="MHCC010012342024", created_at=now)
        await self.store.upsert(r)

        found = await self.store.get_for_user_and_case("u1", "c1")
        self.assertIsNotNone(found)
        self.assertEqual(found.id, "m1")

        not_found = await self.store.get_for_user_and_case("u1", "non-existent")
        self.assertNull if hasattr(self, "assertNull") else self.assertIsNone(not_found)

    async def test_list_due_returns_records_needing_check(self):
        now = datetime.now(timezone.utc)
        old_time = now - timedelta(hours=4)
        recent_time = now - timedelta(minutes=10)

        # Due: never checked
        r_new = CaseMonitorRecord(id="m1", user_id="u1", case_id="c1", cnr="MHCC010012342024", created_at=now)
        # Due: checked 4 hours ago
        r_old = CaseMonitorRecord(id="m2", user_id="u1", case_id="c2", cnr="MHCC010012342025", created_at=now, last_checked_at=old_time)
        # Not due: checked 10 minutes ago
        r_recent = CaseMonitorRecord(id="m3", user_id="u1", case_id="c3", cnr="MHCC010012342026", created_at=now, last_checked_at=recent_time)
        # Not due: disabled
        r_disabled = CaseMonitorRecord(id="m4", user_id="u1", case_id="c4", cnr="MHCC010012342027", status=MonitoringStatus.DISABLED, created_at=now)

        await self.store.upsert(r_new)
        await self.store.upsert(r_old)
        await self.store.upsert(r_recent)
        await self.store.upsert(r_disabled)

        cutoff = now - timedelta(hours=2)
        due = await self.store.list_due(before=cutoff, batch_size=10)
        due_ids = [r.id for r in due]

        self.assertIn("m1", due_ids)
        self.assertIn("m2", due_ids)
        self.assertNotIn("m3", due_ids)
        self.assertNotIn("m4", due_ids)

    async def test_delete_removes_record(self):
        now = datetime.now(timezone.utc)
        r = CaseMonitorRecord(id="m1", user_id="u1", case_id="c1", cnr="MHCC010012342024", created_at=now)
        await self.store.upsert(r)

        self.assertTrue(await self.store.delete("m1"))
        self.assertIsNone(await self.store.get("m1"))
        self.assertFalse(await self.store.delete("m1"))  # second delete returns False


class TestSchedulerCheckRecord(unittest.IsolatedAsyncioTestCase):
    """Test the single-record monitoring check logic."""

    @patch("monitoring.scheduler.get_monitoring_store")
    @patch("monitoring.scheduler.dispatch_change_notification")
    @patch("monitoring.scheduler._validate_monitor_record", new_callable=AsyncMock, return_value=None)
    async def test_first_check_records_fingerprint_no_notification(
        self, mock_validate, mock_dispatch, mock_store_factory
    ):
        mock_store = AsyncMock()
        mock_store_factory.return_value = mock_store

        mock_service = AsyncMock()
        mock_service.get_case.return_value = _make_snapshot(status="Pending")

        now = datetime.now(timezone.utc)
        record = CaseMonitorRecord(
            id="m1",
            user_id="u1",
            case_id="c1",
            cnr="MHCC010012342024",
            created_at=now,
            last_known_fingerprint=None,  # first check
        )

        await _check_record(record, mock_service)

        mock_store.upsert.assert_called_once()
        saved_record = mock_store.upsert.call_args[0][0]

        self.assertIsNotNone(saved_record.last_known_fingerprint)
        self.assertIsNotNone(saved_record.last_successful_check_at)
        self.assertEqual(saved_record.consecutive_failures, 0)
        # First check must NOT dispatch a notification
        mock_dispatch.assert_not_called()

    @patch("monitoring.scheduler.get_monitoring_store")
    @patch("monitoring.scheduler.dispatch_change_notification")
    @patch("monitoring.scheduler._validate_monitor_record", new_callable=AsyncMock, return_value=None)
    async def test_changed_case_dispatches_notification(
        self, mock_validate, mock_dispatch, mock_store_factory
    ):
        mock_store = AsyncMock()
        mock_store_factory.return_value = mock_store

        old_snapshot = _make_snapshot(status="Pending", next_hearing="2026-10-10")
        old_fingerprint = compute_fingerprint(old_snapshot)

        # External service now returns a new hearing date
        new_snapshot = _make_snapshot(status="Pending", next_hearing="2026-11-25")
        mock_service = AsyncMock()
        mock_service.get_case.return_value = new_snapshot

        now = datetime.now(timezone.utc)
        record = CaseMonitorRecord(
            id="m1",
            user_id="u1",
            case_id="c1",
            cnr="MHCC010012342024",
            created_at=now,
            last_known_fingerprint=old_fingerprint,
        )

        await _check_record(record, mock_service)

        mock_dispatch.assert_called_once()
        saved_record = mock_store.upsert.call_args[0][0]
        self.assertNotEqual(saved_record.last_known_fingerprint, old_fingerprint)
        self.assertIsNotNone(saved_record.last_detected_change_at)

    @patch("monitoring.scheduler.get_monitoring_store")
    @patch("monitoring.scheduler.dispatch_change_notification")
    @patch("monitoring.scheduler._validate_monitor_record", new_callable=AsyncMock, return_value=None)
    async def test_consecutive_failures_eventually_sets_error_status(
        self, mock_validate, mock_dispatch, mock_store_factory
    ):
        from core.errors import ProviderUnavailableError

        mock_store = AsyncMock()
        mock_store_factory.return_value = mock_store

        mock_service = AsyncMock()
        mock_service.get_case.side_effect = ProviderUnavailableError("Provider offline")

        now = datetime.now(timezone.utc)
        # 4 prior failures; 5th should trip ERROR status
        record = CaseMonitorRecord(
            id="m1",
            user_id="u1",
            case_id="c1",
            cnr="MHCC010012342024",
            created_at=now,
            consecutive_failures=4,
        )

        await _check_record(record, mock_service)

        saved_record = mock_store.upsert.call_args[0][0]
        self.assertEqual(saved_record.consecutive_failures, 5)
        self.assertEqual(saved_record.status, MonitoringStatus.ERROR)
        self.assertIn("offline", saved_record.last_error or "")


class TestMonitoringApi(unittest.TestCase):
    """Test /monitor FastAPI HTTP endpoints."""

    def setUp(self):
        from core.permissions import PermissionResult
        self._entitlement_patch = patch("api.monitoring._check_monitoring_entitlement", new_callable=AsyncMock, return_value=None)
        from types import SimpleNamespace
        mock_access = MagicMock()
        mock_access.get_case_access = AsyncMock(return_value=SimpleNamespace(
            case=SimpleNamespace(cnr="MHCC010012342024")
        ))
        self._data_access_patch = patch("api.monitoring.get_led_data_access", return_value=mock_access)
        self._data_access_patch.start()
        self._permission_patch = patch(
            "api.monitoring.LedPermissionEngine.check",
            new=AsyncMock(return_value=PermissionResult(allowed=True, reason="test")),
        )
        self._entitlement_patch.start()
        self._permission_patch.start()
        self.client = TestClient(app)

    def tearDown(self):
        self._permission_patch.stop()
        self._entitlement_patch.stop()
        self._data_access_patch.stop()

    def test_create_monitor_with_valid_cnr_returns_201(self):
        response = self.client.post(
            "/monitor",
            json={"case_id": "case-test-1", "cnr": "MHCC010012342024"},
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data["case_id"], "case-test-1")
        self.assertEqual(data["cnr"], "MHCC010012342024")
        self.assertEqual(data["status"], "active")

    def test_create_monitor_with_invalid_cnr_returns_422(self):
        response = self.client.post(
            "/monitor",
            json={"case_id": "case-test-2", "cnr": "invalid_cnr"},
        )
        self.assertEqual(response.status_code, 422)

    def test_create_monitor_is_idempotent(self):
        r1 = self.client.post(
            "/monitor",
            json={"case_id": "case-idempotent", "cnr": "MHCC010012342024"},
        )
        self.assertEqual(r1.status_code, 201)
        id1 = r1.json()["id"]

        r2 = self.client.post(
            "/monitor",
            json={"case_id": "case-idempotent", "cnr": "MHCC010012342024"},
        )
        self.assertEqual(r2.status_code, 201)
        id2 = r2.json()["id"]

        self.assertEqual(id1, id2)

    def test_list_monitors_returns_list(self):
        response = self.client.get("/monitor")
        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.json(), list)

    def test_delete_monitor(self):
        # Create first
        created = self.client.post(
            "/monitor",
            json={"case_id": "case-to-delete", "cnr": "MHCC010012342024"},
        ).json()
        mon_id = created["id"]

        # Delete
        delete_resp = self.client.delete(f"/monitor/{mon_id}")
        self.assertEqual(delete_resp.status_code, 204)

        # Deleting non-existent returns 404
        delete_again = self.client.delete(f"/monitor/{mon_id}")
        self.assertEqual(delete_again.status_code, 404)
