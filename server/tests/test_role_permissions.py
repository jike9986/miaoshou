"""三级角色的真实 HTTP 权限矩阵与全路由防漏挂检查。"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from api import deps
from core.config import get_settings
from db.session import SessionLocal, init_db
from main import app
from models.product import Product
from models.product_claim import ProductClaim
from services import operator_service, warehouse_service

API = "/api/v1"


@pytest.fixture(scope="module")
def client():
    init_db()
    instance = TestClient(app, raise_server_exceptions=False)
    yield instance
    instance.close()


@pytest.fixture(scope="module")
def tokens(client):
    del client  # 确保数据库已初始化
    rows = {}
    db = SessionLocal()
    try:
        operator_service.ensure_default(db)
        for role in (operator_service.ROLE_TEACHER, operator_service.ROLE_MEMBER):
            _row, token = operator_service.create(
                db, f"matrix-{role}-{uuid.uuid4().hex[:8]}", role=role)
            rows[role] = token
        db.commit()
    finally:
        db.close()
    rows[operator_service.ROLE_ADMIN] = get_settings().local_secret()
    return rows


def auth(tokens, role):
    return {"Authorization": f"Bearer {tokens[role]}"}


@pytest.mark.parametrize("path", [
    "/warehouse", "/words", "/groups", "/size-charts",
    "/publish/settings", "/images/usage", "/auth/status",
])
def test_member_can_read_business_pages(client, tokens, path):
    response = client.get(API + path, headers=auth(tokens, "member"))
    assert response.status_code == 200, response.text


@pytest.mark.parametrize(("method", "path"), [
    ("POST", "/collect-box/123/process"),
    ("POST", "/collect-box/123/claim"),
    ("POST", "/collect-box/delete"),
    ("PATCH", "/warehouse/123"),
    ("DELETE", "/warehouse/123"),
    ("POST", "/warehouse/custom-categories"),
    ("POST", "/warehouse/batch-delete"),
    ("POST", "/warehouse/confirm-save"),
    ("POST", "/groups"),
    ("PATCH", "/groups/123"),
    ("POST", "/words"),
    ("PATCH", "/violation-words/enabled"),
    ("POST", "/categories/sync"),
    ("POST", "/images/translate"),
    ("POST", "/images/reconcile"),
    ("POST", "/size-charts"),
    ("POST", "/titles/save-to-miaoshou"),
    ("POST", "/sync/source-links"),
    ("POST", "/batch"),
    ("POST", "/schedules"),
    ("POST", "/carrier/links"),
    ("POST", "/publish/logs/refresh"),
])
def test_member_cannot_write_business_data(client, tokens, method, path):
    response = client.request(method, API + path, json={}, headers=auth(tokens, "member"))
    assert response.status_code == 403, response.text


@pytest.mark.parametrize(("method", "path"), [
    ("DELETE", "/auth/disconnect"),
    ("PATCH", "/auth/accounts/123"),
    ("POST", "/operators"),
    ("PATCH", "/operators/123"),
    ("POST", "/operators/123/disable"),
    ("POST", "/operators/123/rotate"),
    ("GET", "/audit-logs"),
    ("POST", "/system/settings"),
    ("POST", "/system/update"),
    ("POST", "/system/share"),
    ("POST", "/system/maintenance/backfill"),
    ("POST", "/system/maintenance/prune"),
    ("POST", "/images/limits"),
    ("POST", "/images/verify"),
    ("POST", "/publish/settings"),
])
def test_teacher_cannot_manage_system_or_identities(client, tokens, method, path):
    response = client.request(method, API + path, json={}, headers=auth(tokens, "teacher"))
    assert response.status_code == 403, response.text


@pytest.mark.parametrize(("method", "path"), [
    ("POST", "/collect-box/123/process"),
    ("POST", "/collect-box/123/claim"),
    ("POST", "/collect-box/delete"),
    ("PATCH", "/warehouse/123"),
    ("DELETE", "/warehouse/123"),
    ("POST", "/warehouse/custom-categories"),
    ("POST", "/warehouse/batch-delete"),
    ("POST", "/warehouse/confirm-save"),
    ("POST", "/groups"),
    ("PATCH", "/groups/123"),
    ("POST", "/categories/sync"),
    ("POST", "/images/translate"),
    ("POST", "/images/reconcile"),
    ("POST", "/size-charts"),
    ("POST", "/titles/save-to-miaoshou"),
    ("POST", "/sync/source-links"),
    ("POST", "/batch"),
    ("POST", "/schedules"),
    ("POST", "/carrier/links"),
    ("POST", "/publish"),
    ("POST", "/publish/logs/refresh"),
])
def test_teacher_passes_business_permission_gate(client, tokens, method, path):
    """空 body 在权限之后可返回参数/业务错误，但绝不能是 401/403。"""
    response = client.request(method, API + path, json={}, headers=auth(tokens, "teacher"))
    assert response.status_code != 403, response.text
    if response.status_code == 401:
        # 未连接妙手账号也是 401，但它发生在角色校验之后，不是身份/权限拒绝。
        assert response.json()["error"]["details"].get("reason") == "not_connected", response.text
    assert response.status_code < 500, response.text


def test_teacher_can_edit_words_and_groups(client, tokens):
    suffix = uuid.uuid4().hex[:8]
    word = client.post(API + "/words", json={"action": "add", "word": f"matrix-{suffix}"},
                       headers=auth(tokens, "teacher"))
    assert word.status_code == 200, word.text
    group = client.post(API + "/groups", json={"name": f"matrix-{suffix}"},
                        headers=auth(tokens, "teacher"))
    assert group.status_code == 201, group.text


def test_member_dynamic_write_branches_are_closed(client, tokens):
    headers = auth(tokens, "member")
    transform = client.post(API + "/warehouse/transform",
                            json={"ids": ["123"], "dryRun": False}, headers=headers)
    assert transform.status_code == 403, transform.text
    sync = client.post(API + "/sync/price-templates", json={"save": True}, headers=headers)
    assert sync.status_code == 403, sync.text
    for path in (
        "/categories?refresh=true",
        "/publish/logs?refresh=true",
        "/images/jobs/not-a-job?refresh=true",
    ):
        response = client.get(API + path, headers=headers)
        assert response.status_code == 403, response.text


def test_display_name_is_not_a_login_token_but_remains_a_declaration(client, tokens):
    name = f"matrix-name-{uuid.uuid4().hex[:8]}"
    db = SessionLocal()
    try:
        operator_service.create(db, name)
        db.commit()
    finally:
        db.close()

    for path in ("/operators", "/warehouse"):
        rejected = client.get(API + path, headers={"Authorization": f"Bearer {name}"})
        assert rejected.status_code == 401, rejected.text

    declared = client.get(API + "/operators", headers={
        **auth(tokens, "admin"),
        "X-Operator": name,
    })
    assert declared.status_code == 200, declared.text
    assert declared.json()["current"]["name"] == name


def test_admin_can_read_audit_and_change_shared_settings(client, tokens):
    headers = auth(tokens, "admin")
    audit = client.get(API + "/audit-logs", headers=headers)
    assert audit.status_code == 200, audit.text
    settings = client.post(API + "/system/settings", json={}, headers=headers)
    assert settings.status_code == 200, settings.text


def test_explicit_role_overrides_legacy_admin_flag(client, tokens):
    name = f"matrix-role-{uuid.uuid4().hex[:8]}"
    created = client.post(API + "/operators",
                          json={"name": name, "isAdmin": True, "role": "teacher"},
                          headers=auth(tokens, "admin"))
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["operator"]["role"] == "teacher"
    assert body["operator"]["isAdmin"] is False
    teacher_headers = {"Authorization": f"Bearer {body['token']}"}
    forbidden = client.post(API + "/system/settings", json={}, headers=teacher_headers)
    assert forbidden.status_code == 403, forbidden.text


def test_operator_header_cannot_impersonate_another_operator(client, tokens):
    response = client.get(
        API + "/operators",
        headers={
            **auth(tokens, "teacher"),
            "X-Operator": tokens[operator_service.ROLE_MEMBER],
        },
    )
    assert response.status_code == 403, response.text


def test_shared_mode_rejects_the_machine_secret(client, tokens, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        deps,
        "get_settings",
        lambda: SimpleNamespace(
            shared_mode=True,
            local_secret=lambda: tokens[operator_service.ROLE_ADMIN],
        ),
    )

    guarded = client.get(API + "/warehouse", headers=auth(tokens, "admin"))
    assert guarded.status_code == 401, guarded.text
    identity_probe = client.get(API + "/operators", headers=auth(tokens, "admin"))
    assert identity_probe.status_code == 401, identity_probe.text


def test_automation_results_are_hidden_until_teacher_imports_them(client, tokens):
    suffix = uuid.uuid4().hex[:8]
    db = SessionLocal()
    try:
        pending = Product(
            warehouse_key=f"{suffix}:1", detail_id=suffix, shop_id="1",
            title="Processed item", status="published", payload={},
            automation_result=True,
        )
        regular = Product(
            warehouse_key=f"{suffix}:2", detail_id=suffix, shop_id="2",
            title="Finished item", status="ready", payload={},
        )
        db.add_all([pending, regular])
        db.commit()
        pending_id = str(pending.id)
        regular_id = str(regular.id)
    finally:
        db.close()

    member = auth(tokens, "member")
    denied = client.get(API + "/warehouse/automation-results", headers=member)
    assert denied.status_code == 403, denied.text

    teacher = auth(tokens, "teacher")
    results = client.get(API + "/warehouse/automation-results", headers=teacher)
    assert results.status_code == 200, results.text
    assert pending_id in {item["id"] for item in results.json()["items"]}

    warehouse = client.get(API + "/warehouse", headers=teacher)
    assert warehouse.status_code == 200, warehouse.text
    assert pending_id not in {item["id"] for item in warehouse.json()["items"]}
    assert regular_id in {item["id"] for item in warehouse.json()["items"]}

    imported = client.post(
        API + "/warehouse/automation-results/import",
        json={"ids": [pending_id]}, headers=teacher)
    assert imported.status_code == 200, imported.text
    assert imported.json()["added"] == 1

    warehouse = client.get(API + "/warehouse", headers=teacher)
    assert pending_id in {item["id"] for item in warehouse.json()["items"]}
    repeated = client.post(
        API + "/warehouse/automation-results/import",
        json={"ids": [pending_id]}, headers=teacher)
    assert repeated.status_code == 400, repeated.text


def test_product_claims_are_per_member_and_preserve_legacy_claims(client, tokens):
    suffix = uuid.uuid4().hex[:8]
    db = SessionLocal()
    try:
        product = Product(
            warehouse_key=f"{suffix}:claim", detail_id=suffix, shop_id="claim",
            title="Shared finished product", claimed=True,
            claimed_at=123.0, claimed_shop_id="legacy-shop", payload={},
        )
        db.add(product)
        second, second_token = operator_service.create(
            db, f"second-claim-{suffix}", role=operator_service.ROLE_MEMBER)
        db.commit()
        product_id = str(product.id)
        second_id = second.id
    finally:
        db.close()

    first_headers = auth(tokens, "member")
    second_headers = {"Authorization": f"Bearer {second_token}"}
    path = API + f"/warehouse/{product_id}"

    before = client.get(path, headers=first_headers).json()["product"]
    assert before["claimed"] is False
    assert before["claimStatus"] is None
    assert before["legacyClaimed"] is True

    first_claim = client.post(path + "/claim", headers=first_headers)
    assert first_claim.status_code == 200, first_claim.text
    first_view = client.get(path, headers=first_headers).json()["product"]
    second_view = client.get(path, headers=second_headers).json()["product"]
    assert first_view["claimed"] is True
    assert second_view["claimed"] is False
    second_unclaimed = client.get(
        API + "/warehouse?claimed=false", headers=second_headers)
    assert product_id in {row["id"] for row in second_unclaimed.json()["items"]}

    repeated = client.post(path + "/claim", headers=first_headers)
    second_claim = client.post(path + "/claim", headers=second_headers)
    assert repeated.status_code == 200, repeated.text
    assert first_claim.json()["id"] == repeated.json()["id"]
    assert second_claim.status_code == 200, second_claim.text
    assert first_claim.json()["id"] != second_claim.json()["id"]

    first_view = client.get(path, headers=first_headers).json()["product"]
    second_view = client.get(path, headers=second_headers).json()["product"]
    assert first_view["claimed"] is True
    assert second_view["claimed"] is True
    assert first_view["claimStatus"] == second_view["claimStatus"] == "claimed"
    assert first_view["legacyClaimed"] is True

    first_list = client.get(API + "/warehouse?claimed=true", headers=first_headers)
    second_list = client.get(API + "/warehouse?claimed=true", headers=second_headers)
    assert first_list.status_code == second_list.status_code == 200
    assert product_id in {row["id"] for row in first_list.json()["items"]}
    assert product_id in {row["id"] for row in second_list.json()["items"]}

    db = SessionLocal()
    try:
        claims = db.query(ProductClaim).filter(
            ProductClaim.product_id == int(product_id)).all()
        assert {row.owner_operator_id for row in claims} == {
            int(first_claim.json()["ownerOperatorId"]), second_id}
        assert len(claims) == 2
        legacy_product = db.query(Product).filter(Product.id == int(product_id)).one()
        assert legacy_product.claimed is True
        assert legacy_product.claimed_at == 123.0
        assert legacy_product.claimed_shop_id == "legacy-shop"
    finally:
        db.close()


def test_product_claim_service_isolates_owners_and_keeps_legacy_fields(client):
    del client  # initialize the isolated database
    suffix = uuid.uuid4().hex[:8]
    db = SessionLocal()
    try:
        first, _ = operator_service.create(
            db, f"claim-service-a-{suffix}", role=operator_service.ROLE_MEMBER)
        second, _ = operator_service.create(
            db, f"claim-service-b-{suffix}", role=operator_service.ROLE_MEMBER)
        product = Product(
            warehouse_key=f"{suffix}:service", detail_id=suffix, shop_id="service",
            claimed=True, claimed_at=123.0, claimed_shop_id="legacy-shop", payload={},
        )
        db.add(product)
        db.flush()

        first_claim = warehouse_service.claim_product(db, product.id, first.id)
        duplicate = warehouse_service.claim_product(db, product.id, first.id)
        assert first_claim.id == duplicate.id

        first_products = warehouse_service.list_products(
            db, claimed=True, operator_id=first.id)["items"]
        second_unclaimed = warehouse_service.list_products(
            db, claimed=False, operator_id=second.id)["items"]
        first_body = warehouse_service.detail(
            db, product.id, operator_id=first.id)["product"]
        second_body = warehouse_service.detail(
            db, product.id, operator_id=second.id)["product"]
        assert str(product.id) in {item["id"] for item in first_products}
        assert str(product.id) in {item["id"] for item in second_unclaimed}
        assert first_body["claimed"] is True
        assert second_body["claimed"] is False
        assert first_body["claimStatus"] == "claimed"
        assert second_body["claimStatus"] is None
        assert first_body["legacyClaimed"] is second_body["legacyClaimed"] is True

        second_claim = warehouse_service.claim_product(db, product.id, second.id)
        assert second_claim.id != first_claim.id
        assert warehouse_service.detail(
            db, product.id, operator_id=second.id)["product"]["claimed"] is True
        assert warehouse_service.detail(
            db, product.id, operator_id=second.id)["product"]["claimStatus"] == "claimed"
        assert warehouse_service.stats(db, operator_id=first.id)["totalClaimed"] == 1
        assert warehouse_service.stats(db, operator_id=second.id)["totalClaimed"] == 1
        assert product.claimed is True
        assert product.claimed_at == 123.0
        assert product.claimed_shop_id == "legacy-shop"
    finally:
        db.rollback()
        db.close()


# 这些 POST 只计算/识别，不写业务数据，成员可以调用。
READ_ONLY_POSTS = {
    "/api/v1/warehouse/price-preview",
    "/api/v1/warehouse/pre-save-gate",
    "/api/v1/images/analyze",
    "/api/v1/images/review-gate",
    "/api/v1/titles/preview",
    "/api/v1/system/update-check",
    "/api/v1/system/price-preview",
}
# 同一路由按 body 区分预览与写入，由上面的真实 HTTP 用例覆盖写分支。
DYNAMIC_WRITE_ROUTES = {
    ("POST", "/api/v1/warehouse/transform"),
    ("POST", "/api/v1/sync/price-templates"),
    # Binding is available to a member only for their own operator owner id.
    ("POST", "/api/v1/auth/connect"),
    # 这些路由在处理函数内校验当前 OperatorDep 的角色/状态。
    ("POST", "/api/v1/operators/students"),
    ("POST", "/api/v1/operators/{operator_id}/reset-password"),
    ("POST", "/api/v1/auth/change-password"),
    ("POST", "/api/v1/auth/initialize-credentials"),
    # Member claim creation is an intentional owner-scoped write.
    ("POST", "/api/v1/warehouse/{ident}/claim"),
    # Member publishing is allowed only after publish_service verifies every claim.
    ("POST", "/api/v1/publish"),
    # 登录是唯一公开的写入路由，认证失败统一返回 401。
    ("POST", "/api/v1/auth/login"),
}


def _required_permissions(dependant):
    found = set()
    for dependency in dependant.dependencies:
        call = dependency.call
        if call is deps.require_admin:
            found.add("admin")
        permission = getattr(call, "permission", None)
        if permission:
            found.add(permission)
        found.update(_required_permissions(dependency))
    return found


def test_every_mutating_route_has_a_permission_above_member():
    """直接检查 FastAPI 依赖树，防止新写路由漏挂或误用成员只读权限。"""
    member_permissions = operator_service.ROLE_PERMISSIONS[operator_service.ROLE_MEMBER]
    missing = []
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith(API):
            continue
        for method in sorted(getattr(route, "methods", set()) or set()):
            if method not in {"POST", "PUT", "PATCH", "DELETE"}:
                continue
            if path in READ_ONLY_POSTS or (method, path) in DYNAMIC_WRITE_ROUTES:
                continue
            permissions = _required_permissions(route.dependant)
            if not permissions or not any(
                    permission == "admin" or permission not in member_permissions
                    for permission in permissions):
                missing.append(f"{method} {path} -> {sorted(permissions)}")
    assert not missing, "写接口缺少高于成员的权限：\n" + "\n".join(missing)
