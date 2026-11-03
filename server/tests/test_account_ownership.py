from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from api import reference
from api import system as system_api
from core.errors import AppError
from db.session import SessionLocal, init_db
from main import app
from models.account import Account
from models.account_binding import AccountBinding
from models.automation_run import AutomationRun, AutomationRunItem
from models.batch import Batch
from models.image_job import ImageJob
from models.product import Product
from models.publish_log import PublishLog
from services import account_service, image_service, operator_service, setting_service
from services.miaoshou import MiaoshouError

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
    assert "ownerOperatorId" not in first.json()
    assert "boundByOperatorId" not in first.json()

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
    assert all("ownerOperatorId" not in row and "boundByOperatorId" not in row
               for row in first_status.json()["accounts"])

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
    admin_accounts = {row["id"]: row for row in admin_status.json()["accounts"]}
    assert {first_account_id, second_account_id} <= admin_accounts.keys()
    first_summary = admin_accounts[first_account_id]
    assert first_summary["status"] == "active"
    assert first_summary["ownerOperatorId"] == str(identities["first_id"])
    assert first_summary["boundByOperatorId"] == str(identities["first_id"])
    assert first_summary["appIdHint"].endswith("ey-a")
    assert first_summary["secretHint"].endswith("et-a")
    assert all("appSecret" not in row and "app_secret_enc" not in row
               for row in admin_accounts.values())

    teacher_status = client.get(API + "/auth/accounts",
                                headers=_headers(identities["teacher_token"]))
    teacher_accounts = {row["id"]: row for row in teacher_status.json()["accounts"]}
    assert {first_account_id, second_account_id} <= teacher_accounts.keys()
    assert teacher_accounts[first_account_id]["status"] == "active"
    assert teacher_accounts[first_account_id]["ownerOperatorId"] == str(identities["first_id"])
    assert teacher_accounts[first_account_id]["boundByOperatorId"] == str(identities["first_id"])
    assert teacher_accounts[first_account_id]["secretHint"].endswith("et-a")
    assert all("appSecret" not in row and "app_secret_enc" not in row
               for row in teacher_accounts.values())


def test_unbound_member_does_not_inherit_legacy_default_account(client, identities):
    response = client.get(API + "/auth/status",
                          headers=_headers(identities["unbound_token"]))
    assert response.status_code == 200, response.text
    assert response.json()["connected"] is False
    assert response.json()["accountId"] is None
    assert response.json()["accounts"] == []


def test_member_explicit_binding_blocks_legacy_owner_fallback(client, identities):
    """Once a member has a binding, owner_operator_id cannot expose old extras."""
    db = SessionLocal()
    try:
        member, member_token = operator_service.create(
            db, f"legacy-fallback-{uuid.uuid4().hex[:8]}",
            role=operator_service.ROLE_MEMBER)
        bound = Account(
            name=f"bound-account-{uuid.uuid4().hex[:8]}",
            app_key=f"bound-key-{uuid.uuid4().hex[:8]}",
            app_secret_enc="", owner_operator_id=member.id, shops="[]",
            is_default=False,
        )
        legacy_extra = Account(
            name=f"legacy-extra-{uuid.uuid4().hex[:8]}",
            app_key=f"legacy-extra-key-{uuid.uuid4().hex[:8]}",
            app_secret_enc="", owner_operator_id=member.id, shops="[]",
            is_default=False,
        )
        db.add_all([bound, legacy_extra])
        db.flush()
        db.add(AccountBinding(account_id=bound.id, operator_id=member.id))
        db.commit()

        assert account_service.has_account_access(db, bound.id, member.id) is True
        assert account_service.has_account_access(db, legacy_extra.id, member.id) is False
        bound_id, legacy_extra_id = str(bound.id), str(legacy_extra.id)
    finally:
        db.rollback()
        db.close()

    status = client.get(API + "/auth/status", headers=_headers(member_token))
    assert status.status_code == 200, status.text
    assert [row["id"] for row in status.json()["accounts"]] == [bound_id]
    forbidden = client.get(
        API + "/shops", params={"accountId": legacy_extra_id},
        headers=_headers(member_token))
    assert forbidden.status_code == 403, forbidden.text


