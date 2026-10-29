from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from api import reference
from api import system as system_api
from core.errors import AppError
from db.session import SessionLocal, init_db
from main import app
from models.account import Account
from models.batch import Batch
from models.image_job import ImageJob
from models.product import Product
from models.publish_log import PublishLog
from services import account_service, image_service, operator_service, setting_service

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
        builtin = operator_service.ensure_default(db)
        admin, admin_token = operator_service.create(
            db, f"owner-admin-{uuid.uuid4().hex[:8]}",
            role=operator_service.ROLE_ADMIN)
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
            "admin_token": admin_token,
            "admin_id": builtin.id,
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


def test_member_reads_are_scoped_to_owned_account(client, identities):
    db = SessionLocal()
    try:
        own = (db.query(Account)
               .filter(Account.owner_operator_id == identities["first_id"])
               .order_by(Account.last_used.desc().nullslast(), Account.id.desc())
               .first())
        if own is None:
            own = Account(
                name="read scope own", app_key="read-own-key", app_secret_enc="",
                owner_operator_id=identities["first_id"], shops="[]", is_default=False,
            )
            db.add(own)
        foreign = Account(
            name="read scope foreign", app_key="read-foreign-key", app_secret_enc="",
            owner_operator_id=identities["second_id"], shops="[]", is_default=False,
        )
        db.add(foreign)
        db.flush()
        own_log = PublishLog(account_id=own.id, detail_id="own-detail",
                             shop_id="own-shop", title="own publish")
        foreign_log = PublishLog(account_id=foreign.id, detail_id="foreign-detail",
                                 shop_id="foreign-shop", title="foreign publish")
        own_product = Product(
            warehouse_key=f"scope-own-{uuid.uuid4().hex[:10]}",
            detail_id="scope-own", shop_id="shop-own", account_id=own.id,
            payload={"sourceItemId": "190001"},
        )
        foreign_product = Product(
            warehouse_key=f"scope-foreign-{uuid.uuid4().hex[:10]}",
            detail_id="scope-foreign", shop_id="shop-foreign", account_id=foreign.id,
            payload={"sourceItemId": "190002"},
        )
        db.add_all([own_log, foreign_log, own_product, foreign_product])
        setting_service.select_template(db, "own-template", account=str(own.id))
        setting_service.select_template(db, "foreign-template", account=str(foreign.id))
        db.commit()
        own_id, foreign_id = own.id, foreign.id
    finally:
        db.close()

    member_headers = _headers(identities["first_token"])
    foreign_logs = client.get(
        API + "/publish/logs", params={"accountId": str(foreign_id)},
        headers=member_headers)
    own_logs = client.get(API + "/publish/logs", headers=member_headers)
    assert foreign_logs.status_code == 403, foreign_logs.text
    assert own_logs.status_code == 200, own_logs.text
    assert "own publish" in {row["title"] for row in own_logs.json()["items"]}
    assert "foreign publish" not in {
        row["title"] for row in own_logs.json()["items"]}

    foreign_sources = client.get(
        API + "/warehouse/source-item-ids", params={"accountId": str(foreign_id)},
        headers=member_headers)
    own_sources = client.get(API + "/warehouse/source-item-ids", headers=member_headers)
    assert foreign_sources.status_code == 403, foreign_sources.text
    assert own_sources.status_code == 200, own_sources.text
    assert "190001" in own_sources.json()["ids"]
    assert "190002" not in own_sources.json()["ids"]

    foreign_templates = client.get(
        API + "/system/templates", params={"accountId": str(foreign_id)},
        headers=member_headers)
    own_templates = client.get(API + "/system/templates", headers=member_headers)
    foreign_preview = client.post(
        API + "/system/price-preview",
        json={"purchaseCny": 10, "weightKg": 1,
              "accountId": str(foreign_id)},
        headers=member_headers)
    assert foreign_templates.status_code == 403, foreign_templates.text
    assert own_templates.status_code == 200, own_templates.text
    assert str(foreign_id) not in own_templates.json()["selected"]
    assert foreign_preview.status_code == 403, foreign_preview.text

    teacher_headers = _headers(identities["teacher_token"])
    teacher_logs = client.get(API + "/publish/logs", headers=teacher_headers)
    teacher_sources = client.get(API + "/warehouse/source-item-ids",
                                 headers=teacher_headers)
    assert teacher_logs.status_code == 200, teacher_logs.text
    assert {"own publish", "foreign publish"} <= {
        row["title"] for row in teacher_logs.json()["items"]}
    assert {"190001", "190002"} <= set(teacher_sources.json()["ids"])
    teacher_templates = client.get(API + "/system/templates",
                                   headers=teacher_headers)
    assert {str(own_id), str(foreign_id)} <= set(
        teacher_templates.json()["selected"])
    assert own_id != foreign_id


