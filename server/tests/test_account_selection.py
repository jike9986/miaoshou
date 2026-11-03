from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from db.session import SessionLocal, init_db
from main import app
from models.account import Account
from models.account_binding import AccountBinding
from services import account_service, operator_service

API = "/api/v1"


@pytest.fixture(scope="module")
def client():
    init_db()
    instance = TestClient(app, raise_server_exceptions=False)
    yield instance
    instance.close()


@pytest.fixture
def identities(client):
    del client
    db = SessionLocal()
    try:
        identities = {}
        for name, role in (
            ("admin", operator_service.ROLE_ADMIN),
            ("teacher", operator_service.ROLE_TEACHER),
            ("other_teacher", operator_service.ROLE_TEACHER),
            ("member", operator_service.ROLE_MEMBER),
            ("other_member", operator_service.ROLE_MEMBER),
            ("unbound", operator_service.ROLE_MEMBER),
        ):
            operator, token = operator_service.create(
                db, f"selection-{name}-{uuid.uuid4().hex[:8]}", role=role)
            identities[name] = {"id": operator.id, "token": token}
        for name, owner in (("first", "member"), ("second", "other_member")):
            account = Account(
                name=f"selection {name}", app_key=f"selection-{uuid.uuid4().hex}",
                app_secret_enc="not-decryptable-for-selection",
                app_id_hint=f"{name}-key", secret_hint="hint", site="MY",
                owner_operator_id=identities[owner]["id"],
                bound_by_operator_id=identities["teacher"]["id"],
                bound_at=100, last_used=100, last_verified_at=100,
                shops_synced_at=100, status="active", is_default=False,
                shops=json.dumps([{"shopId": f"{name}-shop", "name": name,
                                   "site": "MY", "siteName": "Malaysia",
                                   "languages": ["en"], "status": "active"}]),
            )
            db.add(account)
            db.flush()
            db.add(AccountBinding(
                account_id=account.id, operator_id=identities[owner]["id"],
                bound_by_operator_id=identities["teacher"]["id"], bound_at=100))
            identities[name] = str(account.id)
        member_second = Account(
            name="selection member second",
            app_key=f"selection-{uuid.uuid4().hex}",
            app_secret_enc="not-decryptable-for-selection",
            app_id_hint="member-second-key", secret_hint="hint", site="MY",
            owner_operator_id=identities["member"]["id"],
            bound_by_operator_id=identities["teacher"]["id"],
            bound_at=200, last_used=200, last_verified_at=100,
            shops_synced_at=100, status="active", is_default=False,
            shops=json.dumps([{"shopId": "member-second-shop", "name": "member second",
                               "site": "MY", "siteName": "Malaysia",
                               "languages": ["en"], "status": "active"}]),
        )
        db.add(member_second)
        db.flush()
        db.add(AccountBinding(
            account_id=member_second.id, operator_id=identities["member"]["id"],
            bound_by_operator_id=identities["teacher"]["id"], bound_at=200))
        identities["member_second"] = str(member_second.id)
        db.commit()
        return identities
    finally:
        db.close()


def _headers(identity):
    return {"Authorization": f"Bearer {identity['token']}"}


def _account_snapshot():
    db = SessionLocal()
    try:
        return {
            "accounts": [
                (row.id, row.app_key, row.app_secret_enc, row.owner_operator_id,
                 row.bound_by_operator_id, row.bound_at, row.is_default,
                 row.last_verified_at, row.last_used, row.shops, row.shops_synced_at)
                for row in db.query(Account).order_by(Account.id).all()
            ],
            "bindings": [
                (row.id, row.account_id, row.operator_id,
                 row.bound_by_operator_id, row.bound_at)
                for row in db.query(AccountBinding).order_by(AccountBinding.id).all()
            ],
        }
    finally:
        db.close()


@pytest.mark.parametrize("role", ["admin", "teacher"])
def test_select_existing_account_is_local_and_does_not_rebind(
        client, identities, monkeypatch, role):
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("Selecting a connection must not decrypt or call Miaoshou")

    monkeypatch.setattr(account_service.crypto, "decrypt", fail_if_called)
    monkeypatch.setattr(account_service, "MiaoshouClient", fail_if_called)
    before = _account_snapshot()
    headers = _headers(identities[role])
    response = client.post(API + "/auth/select",
                           json={"accountId": identities["second"]}, headers=headers)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["accountId"] == identities["second"]
    assert result["shops"][0]["shopId"] == "second-shop"
    assert result["autoScan"] is False
    assert "appSecret" not in result and "app_key" not in result
    assert _account_snapshot() == before

    status = client.get(API + "/auth/status", headers=headers)
    assert status.status_code == 200, status.text
    assert status.json()["accountId"] == identities["second"]
    assert [row["id"] for row in status.json()["accounts"] if row["isDefault"]] == [
        identities["second"]]