def test_member_reads_are_scoped_to_owned_account(client, identities):
    db = SessionLocal()
    try:
        own_operator, own_token = operator_service.create(
            db, f"read-scope-own-{uuid.uuid4().hex[:8]}",
            role=operator_service.ROLE_MEMBER)
        foreign_operator, _ = operator_service.create(
            db, f"read-scope-foreign-{uuid.uuid4().hex[:8]}",
            role=operator_service.ROLE_MEMBER)
        own = Account(
            name="read scope own", app_key=f"read-own-{uuid.uuid4().hex[:8]}",
            app_secret_enc="", owner_operator_id=own_operator.id,
            shops="[]", is_default=False,
        )
        foreign = Account(
            name="read scope foreign", app_key=f"read-foreign-{uuid.uuid4().hex[:8]}",
            app_secret_enc="", owner_operator_id=foreign_operator.id,
            shops="[]", is_default=False,
        )
        db.add_all([own, foreign])
        db.flush()
        db.add_all([
            AccountBinding(account_id=own.id, operator_id=own_operator.id),
            AccountBinding(account_id=foreign.id, operator_id=foreign_operator.id),
        ])
        own_log = PublishLog(account_id=own.id, detail_id="own-detail",
                             shop_id="own-shop", title="own publish",
                             operator_id=own_operator.id)
        foreign_log = PublishLog(account_id=foreign.id, detail_id="foreign-detail",
                                 shop_id="foreign-shop", title="foreign publish",
                                 operator_id=foreign_operator.id)
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

    member_headers = _headers(own_token)
    foreign_logs = client.get(
        API + "/publish/logs", params={"accountId": str(foreign_id)},
        headers=member_headers)
    own_logs = client.get(
        API + "/publish/logs", params={"accountId": str(own_id)},
        headers=member_headers)
    assert foreign_logs.status_code == 403, foreign_logs.text
    assert own_logs.status_code == 200, own_logs.text
    assert "own publish" in {row["title"] for row in own_logs.json()["items"]}
    assert "foreign publish" not in {
        row["title"] for row in own_logs.json()["items"]}

    foreign_sources = client.get(
        API + "/warehouse/source-item-ids", params={"accountId": str(foreign_id)},
        headers=member_headers)
    own_sources = client.get(
        API + "/warehouse/source-item-ids", params={"accountId": str(own_id)},
        headers=member_headers)
    assert foreign_sources.status_code == 403, foreign_sources.text
    assert own_sources.status_code == 200, own_sources.text
    assert "190001" in own_sources.json()["ids"]
    assert "190002" not in own_sources.json()["ids"]

    foreign_templates = client.get(
        API + "/system/templates", params={"accountId": str(foreign_id)},
        headers=member_headers)
    own_templates = client.get(
        API + "/system/templates", params={"accountId": str(own_id)},
        headers=member_headers)
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
        client, monkeypatch):
    db = SessionLocal()
    try:
        own_operator, own_token = operator_service.create(
            db, f"price-own-{uuid.uuid4().hex[:8]}",
            role=operator_service.ROLE_MEMBER)
        foreign_operator, _ = operator_service.create(
            db, f"price-foreign-{uuid.uuid4().hex[:8]}",
            role=operator_service.ROLE_MEMBER)
        own = Account(
            name="price defaults own", app_key="price-own-key", app_secret_enc="",
            owner_operator_id=own_operator.id, shops="[]", is_default=False,
        )
        foreign = Account(
            name="price defaults foreign", app_key="price-foreign-key",
            app_secret_enc="", owner_operator_id=foreign_operator.id,
            shops="[]", is_default=False,
        )
        db.add_all([own, foreign])
        db.flush()
        db.add_all([
            AccountBinding(account_id=own.id, operator_id=own_operator.id),
            AccountBinding(account_id=foreign.id, operator_id=foreign_operator.id),
        ])
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
    headers = _headers(own_token)

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
        own_operator, own_token = operator_service.create(
            db, f"bootstrap-own-{uuid.uuid4().hex[:8]}",
            role=operator_service.ROLE_MEMBER)
        foreign_operator, _ = operator_service.create(
            db, f"bootstrap-foreign-{uuid.uuid4().hex[:8]}",
            role=operator_service.ROLE_MEMBER)
        own_account = Account(
            name="bootstrap own", app_key="bootstrap-own", app_secret_enc="",
            # The account belongs to the teacher and is explicitly shared with
            # this member, so removing the binding must revoke batch visibility.
            owner_operator_id=identities["teacher_id"], shops="[]", is_default=False,
        )
        foreign_account = Account(
            name="bootstrap foreign", app_key="bootstrap-foreign", app_secret_enc="",
            owner_operator_id=foreign_operator.id, shops="[]", is_default=False,
        )
        db.add_all([own_account, foreign_account])
        db.flush()
        own_account_id = str(own_account.id)
        foreign_account_id = str(foreign_account.id)
        db.add_all([
            AccountBinding(account_id=own_account.id, operator_id=own_operator.id),
            AccountBinding(account_id=foreign_account.id,
                           operator_id=foreign_operator.id),
        ])
        own_batch_id = f"bootstrap-own-{uuid.uuid4().hex[:8]}"
        foreign_batch_id = f"bootstrap-foreign-{uuid.uuid4().hex[:8]}"
        db.add_all([
            Batch(id=own_batch_id,
                  account_id=own_account.id, operator_id=own_operator.id,
                  shop_id="own-shop", detail_ids=["101"], settings={}, results=[]),
            Batch(id=foreign_batch_id,
                  account_id=foreign_account.id, operator_id=foreign_operator.id,
                  shop_id="foreign-shop", detail_ids=["202"], settings={}, results=[]),
        ])
        db.commit()
    finally:
        db.close()

    db = SessionLocal()
    try:
        previous_selection = setting_service.get(
            db, setting_service.PRICING_SELECTED_KEY)
        selected = setting_service.selected_templates(db)
        selected[own_account_id] = "own template"
        selected[foreign_account_id] = "foreign template"
        setting_service.put(db, setting_service.PRICING_SELECTED_KEY,
                            {"accounts": selected})
        db.commit()
    finally:
        db.close()

    try:
        response = client.get(API + "/system/bootstrap",
                              headers=_headers(own_token))
        assert response.status_code == 200, response.text
        body = response.json()
        visible_accounts = {row["id"] for row in body["accounts"]}
        assert own_account_id in visible_accounts
        assert foreign_account_id not in visible_accounts
        visible_batches = {row["id"] for row in body["batches"]["items"]}
        assert own_batch_id in visible_batches
        assert foreign_batch_id not in visible_batches
        assert body["templates"]["selected"].get(own_account_id) == "own template"
        assert foreign_account_id not in body["templates"]["selected"]

        db = SessionLocal()
        try:
            db.query(AccountBinding).filter(
                AccountBinding.account_id == int(own_account_id),
                AccountBinding.operator_id == own_operator.id,
            ).delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()

        unbound_response = client.get(API + "/system/bootstrap",
                                      headers=_headers(own_token))
        assert unbound_response.status_code == 200, unbound_response.text
        unbound_batches = {
            row["id"] for row in unbound_response.json()["batches"]["items"]
        }
        assert own_batch_id not in unbound_batches

        teacher_response = client.get(
            API + "/system/bootstrap",
            headers=_headers(identities["teacher_token"]))
        assert teacher_response.status_code == 200, teacher_response.text
        teacher_body = teacher_response.json()
        teacher_batches = {row["id"] for row in teacher_body["batches"]["items"]}
        assert {own_batch_id, foreign_batch_id} <= teacher_batches
        assert teacher_body["templates"]["selected"].get(
            foreign_account_id) == "foreign template"
    finally:
        db = SessionLocal()
        try:
            setting_service.put(db, setting_service.PRICING_SELECTED_KEY,
                                previous_selection)
            db.commit()
        finally:
            db.close()