def test_member_price_defaults_are_scoped_to_owned_account(
        client, identities, monkeypatch):
    db = SessionLocal()
    try:
        own = Account(
            name="price defaults own", app_key="price-own-key", app_secret_enc="",
            owner_operator_id=identities["first_id"], shops="[]", is_default=False,
        )
        foreign = Account(
            name="price defaults foreign", app_key="price-foreign-key",
            app_secret_enc="", owner_operator_id=identities["second_id"],
            shops="[]", is_default=False,
        )
        db.add_all([own, foreign])
        db.commit()
        own_id, foreign_id = str(own.id), str(foreign.id)
    finally:
        db.close()

    class FakeClient:
        def close(self):
            pass

    monkeypatch.setattr(system_api, "account_client", lambda _account: FakeClient())
    monkeypatch.setattr(
        reference, "cached",
        lambda _account, _key, _loader, *, refresh: ({"data": {}}, False, False, 0),
    )
    headers = _headers(identities["first_token"])

    own_response = client.get(
        API + "/system/price-defaults", params={"accountId": own_id},
        headers=headers)
    foreign_response = client.get(
        API + "/system/price-defaults", params={"accountId": foreign_id},
        headers=headers)

    assert own_response.status_code == 200, own_response.text
    assert own_response.json()["accountId"] == own_id
    assert foreign_response.status_code == 403, foreign_response.text


def test_member_bootstrap_hides_other_members_accounts_and_batches(client, identities):
    db = SessionLocal()
    try:
        own_account = Account(
            name="bootstrap own", app_key="bootstrap-own", app_secret_enc="",
            owner_operator_id=identities["first_id"], shops="[]", is_default=False,
        )
        foreign_account = Account(
            name="bootstrap foreign", app_key="bootstrap-foreign", app_secret_enc="",
            owner_operator_id=identities["second_id"], shops="[]", is_default=False,
        )
        db.add_all([own_account, foreign_account])
        db.flush()
        own_account_id = str(own_account.id)
        foreign_account_id = str(foreign_account.id)
        own_batch_id = f"bootstrap-own-{uuid.uuid4().hex[:8]}"
        foreign_batch_id = f"bootstrap-foreign-{uuid.uuid4().hex[:8]}"
        db.add_all([
            Batch(id=own_batch_id,
                  account_id=own_account.id, operator_id=identities["first_id"],
                  shop_id="own-shop", detail_ids=["101"], settings={}, results=[]),
            Batch(id=foreign_batch_id,
                  account_id=foreign_account.id, operator_id=identities["second_id"],
                  shop_id="foreign-shop", detail_ids=["202"], settings={}, results=[]),
        ])
        db.commit()
    finally:
        db.close()

    response = client.get(API + "/system/bootstrap",
                          headers=_headers(identities["first_token"]))
    assert response.status_code == 200, response.text
    body = response.json()
    visible_accounts = {row["id"] for row in body["accounts"]}
    assert own_account_id in visible_accounts
    assert foreign_account_id not in visible_accounts
    visible_batches = {row["id"] for row in body["batches"]["items"]}
    assert own_batch_id in visible_batches
    assert foreign_batch_id not in visible_batches

    teacher_response = client.get(
        API + "/system/bootstrap", headers=_headers(identities["teacher_token"]))
    assert teacher_response.status_code == 200, teacher_response.text
    teacher_batches = {row["id"] for row in teacher_response.json()["batches"]["items"]}
    assert {own_batch_id, foreign_batch_id} <= teacher_batches


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


def test_teacher_can_assign_legacy_unowned_key_to_student(client, identities, monkeypatch):
    monkeypatch.setattr(account_service, "MiaoshouClient", FakeMiaoshouClient)
    db = SessionLocal()
    try:
        legacy = Account(
            name="unowned legacy key", app_key="legacy-owned-key",
            app_secret_enc=account_service.crypto.encrypt("legacy-secret"),
            app_id_hint="key", secret_hint="cret", status="active", shops="[]",
            is_default=False,
        )
        db.add(legacy)
        db.commit()
        legacy_id = str(legacy.id)
    finally:
        db.close()

    response = client.post(
        API + "/auth/connect",
        json={"accountId": legacy_id,
              "studentOperatorId": str(identities["second_id"]),
              "autoScan": False},
        headers=_headers(identities["teacher_token"]),
    )
    assert response.status_code == 200, response.text
    assert response.json()["ownerOperatorId"] == str(identities["second_id"])
    assert response.json()["boundByOperatorId"] == str(identities["teacher_id"])