def test_teachers_keep_independent_selected_accounts(client, identities):
    for role, account in (("teacher", "first"), ("other_teacher", "second")):
        response = client.post(
            API + "/auth/select", json={"accountId": identities[account]},
            headers=_headers(identities[role]))
        assert response.status_code == 200, response.text
    for role, account in (("teacher", "first"), ("other_teacher", "second")):
        status = client.get(API + "/auth/status", headers=_headers(identities[role]))
        assert status.status_code == 200, status.text
        assert status.json()["accountId"] == identities[account]
        assert [row["id"] for row in status.json()["accounts"]
                if row["isDefault"]] == [identities[account]]


def test_member_selection_cannot_cross_binding_or_keep_revoked_access(
        client, identities):
    headers = _headers(identities["member"])
    own = client.post(API + "/auth/select", json={"accountId": identities["first"]},
                      headers=headers)
    assert own.status_code == 200, own.text
    foreign = client.post(API + "/auth/select",
                          json={"accountId": identities["second"]}, headers=headers)
    assert foreign.status_code == 403, foreign.text
    unbound = client.post(API + "/auth/select",
                          json={"accountId": identities["first"]},
                          headers=_headers(identities["unbound"]))
    assert unbound.status_code == 403, unbound.text
    status = client.get(API + "/auth/status", headers=headers)
    assert status.json()["accountId"] == identities["first"]
    assert {row["id"] for row in status.json()["accounts"]} == {
        identities["first"], identities["member_second"]}

    db = SessionLocal()
    try:
        binding = db.query(AccountBinding).filter(
            AccountBinding.operator_id == identities["member"]["id"],
            AccountBinding.account_id == int(identities["first"])).one()
        db.delete(binding)
        account = db.get(Account, int(identities["first"]))
        account.owner_operator_id = identities["teacher"]["id"]
        db.commit()
    finally:
        db.close()
    revoked = client.get(API + "/auth/status", headers=headers)
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["connected"] is True
    assert revoked.json()["accountId"] == identities["member_second"]
    retry = client.post(API + "/auth/select", json={"accountId": identities["first"]},
                        headers=headers)
    assert retry.status_code == 403, retry.text


def test_member_can_switch_between_multiple_own_bindings(client, identities):
    headers = _headers(identities["member"])
    for account_name, expected_shop in (
        ("first", "first-shop"), ("member_second", "member-second-shop"),
    ):
        selected = client.post(
            API + "/auth/select",
            json={"accountId": identities[account_name]},
            headers=headers,
        )
        assert selected.status_code == 200, selected.text
        assert selected.json()["shops"][0]["shopId"] == expected_shop
        status = client.get(API + "/auth/status", headers=headers)
        assert status.status_code == 200, status.text
        assert status.json()["accountId"] == identities[account_name]
        assert {row["id"] for row in status.json()["accounts"]} == {
            identities["first"], identities["member_second"]}


def test_invalid_selection_preserves_last_valid_choice(client, identities):
    headers = _headers(identities["teacher"])
    selected = client.post(API + "/auth/select",
                           json={"accountId": identities["first"]}, headers=headers)
    assert selected.status_code == 200, selected.text
    db = SessionLocal()
    try:
        account = db.get(Account, int(identities["second"]))
        account.status = "invalid"
        db.commit()
    finally:
        db.close()
    for account_id, expected in ((identities["second"], 400), ("999999999", 404)):
        response = client.post(API + "/auth/select", json={"accountId": account_id},
                               headers=headers)
        assert response.status_code == expected, response.text
    status = client.get(API + "/auth/status", headers=headers)
    assert status.json()["accountId"] == identities["first"]


def test_connect_new_account_updates_previous_personal_selection(
        client, identities, monkeypatch):
    class FakeMiaoshouClient:
        def __init__(self, *_args):
            pass

        def shop_list(self, **_kwargs):
            return {"data": {"shopList": []}}

        def close(self):
            pass

    monkeypatch.setattr(account_service, "MiaoshouClient", FakeMiaoshouClient)
    headers = _headers(identities["teacher"])
    selected = client.post(API + "/auth/select",
                           json={"accountId": identities["first"]}, headers=headers)
    assert selected.status_code == 200, selected.text
    response = client.post(
        API + "/auth/connect", headers=headers,
        json={"appId": f"selection-new-{uuid.uuid4().hex}",
              "appSecret": "selection-new-secret", "autoScan": False})
    assert response.status_code == 200, response.text
    status = client.get(API + "/auth/status", headers=headers)
    assert status.json()["accountId"] == response.json()["accountId"]
    assert status.json()["accountId"] != identities["first"]