@pytest.mark.parametrize("role", [operator_service.ROLE_TEACHER, operator_service.ROLE_ADMIN])
def test_staff_can_bind_own_key_in_shared_mode(client, monkeypatch, role):
    """Teachers and admins may bind multiple Miaoshou accounts to themselves."""
    monkeypatch.setattr(account_service, "MiaoshouClient", FakeMiaoshouClient)
    from core.config import get_settings as _gs
    real = _gs()

    class _Shared:
        shared_mode = True

        def __getattr__(self, name):
            return getattr(real, name)

    import api.auth as auth_module
    monkeypatch.setattr(auth_module, "get_settings", lambda: _Shared())

    suffix = uuid.uuid4().hex[:8]
    db = SessionLocal()
    try:
        owner, token = operator_service.create(db, f"self-bind-{role}-{suffix}", role=role)
        db.commit()
        owner_id = owner.id
    finally:
        db.close()

    response = client.post(
        API + "/auth/connect",
        json={"appId": f"key-staff-{suffix}", "appSecret": "secret-staff",
              "autoScan": False},
        headers=_headers(token),
    )
    assert response.status_code == 200, response.text
    assert response.json()["ownerOperatorId"] == str(owner_id)
    assert response.json()["boundByOperatorId"] == str(owner_id)

    second = client.post(
        API + "/auth/connect",
        json={"appId": f"key-staff-extra-{suffix}", "appSecret": "secret-extra",
              "autoScan": False},
        headers=_headers(token),
    )
    assert second.status_code == 200, second.text
    db = SessionLocal()
    try:
        bindings = db.query(AccountBinding).filter(AccountBinding.operator_id == owner_id).all()
        assert {row.account_id for row in bindings} == {
            int(response.json()["accountId"]), int(second.json()["accountId"])}
    finally:
        db.close()


