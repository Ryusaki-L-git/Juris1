from __future__ import annotations

from datetime import datetime, timezone

import pytest

import core.finance_store as finance_store
from core.finance_store import FirestoreFinanceRepository


class Snapshot:
    def __init__(self, data):
        self.exists = data is not None
        self._data = data or {}

    def to_dict(self):
        return dict(self._data)


class Ref:
    def __init__(self, client, path):
        self.client = client
        self.path = path

    def collection(self, name):
        return Collection(self.client, f"{self.path}/{name}")

    def document(self, name):
        return Ref(self.client, f"{self.path}/{name}")

    def get(self, transaction=None):
        return Snapshot(self.client.store.get(self.path))

    def set(self, data, merge=False):
        if merge:
            value = dict(self.client.store.get(self.path) or {})
            value.update(data)
            self.client.store[self.path] = value
        else:
            self.client.store[self.path] = dict(data)

    def create(self, data):
        if self.path in self.client.store:
            raise RuntimeError("document already exists")
        self.client.store[self.path] = dict(data)

    def delete(self):
        self.client.store.pop(self.path, None)


class Collection:
    def __init__(self, client, path):
        self.client = client
        self.path = path

    def document(self, name):
        return Ref(self.client, f"{self.path}/{name}")


class FakeClient:
    def __init__(self, store):
        self.store = dict(store)

    def collection(self, name):
        return Collection(self, name)

    def transaction(self, max_attempts=5):
        return FakeTransaction(self)


class FakeTransaction:
    def __init__(self, client):
        self.client = client

    def get(self, ref):
        return ref.get(transaction=self)

    def set(self, ref, data, merge=False):
        ref.set(data, merge=merge)

    def create(self, ref, data):
        ref.create(data)

    def delete(self, ref):
        ref.delete()


def _seed(priority="team_first"):
    period = datetime.now(timezone.utc).strftime("%Y-%m")
    return {
        "users/uid-a": {
            "billingPreferences": {
                "usageDeductionPriority": priority,
                "preferredTeamId": "team-a",
            }
        },
        "teams/team-a": {"ownerId": "uid-owner", "isActive": True},
        "teams/team-a/members/uid-a": {
            "userId": "uid-a",
            "teamId": "team-a",
            "identityType": "firebase_uid",
            "role": "member",
            "isActive": True,
            "billingLimits": {"jurisCreditLimitMinorUnits": 1000},
            "billingUsage": {
                "period": period,
                "caseTokenUsedMinorUnits": 100,
                "jurisCreditUsedMinorUnits": 100,
            },
        },
        "_server_team_token_accounts/team-a/balances/juris": {
            "tokenType": "juris",
            "balance": 10,
            "balanceMinorUnits": 1000,
        },
        "_server_token_accounts/uid-a/balances/juris": {
            "tokenType": "juris",
            "balance": 5,
            "balanceMinorUnits": 500,
        },
    }


def test_team_first_reservation_debits_shared_pool_and_member_limit(monkeypatch):
    monkeypatch.setattr(finance_store.firestore, "transactional", lambda fn: fn)
    client = FakeClient(_seed())
    repository = FirestoreFinanceRepository(client)

    entry = repository.reserve_credits(
        "uid-a", 250, "JURIS AI usage reservation", "ai-team-1"
    )

    assert entry.amount == 2.5
    assert entry.balance_after == 7.5
    assert client.store["_server_team_token_accounts/team-a/balances/juris"]["balanceMinorUnits"] == 750
    usage = client.store["teams/team-a/members/uid-a"]["billingUsage"]
    assert usage["jurisCreditUsedMinorUnits"] == 350
    marker = client.store[next(path for path in client.store if path.startswith("_server_ai_usage_reservations/"))]
    assert marker["source"] == "team"
    assert marker["teamId"] == "team-a"


def test_team_reservation_settles_unused_hold_to_same_pool(monkeypatch):
    monkeypatch.setattr(finance_store.firestore, "transactional", lambda fn: fn)
    client = FakeClient(_seed())
    repository = FirestoreFinanceRepository(client)

    repository.reserve_credits(
        "uid-a", 250, "JURIS AI usage reservation", "ai-team-2"
    )
    balance_after = repository.settle_credits("uid-a", "ai-team-2", 75)

    assert balance_after == 9.25
    assert client.store["_server_team_token_accounts/team-a/balances/juris"]["balanceMinorUnits"] == 925
    usage = client.store["teams/team-a/members/uid-a"]["billingUsage"]
    assert usage["jurisCreditUsedMinorUnits"] == 175


def test_ask_preference_fails_before_reservation_until_source_is_selected(monkeypatch):
    monkeypatch.setattr(finance_store.firestore, "transactional", lambda fn: fn)
    client = FakeClient(_seed(priority="ask"))
    repository = FirestoreFinanceRepository(client)

    with pytest.raises(finance_store.BillingSourceChoiceRequired):
        repository.reserve_credits(
            "uid-a", 100, "JURIS AI usage reservation", "ai-ask-1"
        )

    assert client.store["_server_team_token_accounts/team-a/balances/juris"]["balanceMinorUnits"] == 1000
    assert client.store["_server_token_accounts/uid-a/balances/juris"]["balanceMinorUnits"] == 500


def test_explicit_personal_source_does_not_touch_team_wallet(monkeypatch):
    monkeypatch.setattr(finance_store.firestore, "transactional", lambda fn: fn)
    client = FakeClient(_seed(priority="ask"))
    repository = FirestoreFinanceRepository(client)

    repository.reserve_credits(
        "uid-a", 100, "JURIS AI usage reservation", "ai-personal-1",
        billing_source="personal",
    )

    assert client.store["_server_token_accounts/uid-a/balances/juris"]["balanceMinorUnits"] == 400
    assert client.store["_server_team_token_accounts/team-a/balances/juris"]["balanceMinorUnits"] == 1000
