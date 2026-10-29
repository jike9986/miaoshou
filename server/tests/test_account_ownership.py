from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from core.config import get_settings
from db.session import SessionLocal, init_db
from main import app
from models.account import Account
from services import account_service, operator_service

API = "/api/v1"


@pytest.fixture(scope="module")
def client():
    init_db()
    instance = TestClient(app, raise_server_exceptions=False)
    yield instance
    instance.close()


@pytest.fixture(scope="module")
def identities(client):
    del client
    db = SessionLocal()
    try:
        admin = operator_service.ensure_default(db)
        first, first_token = operator_service.create(
            db, f"owner-a-{uuid.uuid4().hex[:8]}", role=operator_service.ROLE_MEMBER)
        second, second_token = operator_service.create(
            db, f"owner-b-{uuid.uuid4().hex[:8]}", role=operator_service.ROLE_MEMBER)
        unbound, unbound_token = operator_service.create(
            db, f"owner-unbound-{uuid.uuid4().hex[:8]}",
            role=operator_service.ROLE_MEMBER)
        teacher, teacher_token = operator_service.create(
            db, f"owner-teacher-{uuid.uuid4().hex[:8]}",
            role=operator_service.ROLE_TEACHER)
        db.commit()
        legacy = Account(name="legacy unassigned", app_key="legacy-key",
                         app_secret_enc="", status="active", shops="[]",
                         is_default=True)
        db.add(legacy)
        db.commit()
        return {
            "first_id": first.id,
            "first_token": first_token,
            "second_id": second.id,
            "second_token": second_token,
            "unbound_id": unbound.id,
            "unbound_token": unbound_token,
            "teacher_id": teacher.id,
            "teacher_token": teacher_token,
            "admin_token": get_settings().local_secret(),
            "admin_id": admin.id,
        }
    finally:
        db.close()


class FakeMiaoshouClient:
    def __init__(self, app_key, app_secret):
        self.app_key = app_key
        self.app_secret = app_secret

    def shop_list(self, **_kwargs):
        return {"data": {"shopList": []}}

    def close(self):
        pass


def _headers(token):
    return {"Authorization": f"Bearer {token}"}


def test_member_can_bind_own_key_and_cannot_read_other_account(
        client, identities, monkeypatch):
    monkeypatch.setattr(account_service, "MiaoshouClient", FakeMiaoshouClient)
    first = client.post(
        API + "/auth/connect",
        json={"appId": "key-a", "appSecret": "secret-a", "autoScan": False},
        headers=_headers(identities["first_token"]),
    )
    assert first.status_code == 200, first.text
    first_account_id = first.json()["accountId"]
    assert first.json()["ownerOperatorId"] == str(identities["first_id"])
    assert first.json()["boundByOperatorId"] == str(identities["first_id"])

    db = SessionLocal()
    try:
        second_account = Account(
            name="second account", app_key="key-b",
            app_secret_enc=account_service.crypto.encrypt("secret-b"),
            owner_operator_id=identities["second_id"],
            bound_by_operator_id=identities["second_id"],
            secret_hint="cret-b", app_id_hint="key-b", status="active",
            shops="[]", is_default=False,
        )
        db.add(second_account)
        db.commit()
        second_account_id = str(second_account.id)
    finally:
        db.close()

    first_status = client.get(API + "/auth/status",
                              headers=_headers(identities["first_token"]))
    assert first_status.status_code == 200, first_status.text
    assert first_status.json()["accountId"] == first_account_id
    assert [row["id"] for row in first_status.json()["accounts"]] == [first_account_id]

    foreign = client.get(API + "/shops", params={"accountId": second_account_id},
                         headers=_headers(identities["first_token"]))
    assert foreign.status_code == 403, foreign.text

    header_bypass = client.get(
        API + "/shops", headers={
            **_headers(identities["first_token"]),
            "X-Miaoshou-AppKey": "other-key",
            "X-Miaoshou-AppSecret": "other-secret",
        })
    assert header_bypass.status_code == 403, header_bypass.text

    second_status = client.get(API + "/auth/status",
                               headers=_headers(identities["second_token"]))
    assert second_status.status_code == 200, second_status.text
    assert second_status.json()["accountId"] == second_account_id

    admin_status = client.get(API + "/auth/accounts",
                              headers=_headers(identities["admin_token"]))
    assert admin_status.status_code == 200, admin_status.text
    assert {row["id"] for row in admin_status.json()["accounts"]} >= {
        first_account_id, second_account_id}
    assert all("appSecret" not in row and "app_secret_enc" not in row
               for row in admin_status.json()["accounts"])

    teacher_status = client.get(API + "/auth/accounts",
                                headers=_headers(identities["teacher_token"]))
    assert {row["id"] for row in teacher_status.json()["accounts"]} >= {
        first_account_id, second_account_id}


def test_unbound_member_does_not_inherit_legacy_default_account(client, identities):
    response = client.get(API + "/auth/status",
                          headers=_headers(identities["unbound_token"]))
    assert response.status_code == 200, response.text
    assert response.json()["connected"] is False
    assert response.json()["accountId"] is None
    assert response.json()["accounts"] == []


def test_teacher_must_target_a_student_to_bind_key(client, identities, monkeypatch):
    monkeypatch.setattr(account_service, "MiaoshouClient", FakeMiaoshouClient)
    response = client.post(
        API + "/auth/connect",
        json={"appId": "key-teacher", "appSecret": "secret-teacher",
              "autoScan": False},
        headers=_headers(identities["admin_token"]),
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["details"].get("field") == "studentOperatorId"

    bound = client.post(
        API + "/auth/connect",
        json={"appId": "key-assisted", "appSecret": "secret-assisted",
              "studentOperatorId": str(identities["second_id"]), "autoScan": False},
        headers=_headers(identities["teacher_token"]),
    )
    assert bound.status_code == 200, bound.text
    assert bound.json()["ownerOperatorId"] == str(identities["second_id"])
    assert bound.json()["boundByOperatorId"] == str(identities["teacher_id"])