def test_teacher_can_bind_key_for_student(client, identities, monkeypatch):
    monkeypatch.setattr(account_service, "MiaoshouClient", FakeMiaoshouClient)
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
              "studentOperatorId": str(identities["unbound_id"]),
              "autoScan": False},
        headers=_headers(identities["teacher_token"]),
    )
    assert response.status_code == 200, response.text
    assert response.json()["ownerOperatorId"] == str(identities["unbound_id"])
    assert response.json()["boundByOperatorId"] == str(identities["teacher_id"])


def test_app_key_and_students_can_have_multiple_bindings(
        client, identities, monkeypatch):
    monkeypatch.setattr(account_service, "MiaoshouClient", FakeMiaoshouClient)
    app_key = f"shared-key-{uuid.uuid4().hex[:8]}"
    app_secret = "private-app-secret"

    db = SessionLocal()
    try:
        first_operator, first_token = operator_service.create(
            db, f"owner-shared-first-{uuid.uuid4().hex[:8]}",
            role=operator_service.ROLE_MEMBER)
        db.commit()
        first_id = first_operator.id
    finally:
        db.close()

    first = client.post(
        API + "/auth/connect",
        json={"appId": app_key, "appSecret": app_secret, "autoScan": False},
        headers=_headers(first_token),
    )
    assert first.status_code == 200, first.text
    first_account_id = first.json()["accountId"]

    # 该模块前面的教师代绑用例会占用固定身份；为多人共享回归单独创建
    # 一个尚未绑定的学员，避免测试顺序改变语义。
    db = SessionLocal()
    try:
        extra, extra_token = operator_service.create(
            db, f"owner-shared-{uuid.uuid4().hex[:8]}",
            role=operator_service.ROLE_MEMBER)
        shared_account = db.query(Account).filter(
            Account.id == int(first_account_id)).one()
        shared_account.shops = '[{"shopId":"protected-shop","name":"keep"}]'
        shared_account.shops_synced_at = 123
        original_ciphertext = shared_account.app_secret_enc
        db.commit()
        extra_id = extra.id
    finally:
        db.close()

    wrong_secret = client.post(
        API + "/auth/connect",
        json={"appId": app_key, "appSecret": "different-secret", "autoScan": False},
        headers=_headers(extra_token),
    )
    assert wrong_secret.status_code == 403, wrong_secret.text

    # Knowing a saved account ID must not let an unbound student reuse the
    # server-side secret. Joining is allowed only when both submitted values
    # match the credentials that were originally verified and stored.
    account_id_only = client.post(
        API + "/auth/connect",
        json={"accountId": first_account_id, "autoScan": False},
        headers=_headers(extra_token),
    )
    wrong_key_for_id = client.post(
        API + "/auth/connect",
        json={"accountId": first_account_id, "appId": "wrong-key",
              "appSecret": app_secret, "autoScan": False},
        headers=_headers(extra_token),
    )
    wrong_secret_for_id = client.post(
        API + "/auth/connect",
        json={"accountId": first_account_id, "appId": app_key,
              "appSecret": "wrong-secret", "autoScan": False},
        headers=_headers(extra_token),
    )
    for denied in (account_id_only, wrong_key_for_id, wrong_secret_for_id):
        assert denied.status_code == 403, denied.text
        assert app_secret not in denied.text

    db = SessionLocal()
    try:
        unchanged = db.query(Account).filter(
            Account.id == int(first_account_id)).one()
        assert unchanged.app_secret_enc == original_ciphertext
        assert unchanged.status == "active"
        assert unchanged.shops == '[{"shopId":"protected-shop","name":"keep"}]'
        assert unchanged.shops_synced_at == 123
        assert db.query(AccountBinding).filter(
            AccountBinding.operator_id == extra_id).count() == 0
    finally:
        db.close()

    duplicate = client.post(
        API + "/auth/connect",
        json={"accountId": first_account_id, "appId": app_key,
              "appSecret": app_secret, "autoScan": False},
        headers=_headers(extra_token),
    )
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json()["accountId"] == first_account_id
    assert app_secret not in duplicate.text
    assert "ownerOperatorId" not in duplicate.json()
    assert "boundByOperatorId" not in duplicate.json()

    # The same student can join a second account by proving its credentials;
    # accounts remain individually bound and selectable.
    second_key = f"second-shared-key-{uuid.uuid4().hex[:8]}"
    second_secret = "second-shared-secret"
    db = SessionLocal()
    try:
        second_owner, second_owner_token = operator_service.create(
            db, f"owner-second-shared-{uuid.uuid4().hex[:8]}",
            role=operator_service.ROLE_MEMBER)
        db.commit()
    finally:
        db.close()
    second_account = client.post(
        API + "/auth/connect",
        json={"appId": second_key, "appSecret": second_secret,
              "autoScan": False},
        headers=_headers(second_owner_token),
    )
    assert second_account.status_code == 200, second_account.text
    second_account_id = second_account.json()["accountId"]
    second_binding = client.post(
        API + "/auth/connect",
        json={"accountId": second_account_id, "appId": second_key,
              "appSecret": second_secret, "autoScan": False},
        headers=_headers(extra_token),
    )
    assert second_binding.status_code == 200, second_binding.text
    assert second_binding.json()["accountId"] == second_account_id
    assert second_secret not in second_binding.text

    reconnected = client.post(
        API + "/auth/connect",
        json={"appId": app_key, "appSecret": app_secret, "autoScan": False},
        headers=_headers(first_token),
    )
    assert reconnected.status_code == 200, reconnected.text
    assert reconnected.json()["accountId"] == first_account_id
    assert "ownerOperatorId" not in reconnected.json()
    assert "boundByOperatorId" not in reconnected.json()

    # Learners see the shared account as their own connection, while its
    # ownership and binder metadata remain visible only to global managers.
    for token, expected_account_id, visible_account_ids in (
            (first_token, first_account_id, {first_account_id}),
            (extra_token, second_account_id,
             {first_account_id, second_account_id})):
        status = client.get(API + "/auth/status", headers=_headers(token))
        assert status.status_code == 200, status.text
        assert status.json()["accountId"] == expected_account_id
        assert {row["id"] for row in status.json()["accounts"]} == visible_account_ids
        assert all("ownerOperatorId" not in row and "boundByOperatorId" not in row
                   for row in status.json()["accounts"])

    teacher_accounts_response = client.get(
        API + "/auth/accounts", headers=_headers(identities["teacher_token"]))
    assert teacher_accounts_response.status_code == 200, teacher_accounts_response.text
    teacher_account = next(
        row for row in teacher_accounts_response.json()["accounts"]
        if row["id"] == first_account_id)
    assert teacher_account["ownerOperatorId"] == str(first_id)
    assert teacher_account["boundByOperatorId"] == str(first_id)
    assert "appSecret" not in teacher_account and "app_secret_enc" not in teacher_account

    db = SessionLocal()
    try:
        assert db.query(Account).filter(Account.app_key == app_key).count() == 1

        bindings = (db.query(account_service.AccountBinding)
                    .filter(account_service.AccountBinding.account_id == int(first_account_id))
                    .all())
        assert {row.operator_id for row in bindings} >= {
            first_id, extra_id
        }

        second_key = f"second-key-{uuid.uuid4().hex[:8]}"
        second = account_service.connect(
            db,
            app_id=second_key,
            app_secret="second-secret",
            auto_scan=False,
            owner_operator_id=extra_id,
            bound_by_operator_id=extra_id,
        )
        db.commit()
        assert db.query(AccountBinding).filter(
            AccountBinding.operator_id == extra_id).count() == 3
        assert str(second["accountId"]) != first_account_id
    finally:
        db.close()


