"""Learner automation stays private even when a Miaoshou key is shared."""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from core import errors
from db.session import SessionLocal, init_db
from main import app
from models.account import Account
from models.account_binding import AccountBinding
from models.product import Product
from services import account_service, operator_service, warehouse_service

API = "/api/v1"


@pytest.fixture
def identities():
    init_db()
    suffix = uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        members = [operator_service.create(
            db, f"member-automation-{suffix}-{i}", role=operator_service.ROLE_MEMBER)
            for i in range(2)]
        account = Account(name=f"automation-shared-{suffix}", shops="[]", is_default=False)
        foreign = Account(name=f"automation-foreign-{suffix}", shops="[]", is_default=False)
        db.add_all([account, foreign])
        db.flush()
        db.add_all([AccountBinding(account_id=account.id, operator_id=member.id)
                    for member, _token in members])
        db.commit()
        return {
            "members": [(member.id, {"Authorization": f"Bearer {token}"})
                        for member, token in members],
            "account_id": account.id, "foreign_id": foreign.id,
        }


@pytest.fixture
def client():
    with TestClient(app, raise_server_exceptions=False) as instance:
        yield instance


def _create_run(client, identities, headers, *, account_id=None):
    detail_id = str(uuid.uuid4().int % 10**12)
    return client.post(API + "/automation-runs", headers=headers, json={
        "accountId": str(account_id or identities["account_id"]), "shopId": "22",
        "settings": {"textEnabled": True, "imagesEnabled": False, "publishEnabled": False},
        "items": [{"detailId": detail_id, "shopId": "22", "title": "Raw item"}],
    })


def test_member_can_process_own_run_and_shared_key_runs_stay_private(client, identities):
    (member_id, first_auth), (_other_id, second_auth) = identities["members"]
    created = _create_run(client, identities, first_auth)
    assert created.status_code == 201, created.text
    run = created.json()["run"]
    item = run["items"][0]
    item_url = API + f"/automation-runs/{run['id']}/items/{item['id']}"
    assert client.post(item_url + "/start", headers=first_auth).status_code == 200
    with SessionLocal() as db:
        product = Product(
            warehouse_key=f"{item['detailId']}:22", account_id=identities["account_id"],
            detail_id=item["detailId"], shop_id="22", title="Processed item",
            status="ready", operator_id=member_id, automation_result=True,
            draft_verified_revision=1, payload={}, collected={},
        )
        db.add(product)
        db.commit()
        product_id = str(product.id)
    processed = client.post(item_url + "/process-result", headers=first_auth, json={
        "status": "succeeded", "productId": product_id,
        "result": {"images": {"failed": 0}},
    })
    assert processed.status_code == 200, processed.text
    assert processed.json()["run"]["status"] == "completed"
    assert processed.json()["run"]["items"][0]["processResult"]["draftVerified"] is True

    own_latest = client.get(API + "/automation-runs/latest", headers=first_auth,
                            params={"accountId": identities["account_id"]})
    other_latest = client.get(API + "/automation-runs/latest", headers=second_auth,
                              params={"accountId": identities["account_id"]})
    assert own_latest.json()["run"]["id"] == run["id"]
    assert other_latest.json()["run"] is None
    assert client.get(API + f"/automation-runs/{run['id']}",
                      headers=second_auth).status_code == 403
    assert client.post(item_url + "/start", headers=second_auth).status_code == 403
    assert client.post(API + f"/automation-runs/{run['id']}/recover",
                       headers=second_auth).status_code == 403


def test_member_cannot_link_other_users_product_to_own_run(client, identities):
    (_member_id, auth), (other_id, _other_auth) = identities["members"]
    created = _create_run(client, identities, auth)
    assert created.status_code == 201, created.text
    run = created.json()["run"]
    item = run["items"][0]
    item_url = API + f"/automation-runs/{run['id']}/items/{item['id']}"
    assert client.post(item_url + "/start", headers=auth).status_code == 200
    with SessionLocal() as db:
        product = Product(
            warehouse_key=f"{item['detailId']}:22", account_id=identities["account_id"],
            detail_id=item["detailId"], shop_id="22", title="Someone else's result",
            operator_id=other_id, automation_result=True, status="ready",
            draft_verified_revision=1, payload={}, collected={},
        )
        db.add(product)
        db.commit()
        product_id = str(product.id)
    response = client.post(item_url + "/process-result", headers=auth, json={
        "status": "succeeded", "productId": product_id,
    })
    assert response.status_code == 403, response.text


def test_member_cannot_create_or_process_foreign_account(client, identities, monkeypatch):
    _member_id, auth = identities["members"][0]
    created = _create_run(client, identities, auth, account_id=identities["foreign_id"])
    assert created.status_code == 403, created.text

    def unexpected_upstream(_account):
        pytest.fail("Account isolation must reject before opening an upstream client")

    monkeypatch.setattr(account_service, "account_client", unexpected_upstream)
    processed = client.post(API + "/collect-box/123/process", headers=auth, json={
        "accountId": str(identities["foreign_id"]), "shopId": "22", "save": True,
    })
    assert processed.status_code == 403, processed.text


def test_member_process_endpoint_preserves_operator_ownership(client, identities, monkeypatch):
    member_id, auth = identities["members"][0]
    received = []

    class FakeClient:
        def close(self):
            pass

    def save(_db, account, _client, **kwargs):
        received.append((account.id, kwargs))
        return {"id": "1", "title": "Processed"}

    monkeypatch.setattr(account_service, "account_client", lambda _account: FakeClient())
    monkeypatch.setattr(warehouse_service, "save_from_collect_box", save)
    processed = client.post(API + "/collect-box/123/process", headers=auth, json={
        "accountId": str(identities["account_id"]), "shopId": "22", "save": True,
    })
    assert processed.status_code == 200, processed.text
    assert received[0][0] == identities["account_id"]
    assert received[0][1]["operator_id"] == member_id
    assert received[0][1]["automation_result"] is True


@pytest.mark.parametrize("shared_product", [False, True])
def test_member_process_cannot_overwrite_other_result_or_shared_warehouse(
        identities, shared_product):
    (member_id, _auth), (other_id, _other_auth) = identities["members"]
    with SessionLocal() as db:
        account = db.get(Account, identities["account_id"])
        existing = Product(
            warehouse_key="123:22", account_id=account.id, detail_id="123", shop_id="22",
            title="Protected result", status="ready", operator_id=other_id,
            automation_result=not shared_product, payload={}, collected={},
        )
        db.add(existing)
        db.flush()

        class UnexpectedClient:
            def shop_detail(self, *_args):
                pytest.fail("Ownership must be checked before any upstream read/write")

        with pytest.raises(errors.AppError) as exc:
            warehouse_service.save_from_collect_box(
                db, account, UnexpectedClient(), detail_id="123", shop_id="22",
                operator_id=member_id, automation_result=True,
            )
        assert exc.value.status == 403
        assert existing.title == "Protected result"
        assert existing.operator_id == other_id