def test_app_key_cannot_be_bound_to_multiple_students_but_owner_can_reconnect(
        client, identities, monkeypatch):
    monkeypatch.setattr(account_service, "MiaoshouClient", FakeMiaoshouClient)
    app_key = f"shared-key-{uuid.uuid4().hex[:8]}"
    app_secret = "private-app-secret"

    first = client.post(
        API + "/auth/connect",
        json={"appId": app_key, "appSecret": app_secret, "autoScan": False},
        headers=_headers(identities["first_token"]),
    )
    assert first.status_code == 200, first.text
    first_account_id = first.json()["accountId"]

    duplicate = client.post(
        API + "/auth/connect",
        json={"appId": app_key, "appSecret": app_secret, "autoScan": False},
        headers=_headers(identities["second_token"]),
    )
    assert duplicate.status_code == 409, duplicate.text
    assert app_secret not in duplicate.text

    reconnected = client.post(
        API + "/auth/connect",
        json={"appId": app_key, "appSecret": app_secret, "autoScan": False},
        headers=_headers(identities["first_token"]),
    )
    assert reconnected.status_code == 200, reconnected.text
    assert reconnected.json()["accountId"] == first_account_id
    assert reconnected.json()["ownerOperatorId"] == str(identities["first_id"])

    db = SessionLocal()
    try:
        assert db.query(Account).filter(Account.app_key == app_key).count() == 1
    finally:
        db.close()


def test_teacher_cannot_reassign_another_students_bound_key(client, identities, monkeypatch):
    monkeypatch.setattr(account_service, "MiaoshouClient", FakeMiaoshouClient)
    app_key = f"owned-key-{uuid.uuid4().hex[:8]}"
    app_secret = "private-app-secret"
    connected = client.post(
        API + "/auth/connect",
        json={"appId": app_key, "appSecret": app_secret, "autoScan": False},
        headers=_headers(identities["first_token"]),
    )
    assert connected.status_code == 200, connected.text

    response = client.post(
        API + "/auth/connect",
        json={"accountId": connected.json()["accountId"],
              "appId": app_key, "appSecret": app_secret,
              "studentOperatorId": str(identities["second_id"]),
              "autoScan": False},
        headers=_headers(identities["teacher_token"]),
    )
    assert response.status_code == 403, response.text
    assert app_secret not in response.text

    db = SessionLocal()
    try:
        account = db.query(Account).filter(Account.app_key == app_key).one()
        assert account.owner_operator_id == identities["first_id"]
    finally:
        db.close()


def test_image_jobs_and_result_files_are_isolated_by_operator(client, identities):
    db = SessionLocal()
    try:
        own_id = uuid.uuid4().hex
        foreign_id = uuid.uuid4().hex
        db.add_all([
            ImageJob(id=own_id, operator_id=identities["first_id"],
                     batch_id=f"own-{own_id[:8]}", status="ready"),
            ImageJob(id=foreign_id, operator_id=identities["second_id"],
                     batch_id=f"other-{foreign_id[:8]}", status="ready"),
        ])
        db.commit()
    finally:
        db.close()

    first_headers = _headers(identities["first_token"])
    jobs = client.get(API + "/images/jobs", headers=first_headers)
    assert jobs.status_code == 200, jobs.text
    visible_ids = {row["id"] for row in jobs.json()["items"]}
    assert own_id in visible_ids
    assert foreign_id not in visible_ids

    own_detail = client.get(API + f"/images/jobs/{own_id}", headers=first_headers)
    foreign_detail = client.get(API + f"/images/jobs/{foreign_id}", headers=first_headers)
    foreign_file = client.get(
        API + f"/images/jobs/{foreign_id}/file", headers=first_headers)
    assert own_detail.status_code == 200, own_detail.text
    assert foreign_detail.status_code == 404, foreign_detail.text
    assert foreign_file.status_code == 404, foreign_file.text

    teacher_jobs = client.get(
        API + "/images/jobs", headers=_headers(identities["teacher_token"]))
    assert teacher_jobs.status_code == 200, teacher_jobs.text
    teacher_ids = {row["id"] for row in teacher_jobs.json()["items"]}
    assert {own_id, foreign_id} <= teacher_ids


def test_member_cannot_reuse_another_operators_image_translation(client, identities,
                                                                 monkeypatch):
    del client
    raw = b"same-image-content"
    monkeypatch.setattr(image_service, "_api_key", lambda: "configured")
    monkeypatch.setattr(image_service, "_fetch_source", lambda _url: raw)
    monkeypatch.setattr(image_service, "_shrink", lambda data: data)
    monkeypatch.setattr(image_service, "_image_type", lambda _data: "png")
    digest = image_service.image_job_id(
        raw, image_service.IMAGE_MODEL, image_service.TRANSLATION_PROMPT, "1:1")
    db = SessionLocal()
    try:
        db.add(ImageJob(id=digest, operator_id=identities["second_id"],
                        batch_id="foreign-image", status="ready"))
        db.commit()
        with pytest.raises(AppError) as caught:
            image_service.translate(
                    db, {"items": [{"url": "https://example.test/image.png",
                                    "source": "main"}]},
                operator_id=identities["first_id"],
                owner_operator_id=identities["first_id"],
            )
        assert caught.value.status == 403
    finally:
        db.close()