def test_database_rejects_duplicate_nonempty_app_keys(client, identities):
    del client
    app_key = f"unique-index-{uuid.uuid4().hex}"
    db = SessionLocal()
    try:
        db.add(Account(
            name="first unique key", app_key=app_key,
            app_secret_enc=account_service.crypto.encrypt("secret-one"),
            owner_operator_id=identities["first_id"], shops="[]",
        ))
        db.commit()

        db.add(Account(
            name="duplicate unique key", app_key=app_key,
            app_secret_enc=account_service.crypto.encrypt("secret-two"),
            owner_operator_id=identities["second_id"], shops="[]",
        ))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        assert db.query(Account).filter(Account.app_key == app_key).count() == 1
    finally:
        db.close()


def test_bind_race_after_precheck_returns_conflict_and_preserves_winner(
        client, identities, monkeypatch):
    del client
    app_key = f"bind-race-{uuid.uuid4().hex}"
    db = SessionLocal()
    requester, _ = operator_service.create(
        db, f"bind-race-requester-{uuid.uuid4().hex[:8]}",
        role=operator_service.ROLE_MEMBER)
    requester_id = requester.id
    db.commit()

    class RacingMiaoshouClient:
        def __init__(self, _app_id, _app_secret):
            pass

        def shop_list(self, **_kwargs):
            # 用独立会话模拟另一个并发请求，避免把“竞态获胜者”和当前
            # 请求放在同一个事务里，导致当前请求 rollback 时一起回滚。
            winner_db = SessionLocal()
            try:
                winner_db.add(Account(
                    name="concurrent winner", app_key=app_key,
                    app_secret_enc=account_service.crypto.encrypt("winner-secret"),
                    owner_operator_id=identities["second_id"], shops="[]",
                ))
                winner_db.commit()
            finally:
                winner_db.close()
            return {"data": {"shopList": []}}

        def close(self):
            pass

    monkeypatch.setattr(account_service, "MiaoshouClient", RacingMiaoshouClient)
    try:
        with pytest.raises(AppError) as caught:
            account_service.connect(
                db,
                app_id=app_key,
                app_secret="request-secret",
                auto_scan=False,
                owner_operator_id=requester_id,
                bound_by_operator_id=requester_id,
            )

        assert caught.value.status == 409
        assert "request-secret" not in str(caught.value)
        db.rollback()
        check_db = SessionLocal()
        try:
            rows = check_db.query(Account).filter(Account.app_key == app_key).all()
            assert len(rows) == 1
            assert rows[0].owner_operator_id == identities["second_id"]
        finally:
            check_db.close()
    finally:
        db.close()


