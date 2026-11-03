"""老师/学员权限矩阵与全路由防漏挂检查。"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from api import deps
from db.session import SessionLocal, init_db
from main import app
from models.account import Account
from models.automation_run import AutomationRun, AutomationRunItem
from models.image_job import ImageJob
from models.operator import Operator
from models.product import Product
from models.product_claim import ProductClaim
from models.publish_log import PublishLog
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
        _admin, rows[operator_service.ROLE_ADMIN] = operator_service.create(
            db, f"matrix-admin-{uuid.uuid4().hex[:8]}",
            role=operator_service.ROLE_ADMIN)
        db.commit()
    finally:
        db.close()
    return rows


def auth(tokens, role):
    return {"Authorization": f"Bearer {tokens[role]}"}


def test_schedule_is_available_to_shared_client_and_members():
    client_operator = Operator(name=operator_service.CLIENT_OPERATOR_NAME,
                               token="client-test-token", role=operator_service.ROLE_MEMBER)
    member = Operator(name="ordinary-member", token="member-test-token",
                      role=operator_service.ROLE_MEMBER)
    assert operator_service.can(client_operator, "schedule") is True
    assert operator_service.can(member, "schedule") is True


@pytest.mark.parametrize("path", [
    "/warehouse", "/words", "/groups", "/size-charts",
    "/publish/settings", "/images/usage", "/auth/status",
    "/schedules",
])
def test_member_can_read_business_pages(client, tokens, path):
    response = client.get(API + path, headers=auth(tokens, "member"))
    assert response.status_code == 200, response.text


def test_word_backup_export_is_available_to_teacher_and_denied_to_member(client, tokens):
    created = client.post(
        API + "/violation-words",
        json={"action": "add", "word": "backup permission check"},
        headers=auth(tokens, "teacher"),
    )
    assert created.status_code == 200, created.text

    exported = client.get(API + "/violation-words/export", headers=auth(tokens, "teacher"))
    assert exported.status_code == 200, exported.text
    assert "attachment; filename=\"violation-words-backup.json\"" in exported.headers[
        "content-disposition"
    ]
    item = next(
        row for row in exported.json()["items"]
        if row["word"] == "backup permission check"
    )
    assert set(item) == {
        "word", "action", "source", "siteScope", "sourceUrl", "ruleVersion",
        "enabled", "createdAt",
    }
    assert item["source"] == "manual"
    assert item["enabled"] is True

    restored = client.post(
        API + "/violation-words",
        json={"action": "import", "category": "remove", "mode": "replace",
              "words": exported.json()},
        headers=auth(tokens, "teacher"),
    )
    assert restored.status_code == 200, restored.text
    restored_export = client.get(
        API + "/violation-words/export", headers=auth(tokens, "teacher")
    )
    assert restored_export.json() == exported.json()

    denied = client.get(API + "/violation-words/export", headers=auth(tokens, "member"))
    assert denied.status_code == 403, denied.text


def test_word_rule_provenance_is_bound_over_http_and_in_list(client, tokens):
    suffix = uuid.uuid4().hex[:8]
    word = f"source-{suffix}"
    created = client.post(
        API + "/violation-words",
        json={
            "action": "add",
            "word": word,
            "siteScope": "my",
            "sourceUrl": "https://example.org/policy",
            "ruleVersion": "2026-10",
        },
        headers=auth(tokens, "teacher"),
    )
    assert created.status_code == 200, created.text
    assert created.json()["items"][0]["siteScope"] == "MY"

    listed = client.get(API + "/violation-words", params={"q": word},
                        headers=auth(tokens, "teacher"))
    assert listed.status_code == 200, listed.text
    item = listed.json()["items"][0]
    assert item["sourceUrl"] == "https://example.org/policy"
    assert item["ruleVersion"] == "2026-10"

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
    ("POST", "/carrier/links"),
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
def test_member_cannot_manage_system_or_identities(client, tokens, method, path):
    response = client.request(method, API + path, json={}, headers=auth(tokens, "member"))
    assert response.status_code == 403, response.text


@pytest.mark.parametrize("path", [
    "/system/logs/tail",
    "/system/maintenance/backfill",
    "/system/maintenance/prune",
])
@pytest.mark.parametrize("role", ["teacher", "admin"])
def test_teacher_and_admin_can_read_global_system_diagnostics(client, tokens,
                                                              path, role):
    response = client.get(API + path, headers=auth(tokens, role))
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("path", [
    "/system/logs/tail",
    "/system/maintenance/backfill",
    "/system/maintenance/prune",
])
def test_member_cannot_read_global_system_diagnostics(client, tokens, path):
    response = client.get(API + path, headers=auth(tokens, "member"))
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
        if response.status_code == 502:
            # 该用例不配置真实妙手凭据；确认已通过权限层后，允许上游拒绝。
            assert response.json()["error"]["code"] == "UPSTREAM_ERROR", response.text
        else:
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
        "/images/jobs/not-a-job?refresh=true",
    ):
        response = client.get(API + path, headers=headers)
        assert response.status_code == 403, response.text


def test_member_can_refresh_own_publish_receipts(client, tokens):
    """Publishing a claimed item and refreshing its receipt use one member gate."""
    for method, path in (("GET", "/publish/logs?refresh=true"),
                         ("POST", "/publish/logs/refresh")):
        response = client.request(method, API + path, json={} if method == "POST" else None,
                                  headers=auth(tokens, "member"))
        # With no connected account this may be a business/connection error, but
        # the member must get past the permission dependency.
        assert response.status_code != 403, response.text


def test_member_schedule_routes_remain_available(client, tokens):
    headers = auth(tokens, "member")
    listed = client.get(API + "/schedules", headers=headers)
    assert listed.status_code == 200, listed.text
    created = client.post(API + "/schedules", json={}, headers=headers)
    # The empty request is rejected by input validation, proving the member
    # passed the schedule permission gate without creating a task.
    assert created.status_code == 400, created.text


def test_teacher_cannot_manage_superadmin_credentials(client, tokens):
    db = SessionLocal()
    try:
        superadmin = operator_service.resolve(db, tokens["admin"])
        assert superadmin is not None
        target_id = str(superadmin.id)
    finally:
        db.close()

    headers = auth(tokens, "teacher")
    for method, path, body in (
        ("POST", f"/operators/{target_id}/disable", None),
        ("POST", f"/operators/{target_id}/enable", None),
        ("POST", f"/operators/{target_id}/rotate", None),
        ("PATCH", f"/operators/{target_id}", {"enabled": False}),
        ("PATCH", f"/operators/{target_id}", {"name": "should-not-change"}),
    ):
        response = client.request(method, API + path, json=body, headers=headers)
        assert response.status_code == 403, response.text
        assert response.json()["error"]["details"]["reason"] == "superadmin_target"


def test_create_operator_rejects_unknown_role(client, tokens):
    response = client.post(
        API + "/operators",
        json={"name": f"invalid-role-{uuid.uuid4().hex[:8]}", "role": "not-a-role"},
        headers=auth(tokens, "admin"),
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert response.json()["error"]["details"]["field"] == "role"


def test_display_name_is_not_a_login_token_but_remains_a_declaration(client, tokens):
    name = f"matrix-name-{uuid.uuid4().hex[:8]}"
    db = SessionLocal()
    try:
        _operator, token = operator_service.create(db, name)
        db.commit()
    finally:
        db.close()

    for path in ("/operators", "/warehouse"):
        rejected = client.get(API + path, headers={"Authorization": f"Bearer {name}"})
        assert rejected.status_code == 401, rejected.text

    declared = client.get(API + "/operators", headers={
        "Authorization": f"Bearer {token}",
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


def test_teacher_has_global_management_but_cannot_grant_teacher_role(client, tokens):
    headers = auth(tokens, "teacher")
    audit = client.get(API + "/audit-logs", headers=headers)
    assert audit.status_code == 200, audit.text
    settings = client.post(API + "/system/settings", json={}, headers=headers)
    assert settings.status_code == 200, settings.text

    name = f"teacher-created-{uuid.uuid4().hex[:8]}"
    created = client.post(API + "/operators", json={"name": name, "role": "member"},
                          headers=headers)
    assert created.status_code == 201, created.text
    operator_id = created.json()["operator"]["id"]
    changed = client.patch(API + f"/operators/{operator_id}",
                           json={"role": "teacher"}, headers=headers)
    assert changed.status_code == 403, changed.text
    assert changed.json()["error"]["code"] == "FORBIDDEN"
    disabled = client.post(API + f"/operators/{operator_id}/disable", headers=headers)
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["operator"]["enabled"] is False


def test_operator_cannot_disable_self_using_zero_padded_id(client, tokens):
    headers = auth(tokens, "teacher")
    current = client.get(API + "/operators", headers=headers)
    assert current.status_code == 200, current.text
    operator_id = int(current.json()["current"]["id"])

    disabled = client.post(
        API + f"/operators/{operator_id:06d}/disable", headers=headers)
    assert disabled.status_code == 403, disabled.text
    assert disabled.json()["error"]["details"]["reason"] == "self"


def test_admin_can_grant_and_revoke_teacher_role(client, tokens):
    headers = auth(tokens, "admin")
    name = f"admin-managed-{uuid.uuid4().hex[:8]}"
    created = client.post(API + "/operators", json={"name": name, "role": "member"},
                          headers=headers)
    assert created.status_code == 201, created.text
    operator_id = created.json()["operator"]["id"]

    promoted = client.patch(API + f"/operators/{operator_id}",
                            json={"role": "teacher"}, headers=headers)
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["operator"]["role"] == "teacher"

    demoted = client.patch(API + f"/operators/{operator_id}",
                           json={"role": "member"}, headers=headers)
    assert demoted.status_code == 200, demoted.text
    assert demoted.json()["operator"]["role"] == "member"


def test_superadmin_cannot_demote_self_but_another_superadmin_can_manage_them(client, tokens):
    db = SessionLocal()
    try:
        target, target_token = operator_service.create(
            db, f"self-demote-admin-{uuid.uuid4().hex[:8]}",
            role=operator_service.ROLE_ADMIN)
        target_id = str(target.id)
        _other_superadmin, other_token = operator_service.create(
            db, f"second-admin-{uuid.uuid4().hex[:8]}",
            role=operator_service.ROLE_ADMIN)
        db.commit()
    finally:
        db.close()

    self_change = client.patch(
        API + f"/operators/{target_id}",
        json={"role": operator_service.ROLE_TEACHER},
        headers={"Authorization": f"Bearer {target_token}"},
    )
    assert self_change.status_code == 403, self_change.text
    assert self_change.json()["error"]["details"]["reason"] == "superadmin_self_demotion"

    other_change = client.patch(
        API + f"/operators/{target_id}",
        json={"role": operator_service.ROLE_TEACHER},
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert other_change.status_code == 200, other_change.text
    assert other_change.json()["operator"]["role"] == operator_service.ROLE_TEACHER


def test_explicit_teacher_role_overrides_legacy_admin_flag(client, tokens):
    name = f"matrix-role-{uuid.uuid4().hex[:8]}"
    created = client.post(API + "/operators",
                          json={"name": name, "isAdmin": True, "role": "teacher"},
                          headers=auth(tokens, "admin"))
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["operator"]["role"] == "teacher"
    assert body["operator"]["isAdmin"] is False
    teacher_headers = {"Authorization": f"Bearer {body['token']}"}
    allowed = client.post(API + "/system/settings", json={}, headers=teacher_headers)
    assert allowed.status_code == 200, allowed.text


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
        teacher = operator_service.resolve(db, tokens[operator_service.ROLE_TEACHER])
        account = Account(name=f"automation-results-{suffix}", shops="[]")
        db.add(account)
        db.flush()
        pending = Product(
            warehouse_key=f"{suffix}:1", detail_id=suffix, shop_id="1",
            title="Processed item", status="published", payload={},
            account_id=account.id, automation_result=True, draft_verified_revision=1,
        )
        failed = Product(
            warehouse_key=f"{suffix}:failed", detail_id=f"{suffix}2", shop_id="2",
            title="Failed item", status="ready", payload={},
            account_id=account.id, automation_result=True,
        )
        forged = Product(
            warehouse_key=f"{suffix}:forged", detail_id=f"{suffix}3", shop_id="3",
            title="Unverified item", status="ready", payload={},
            account_id=account.id, automation_result=True,
        )
        regular = Product(
            warehouse_key=f"{suffix}:2", detail_id=suffix, shop_id="2",
            title="Finished item", status="ready", payload={},
        )
        db.add_all([pending, failed, forged, regular])
        db.flush()
        successful_run = AutomationRun(
            id=f"arun_{uuid.uuid4().hex}", owner_operator_id=teacher.id,
            account_id=account.id, shop_id="9", status="completed", settings={},
        )
        failed_run = AutomationRun(
            id=f"arun_{uuid.uuid4().hex}", owner_operator_id=teacher.id,
            account_id=account.id, shop_id="9", status="partial", settings={},
        )
        db.add_all([successful_run, failed_run])
        db.flush()
        receipt = PublishLog(
            account_id=account.id, product_id=pending.id, detail_id=suffix,
            shop_id=successful_run.shop_id, batch_id=f"batch-{suffix}",
            status="success", title="Processed item",
        )
        db.add(receipt)
        db.flush()
        db.add_all([
            AutomationRunItem(
                run_id=successful_run.id, key=f"{suffix}:1", detail_id=suffix,
                source_shop_id="1", process_status="succeeded", product_id=pending.id,
                publish_status="succeeded",
                publish_result={"batchId": receipt.batch_id, "platformStatus": "success",
                                "publishLogId": str(receipt.id)},
            ),
            AutomationRunItem(
                run_id=failed_run.id, key=f"{suffix}2:2", detail_id=f"{suffix}2",
                source_shop_id="2", process_status="failed", product_id=failed.id,
            ),
            AutomationRunItem(
                run_id=successful_run.id, key=f"{suffix}3:3", detail_id=f"{suffix}3",
                source_shop_id="3", process_status="succeeded", product_id=forged.id,
            ),
        ])
        db.commit()
        pending_id = str(pending.id)
        failed_id = str(failed.id)
        forged_id = str(forged.id)
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
    assert failed_id not in {item["id"] for item in results.json()["items"]}
    assert forged_id not in {item["id"] for item in results.json()["items"]}

    rejected = client.post(
        API + "/warehouse/automation-results/import",
        json={"ids": [failed_id]}, headers=teacher)
    assert rejected.status_code == 400, rejected.text
    forged_rejected = client.post(
        API + "/warehouse/automation-results/import",
        json={"ids": [forged_id]}, headers=teacher)
    assert forged_rejected.status_code == 400, forged_rejected.text

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


def test_automation_results_require_a_verified_successful_process(client, tokens):
    suffix = uuid.uuid4().hex[:8]
    db = SessionLocal()
    try:
        teacher = operator_service.resolve(db, tokens[operator_service.ROLE_TEACHER])
        account = Account(name=f"receipt-gate-{suffix}", shops="[]")
        db.add(account)
        db.flush()
        run = AutomationRun(
            id=f"arun_{uuid.uuid4().hex}", owner_operator_id=teacher.id,
            account_id=account.id, shop_id="9", status="completed", settings={},
        )
        db.add(run)
        db.flush()

        cases = [
            ("queued", "succeeded", "queued", {}),
            ("needs_review", "succeeded", "needs_review", {"batchId": "queued-batch"}),
            ("publish_failed", "succeeded", "failed", {"batchId": "failed-batch"}),
            ("not_requested", "succeeded", "not_requested", {}),
            ("no_receipt", "succeeded", "succeeded", {
                "platformStatus": "success", "publishLogId": "99999999",
            }),
            ("process_failed", "failed", "not_requested", {}),
        ]
        products = {}
        items = []
        for index, (key, process_status, publish_status, publish_result) in enumerate(
            cases, start=1,
        ):
            detail_id = f"{suffix}{index}"
            product = Product(
                warehouse_key=f"{suffix}:gate:{key}", detail_id=detail_id,
                shop_id=str(index), title=key, status="ready", payload={},
                account_id=account.id, automation_result=True,
                draft_verified_revision=1,
            )
            db.add(product)
            db.flush()
            products[key] = product
            items.append(AutomationRunItem(
                run_id=run.id, key=f"{suffix}:gate:{key}", detail_id=detail_id,
                source_shop_id=str(index), process_status=process_status,
                product_id=product.id, publish_status=publish_status,
                publish_result=publish_result,
            ))
        stale = Product(
            warehouse_key=f"{suffix}:gate:stale", detail_id=f"{suffix}6",
            shop_id="7", title="Stale receipt", status="ready", payload={},
            account_id=account.id, automation_result=True,
            draft_verified_revision=1,
        )
        db.add(stale)
        db.flush()
        products["stale"] = stale
        stale_receipt = PublishLog(
            account_id=account.id, product_id=stale.id, detail_id=stale.detail_id,
            shop_id=run.shop_id, batch_id=f"old-receipt-{suffix}", status="success",
            title=stale.title,
        )
        db.add(stale_receipt)
        db.flush()
        items.append(AutomationRunItem(
            run_id=run.id, key=f"{suffix}:gate:stale", detail_id=stale.detail_id,
            source_shop_id=stale.shop_id, process_status="succeeded",
            product_id=stale.id, publish_status="succeeded",
            publish_result={"batchId": f"new-attempt-{suffix}",
                            "platformStatus": "success",
                            "publishLogId": str(stale_receipt.id)},
        ))
        eligible = Product(
            warehouse_key=f"{suffix}:gate:eligible", detail_id=f"{suffix}7",
            shop_id="6", title="Receipt confirmed", status="ready", payload={},
            account_id=account.id, automation_result=True,
            draft_verified_revision=1,
        )
        db.add(eligible)
        db.flush()
        receipt = PublishLog(
            account_id=account.id, product_id=eligible.id, detail_id=eligible.detail_id,
            shop_id=run.shop_id, batch_id=f"receipt-{suffix}", status="success",
            title=eligible.title,
        )
        db.add(receipt)
        db.flush()
        items.append(AutomationRunItem(
            run_id=run.id, key=f"{suffix}:gate:eligible", detail_id=eligible.detail_id,
            source_shop_id=eligible.shop_id, process_status="succeeded",
            product_id=eligible.id, publish_status="succeeded",
            publish_result={"batchId": receipt.batch_id, "platformStatus": "success",
                            "publishLogId": str(receipt.id)},
        ))
        image_pending = Product(
            warehouse_key=f"{suffix}:gate:image-pending", detail_id=f"{suffix}8",
            shop_id="8", title="Image pending", status="ready", payload={},
            account_id=account.id, automation_result=True,
            draft_verified_revision=1,
        )
        db.add(image_pending)
        db.flush()
        db.add(ImageJob(
            id=f"image-pending-{suffix}", product_id=image_pending.id,
            detail_id=image_pending.detail_id, source="main", status="submitted",
        ))
        items.append(AutomationRunItem(
            run_id=run.id, key=f"{suffix}:gate:image-pending",
            detail_id=image_pending.detail_id, source_shop_id=image_pending.shop_id,
            process_status="succeeded", product_id=image_pending.id,
            publish_status="needs_review", publish_result={"resumeKind": "images_pending"},
        ))
        db.add_all(items)
        db.commit()
        ids = {key: str(product.id) for key, product in products.items()}
        image_pending_id = str(image_pending.id)
        eligible_id = str(eligible.id)
    finally:
        db.close()

    teacher_headers = auth(tokens, "teacher")
    listed = client.get(API + "/warehouse/automation-results", headers=teacher_headers)
    assert listed.status_code == 200, listed.text
    visible_ids = {item["id"] for item in listed.json()["items"]}
    assert eligible_id in visible_ids
    expected_eligible = {ids[key] for key in (
        "queued", "needs_review", "publish_failed", "not_requested", "no_receipt",
    )}
    assert expected_eligible | {eligible_id, ids["stale"]} <= visible_ids
    assert not ({ids["process_failed"]} & visible_ids)
    assert image_pending_id not in visible_ids

    for key, ident in ids.items():
        if key in {
            "queued", "needs_review", "publish_failed", "not_requested", "no_receipt", "stale",
        }:
            continue
        rejected = client.post(
            API + "/warehouse/automation-results/import",
            json={"ids": [ident]}, headers=teacher_headers,
        )
        assert rejected.status_code == 400, rejected.text
    imported_without_publish = client.post(
        API + "/warehouse/automation-results/import",
        json={"ids": [ids["not_requested"]]}, headers=teacher_headers,
    )
    assert imported_without_publish.status_code == 200, imported_without_publish.text
    assert imported_without_publish.json()["added"] == 1
    imported = client.post(
        API + "/warehouse/automation-results/import",
        json={"ids": [eligible_id]}, headers=teacher_headers,
    )
    assert imported.status_code == 200, imported.text
    assert imported.json()["added"] == 1


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
    # Members can change only their own processing preferences on this mixed-scope
    # route; the handler separately guards global read-only/publish switches.
    ("POST", API + "/system/settings"),
    ("POST", "/api/v1/warehouse/transform"),
    ("POST", "/api/v1/sync/price-templates"),
    # Binding is available to a member only for their own operator owner id.
    ("POST", "/api/v1/auth/connect"),
    # 这些路由在处理函数内校验当前 OperatorDep 的角色/状态。
    ("POST", "/api/v1/operators/students"),
    ("POST", "/api/v1/operators/{operator_id}/reset-password"),
    ("POST", "/api/v1/auth/change-password"),
    # Recovery mailbox requests mutate only the authenticated operator's own
    # email, and require the signed-in OperatorDep at the handler.
    ("POST", "/api/v1/auth/email-verification/request"),
    ("POST", "/api/v1/auth/email-verification/confirm"),
    ("POST", "/api/v1/auth/initialize-credentials"),
    # 找回密码需要在未登录状态下使用；接口自身有令牌、过期和频率限制。
    ("POST", "/api/v1/auth/password-reset/request"),
    ("POST", "/api/v1/auth/password-reset/confirm"),
    # Email registration is intentionally public; the code request has its
    # own IP/mailbox throttles and does not mutate an authenticated account.
    ("POST", "/api/v1/auth/register"),
    ("POST", "/api/v1/auth/registration-code/request"),
    # Member claim creation is an intentional owner-scoped write.
    ("POST", "/api/v1/warehouse/{ident}/claim"),
    # Member publishing is allowed only after publish_service verifies every claim.
    ("POST", "/api/v1/publish"),
    # Receipt refresh is allowed for claimed-product publishers and is scoped
    # to the requesting member's own publish rows in the handler.
    ("POST", "/api/v1/publish/logs/refresh"),
    ("POST", "/api/v1/claim-jobs"),
    # 登录是唯一公开的写入路由，认证失败统一返回 401。
    ("POST", "/api/v1/auth/login"),
    # 邮箱验证码登录与登录同属公开认证入口，不得继承当前个人会话门禁。
    ("POST", "/api/v1/auth/email-code-login/request"),
    ("POST", "/api/v1/auth/email-code-login/confirm"),
    # 首次密码设置只修改当前用户自己的密码，仍须身份和首次修改门禁。
    ("POST", "/api/v1/auth/complete-password-setup"),
    # 定时发布按当前操作员绑定账号隔离，学员也可管理自己的任务。
    ("POST", "/api/v1/schedules"),
    ("PATCH", "/api/v1/schedules/{task_id}"),
    ("DELETE", "/api/v1/schedules/{task_id}"),
    ("POST", "/api/v1/schedules/{task_id}/run"),
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


def _dependency_calls(dependant):
    calls = set()
    for dependency in dependant.dependencies:
        calls.add(dependency.call)
        calls.update(_dependency_calls(dependency))
    return calls


def _route_for(method, path):
    matches = [
        route for route in app.routes
        if getattr(route, "path", None) == path
        and method in (getattr(route, "methods", set()) or set())
    ]
    assert len(matches) == 1, f"预期恰好一个路由：{method} {path}，实际 {len(matches)}"
    return matches[0]


def test_email_code_login_routes_are_public_but_password_setup_is_guarded():
    auth_gates = {
        deps.require_token,
        deps.current_operator,
        deps.require_password_change_complete,
    }
    for path in (
        API + "/auth/email-code-login/request",
        API + "/auth/email-code-login/confirm",
    ):
        calls = _dependency_calls(_route_for("POST", path).dependant)
        assert calls.isdisjoint(auth_gates), f"公开验证码接口意外继承个人会话门禁：{path}"

    setup_calls = _dependency_calls(
        _route_for("POST", API + "/auth/complete-password-setup").dependant
    )
    assert auth_gates <= setup_calls, "首次密码设置必须保留身份与密码修改门禁"


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
