from __future__ import annotations

from fastapi.testclient import TestClient

import api.team_pools as team_pools
from api.deps import get_request_identity
from core.config import settings
from core.identity import JurisUser, RequestIdentity
from main import app
from tests.test_team_credit_pools import FakeClient, _seed

_PREFIX = settings.api_prefix.rstrip('/')


def _api(path: str) -> str:
    return f"{_PREFIX}{path}"


def _identity(uid="uid-a"):
    return RequestIdentity(
        user=JurisUser(user_id=uid, is_verified=True),
        session_id="team-pool-test-session",
        request_id="team-pool-test-request",
        source="test",
    )


def _client(monkeypatch, client):
    async def override_identity():
        return _identity()

    monkeypatch.setattr(team_pools, "get_firestore_client", lambda: client)
    app.dependency_overrides[get_request_identity] = override_identity
    return TestClient(app)


def test_preferences_read_and_save_server_side(monkeypatch):
    client = FakeClient(_seed(priority="personal_first"))
    http = _client(monkeypatch, client)
    try:
        before = http.get(_api("/billing/preferences"))
        assert before.status_code == 200
        assert before.json() == {"usageDeductionPriority": "personal_first", "preferredTeamId": "team-a"}

        saved = http.put(
            _api("/billing/preferences"),
            json={"usageDeductionPriority": "team_first", "preferredTeamId": "team-a"},
        )
        assert saved.status_code == 200
        assert saved.json()["usageDeductionPriority"] == "team_first"
        assert client.store["users/uid-a"]["billingPreferences"]["usageDeductionPriority"] == "team_first"
    finally:
        app.dependency_overrides.pop(get_request_identity, None)


def test_preferences_reject_team_the_user_does_not_belong_to(monkeypatch):
    client = FakeClient(_seed())
    http = _client(monkeypatch, client)
    try:
        response = http.put(
            "/v1/billing/preferences",
            json={"usageDeductionPriority": "team_first", "preferredTeamId": "other-team"},
        )
        assert response.status_code == 404
    finally:
        app.dependency_overrides.pop(get_request_identity, None)


def test_team_pool_keeps_personal_and_shared_balances_separate(monkeypatch):
    client = FakeClient(_seed())
    http = _client(monkeypatch, client)
    try:
        response = http.get(_api("/teams/team-a/pool"))
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["personal"]["jurisCreditsMinorUnits"] == 500
        assert body["shared"]["jurisCreditsMinorUnits"] == 1000
        assert body["allocation"]["jurisCreditAvailableMinorUnits"] == 900
        assert body["role"] == "member"
        assert body["isManager"] is False
    finally:
        app.dependency_overrides.pop(get_request_identity, None)


def test_non_manager_cannot_change_member_allocations(monkeypatch):
    client = FakeClient(_seed())
    http = _client(monkeypatch, client)
    try:
        response = http.put(
            _api("/teams/team-a/pool/members/uid-a"),
            json={
                "caseTokenLimitMinorUnits": 5000,
                "jurisCreditLimitMinorUnits": 2500,
                "storageLimitBytes": 2048,
            },
        )
        assert response.status_code == 403
    finally:
        app.dependency_overrides.pop(get_request_identity, None)