def test_teacher_can_bind_shared_key_to_another_student_without_reassigning_owner(
        client, identities, monkeypatch):
    monkeypatch.setattr(account_service, "MiaoshouClient", FakeMiaoshouClient)
    app_key = f"owned-key-{uuid.uuid4().hex[:8]}"
    app_secret = "private-app-secret"
    db = SessionLocal()
    try:
        owner, owner_token = operator_service.create(
            db, f"teacher-shared-owner-{uuid.uuid4().hex[:8]}",
            role=operator_service.ROLE_MEMBER)
        recipient, recipient_token = operator_service.create(
            db, f"teacher-shared-recipient-{uuid.uuid4().hex[:8]}",
            role=operator_service.ROLE_MEMBER)
        db.commit()
        owner_id, recipient_id = owner.id, recipient.id
    finally:
        db.close()
    connected = client.post(
        API + "/auth/connect",
        json={"appId": app_key, "appSecret": app_secret, "autoScan": False},
        headers=_headers(owner_token),
    )
    assert connected.status_code == 200, connected.text

    response = client.post(
        API + "/auth/connect",
        json={"accountId": connected.json()["accountId"],
              "appId": app_key, "appSecret": app_secret,
              "studentOperatorId": str(recipient_id),
              "autoScan": False},
        headers=_headers(identities["teacher_token"]),
    )
    assert response.status_code == 200, response.text
    assert response.json()["accountId"] == connected.json()["accountId"]
    assert response.json()["ownerOperatorId"] == str(owner_id)
    assert response.json()["boundByOperatorId"] == str(identities["teacher_id"])
    assert app_secret not in response.text

    db = SessionLocal()
    try:
        account = db.query(Account).filter(Account.app_key == app_key).one()
        assert account.owner_operator_id == owner_id
        bindings = (db.query(account_service.AccountBinding)
                    .filter(account_service.AccountBinding.account_id == account.id)
                    .all())
        assert {binding.operator_id for binding in bindings} >= {
            owner_id, recipient_id}
    finally:
        db.close()

    for token in (owner_token, recipient_token):
        status = client.get(API + "/auth/status", headers=_headers(token))
        assert status.status_code == 200, status.text
        assert status.json()["accountId"] == connected.json()["accountId"]


def test_failed_reconnect_does_not_invalidate_or_rewrite_existing_account(
        client, monkeypatch):
    db = SessionLocal()
    try:
        _, owner_token = operator_service.create(
            db, f"failed-reconnect-{uuid.uuid4().hex[:8]}",
            role=operator_service.ROLE_MEMBER)
        db.commit()
    finally:
        db.close()

    app_key = f"failed-reconnect-key-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(account_service, "MiaoshouClient", FakeMiaoshouClient)
    connected = client.post(
        API + "/auth/connect",
        json={"appId": app_key, "appSecret": "stored-secret", "autoScan": False},
        headers=_headers(owner_token),
    )
    assert connected.status_code == 200, connected.text
    account_id = int(connected.json()["accountId"])

    db = SessionLocal()
    try:
        account = db.query(Account).filter(Account.id == account_id).one()
        account.shops = '[{"shopId":"cached-shop","name":"cached"}]'
        account.shops_synced_at = 456
        db.commit()
        original_ciphertext = account.app_secret_enc
    finally:
        db.close()

    class RejectingMiaoshouClient:
        def __init__(self, _app_key, _app_secret):
            pass

        def shop_list(self, **_kwargs):
            raise MiaoshouError("invalid candidate", code="invalidSignature", status=401)

        def close(self):
            pass

    monkeypatch.setattr(account_service, "MiaoshouClient", RejectingMiaoshouClient)
    rejected = client.post(
        API + "/auth/connect",
        json={"appId": app_key, "appSecret": "bad-candidate", "autoScan": False},
        headers=_headers(owner_token),
    )
    assert rejected.status_code != 200

    db = SessionLocal()
    try:
        unchanged = db.query(Account).filter(Account.id == account_id).one()
        assert unchanged.status == "active"
        assert unchanged.app_secret_enc == original_ciphertext
        assert unchanged.shops == '[{"shopId":"cached-shop","name":"cached"}]'
        assert unchanged.shops_synced_at == 456
    finally:
        db.close()


def test_account_owner_and_teacher_can_rotate_shared_account_secret(
        client, identities, monkeypatch):
    monkeypatch.setattr(account_service, "MiaoshouClient", FakeMiaoshouClient)
    db = SessionLocal()
    try:
        _, owner_token = operator_service.create(
            db, f"rotate-owner-{uuid.uuid4().hex[:8]}",
            role=operator_service.ROLE_MEMBER)
        recipient, recipient_token = operator_service.create(
            db, f"rotate-recipient-{uuid.uuid4().hex[:8]}",
            role=operator_service.ROLE_MEMBER)
        db.commit()
        recipient_id = recipient.id
    finally:
        db.close()

    app_key = f"rotate-key-{uuid.uuid4().hex[:8]}"
    connected = client.post(
        API + "/auth/connect",
        json={"appId": app_key, "appSecret": "initial-secret", "autoScan": False},
        headers=_headers(owner_token),
    )
    assert connected.status_code == 200, connected.text

    owner_rotation = client.post(
        API + "/auth/connect",
        json={"appId": app_key, "appSecret": "owner-rotated-secret", "autoScan": False},
        headers=_headers(owner_token),
    )
    assert owner_rotation.status_code == 200, owner_rotation.text

    teacher_rotation = client.post(
        API + "/auth/connect",
        json={"appId": app_key, "appSecret": "teacher-rotated-secret",
              "studentOperatorId": str(recipient_id), "autoScan": False},
        headers=_headers(identities["teacher_token"]),
    )
    assert teacher_rotation.status_code == 200, teacher_rotation.text

    db = SessionLocal()
    try:
        account = db.query(Account).filter(Account.app_key == app_key).one()
        assert account_service.crypto.decrypt(account.app_secret_enc) == (
            "teacher-rotated-secret")
    finally:
        db.close()

    # The student bound by the teacher can use the shared credential but cannot
    # replace it with a different one.
    denied = client.post(
        API + "/auth/connect",
        json={"appId": app_key, "appSecret": "recipient-rotated-secret",
              "autoScan": False},
        headers=_headers(recipient_token),
    )
    assert denied.status_code == 403, denied.text


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


def test_transform_preview_hides_another_members_automation_result(client, identities):
    db = SessionLocal()
    try:
        account = Account(
            name="transform ownership fixture",
            app_key=f"transform-{uuid.uuid4().hex[:10]}",
            app_secret_enc="", owner_operator_id=identities["second_id"],
            shops="[]", is_default=False,
        )
        db.add(account)
        db.flush()
        detail_id = f"transform-{uuid.uuid4().hex[:8]}"
        product = Product(
            warehouse_key=f"{detail_id}:source-shop",
            account_id=account.id, detail_id=detail_id, shop_id="source-shop",
            title="private automation title", payload={"title": "private automation title"},
            automation_result=True,
        )
        db.add(product)
        db.flush()
        run = AutomationRun(
            id=f"transform-{uuid.uuid4().hex[:12]}",
            owner_operator_id=identities["second_id"],
            account_id=account.id, shop_id="source-shop", settings={},
        )
        db.add(run)
        db.flush()
        db.add(AutomationRunItem(
            run_id=run.id, key=detail_id, detail_id=detail_id,
            source_shop_id="source-shop", product_id=product.id,
        ))
        db.commit()
        product_id = str(product.id)
    finally:
        db.close()

    hidden = client.post(
        API + "/warehouse/transform", json={"ids": [product_id]},
        headers=_headers(identities["first_token"]),
    )
    assert hidden.status_code == 403, hidden.text

    teacher_preview = client.post(
        API + "/warehouse/transform", json={"ids": [product_id]},
        headers=_headers(identities["teacher_token"]),
    )
    assert teacher_preview.status_code == 200, teacher_preview.text
    assert teacher_preview.json()["items"][0]["title"] == "private automation title"


def test_member_cannot_reuse_another_operators_image_translation(client, identities,
                                                                 monkeypatch):
    del client
    raw = b"same-image-content"
    monkeypatch.setattr(image_service, "_api_key", lambda: "configured")
    monkeypatch.setattr(image_service, "_fetch_source", lambda _url: raw)
    monkeypatch.setattr(image_service, "_shrink", lambda data: data)
    monkeypatch.setattr(image_service, "_image_type", lambda _data: "png")
    monkeypatch.setattr(image_service, "_reserve_slot", lambda _db: None)
    monkeypatch.setattr(image_service, "budget_limit", lambda _db: 10**9)
    monkeypatch.setattr(image_service, "_submit", lambda *_args, **_kwargs: {
        "status": "submitted", "taskId": "isolated-own-image"})
    digest = image_service.image_job_id(
        raw, image_service.IMAGE_MODEL, image_service.TRANSLATION_PROMPT, "1:1")
    db = SessionLocal()
    try:
        db.add(ImageJob(id=digest, operator_id=identities["second_id"],
                        batch_id="foreign-image", status="ready"))
        db.commit()
        result = image_service.translate(
            db, {"items": [{"url": "https://example.test/image.png", "source": "main"}]},
            operator_id=identities["first_id"],
            owner_operator_id=identities["first_id"],
        )
        own = result["jobs"][0]
        assert own["id"] != digest
        assert own["reused"] is False
        assert db.get(ImageJob, own["id"]).operator_id == identities["first_id"]
        foreign = db.get(ImageJob, digest)
        assert foreign.operator_id == identities["second_id"]
        assert foreign.status == "ready"
    finally:
        db.close()
