from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

import api.warehouse as warehouse_api
from db.session import SessionLocal, init_db
from main import app
from models.account import Account
from models.automation_run import AutomationRun, AutomationRunItem
from models.group import Group
from models.product import Product
from services import gate_service, operator_service

API = "/api/v1"


@pytest.fixture(scope="module")
def client():
    init_db()
    instance = TestClient(app, raise_server_exceptions=False)
    yield instance
    instance.close()


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_member_price_preview_cannot_read_another_members_automation_result(
        client, monkeypatch):
    monkeypatch.setattr(warehouse_api, "_preview_template", lambda *_args: {})
    db = SessionLocal()
    try:
        suffix = uuid.uuid4().hex[:10]
        first, first_token = operator_service.create(
            db, f"preview-owner-{suffix}", role=operator_service.ROLE_MEMBER)
        second, _second_token = operator_service.create(
            db, f"preview-other-{suffix}", role=operator_service.ROLE_MEMBER)
        account = Account(name=f"preview-account-{suffix}",
                          app_key=f"preview-key-{suffix}", app_secret_enc="",
                          shops="[]", is_default=False)
        db.add(account)
        db.flush()

        products: dict[str, Product] = {}
        for label, owner in (("own", first), ("foreign", second)):
            product = Product(
                warehouse_key=f"preview-{label}-{suffix}",
                detail_id=f"preview-{label}-{suffix}",
                shop_id="shared-shop", account_id=account.id,
                title=f"{label} private title", payload={},
                automation_result=True,
            )
            run = AutomationRun(
                id=f"preview-{label}-{suffix}", owner_operator_id=owner.id,
                account_id=account.id, shop_id="shared-shop", settings={})
            db.add_all([product, run])
            db.flush()
            db.add(AutomationRunItem(
                run_id=run.id, key=f"{label}-{suffix}",
                detail_id=product.detail_id, source_shop_id="shared-shop",
                product_id=product.id,
            ))
            products[label] = product

        shared = Product(
            warehouse_key=f"preview-shared-{suffix}",
            detail_id=f"preview-shared-{suffix}", shop_id="shared-shop",
            account_id=account.id, title="shared finished title", payload={},
            automation_result=False,
        )
        db.add(shared)
        db.commit()
        own_id, foreign_id, shared_id = (
            products["own"].id, products["foreign"].id, shared.id)
    finally:
        db.close()

    headers = _headers(first_token)
    def preview(warehouse_id: int):
        return client.post(
            API + "/warehouse/price-preview",
            json={"template": "test", "items": [{"warehouseId": str(warehouse_id)}]},
            headers=headers,
        )

    own_response = preview(own_id)
    foreign_response = preview(foreign_id)
    shared_response = preview(shared_id)

    assert own_response.status_code == 200, own_response.text
    assert foreign_response.status_code == 403, foreign_response.text
    assert shared_response.status_code == 200, shared_response.text


def test_gate_endpoints_cannot_read_another_members_automation_result(
        client, monkeypatch):
    monkeypatch.setattr(warehouse_api, "_preview_template", lambda *_args: {})
    monkeypatch.setattr(
        gate_service, "main_image_gate",
        lambda *_args, **_kwargs: {
            "blocked": False, "removeIndexes": [], "needsReview": [],
            "message": "", "reason": "", "imageCount": 1,
            "keptCount": 1, "rows": [], "warnings": [],
        },
    )
    db = SessionLocal()
    try:
        suffix = uuid.uuid4().hex[:10]
        owner, owner_token = operator_service.create(
            db, f"gate-owner-{suffix}", role=operator_service.ROLE_MEMBER)
        other, other_token = operator_service.create(
            db, f"gate-other-{suffix}", role=operator_service.ROLE_MEMBER)
        _teacher, teacher_token = operator_service.create(
            db, f"gate-teacher-{suffix}", role=operator_service.ROLE_TEACHER)
        account = Account(name=f"gate-account-{suffix}",
                          app_key=f"gate-key-{suffix}", app_secret_enc="",
                          shops="[]", is_default=False)
        db.add(account)
        db.flush()

        product = Product(
            warehouse_key=f"gate-product-{suffix}",
            detail_id=f"gate-product-{suffix}", shop_id="gate-shop",
            account_id=account.id, title="private automation title",
            payload={"title": "private automation title",
                     "imgUrls": ["https://example.test/cover.jpg"]},
            images=["https://example.test/cover.jpg"],
            automation_result=True,
        )
        run = AutomationRun(
            id=f"gate-run-{suffix}", owner_operator_id=owner.id,
            account_id=account.id, shop_id="gate-shop", settings={})
        db.add_all([product, run])
        db.flush()
        db.add(AutomationRunItem(
            run_id=run.id, key=f"gate-item-{suffix}",
            detail_id=product.detail_id, source_shop_id="gate-shop",
            product_id=product.id,
        ))
        db.commit()
        product_id = str(product.id)
    finally:
        db.close()

    def review_gate(token):
        return client.post(
            API + "/images/review-gate",
            json={"warehouseId": product_id}, headers=_headers(token))

    def pre_save_gate(token):
        return client.post(
            API + "/warehouse/pre-save-gate",
            json={"warehouseId": product_id, "skipImages": True},
            headers=_headers(token))

    for request in (review_gate, pre_save_gate):
        assert request(owner_token).status_code == 200
        denied = request(other_token)
        assert denied.status_code == 403, denied.text
        allowed = request(teacher_token)
        assert allowed.status_code == 200, allowed.text


def test_member_title_preview_cannot_read_foreign_automation_result(client):
    db = SessionLocal()
    try:
        suffix = uuid.uuid4().hex[:10]
        owner, owner_token = operator_service.create(
            db, f"title-owner-{suffix}", role=operator_service.ROLE_MEMBER)
        other, _other_token = operator_service.create(
            db, f"title-other-{suffix}", role=operator_service.ROLE_MEMBER)
        account = Account(name=f"title-account-{suffix}",
                          app_key=f"title-key-{suffix}", app_secret_enc="",
                          shops="[]", is_default=False)
        db.add(account)
        db.flush()

        products: dict[str, Product] = {}
        for label, product_owner in (("own", owner), ("foreign", other)):
            product = Product(
                warehouse_key=f"title-{label}-{suffix}",
                detail_id=f"title-{label}-{suffix}", shop_id="title-shop",
                account_id=account.id, title=f"{label} private title", payload={},
                automation_result=True,
            )
            run = AutomationRun(
                id=f"title-{label}-{suffix}", owner_operator_id=product_owner.id,
                account_id=account.id, shop_id="title-shop", settings={})
            db.add_all([product, run])
            db.flush()
            db.add(AutomationRunItem(
                run_id=run.id, key=f"{label}-{suffix}",
                detail_id=product.detail_id, source_shop_id="title-shop",
                product_id=product.id,
            ))
            products[label] = product

        shared = Product(
            warehouse_key=f"title-shared-{suffix}",
            detail_id=f"title-shared-{suffix}", shop_id="title-shop",
            account_id=account.id, title="shared finished title", payload={},
            automation_result=False,
        )
        db.add(shared)
        db.commit()
        own_id, foreign_id, shared_id = (
            products["own"].id, products["foreign"].id, shared.id)
    finally:
        db.close()

    headers = _headers(owner_token)

    def preview(**body):
        return client.post(API + "/titles/preview", json=body, headers=headers)

    assert preview(warehouseIds=[str(own_id)]).status_code == 200
    assert preview(warehouseIds=[str(shared_id)]).status_code == 200
    denied = preview(warehouseIds=[str(foreign_id)])
    assert denied.status_code == 403, denied.text

    # Explicit text takes precedence over warehouseIds and never reads the ID.
    text_only = preview(titles=["caller supplied title"],
                        warehouseIds=[str(foreign_id)])
    assert text_only.status_code == 200, text_only.text
    assert text_only.json()["items"][0]["original"] == "caller supplied title"


def test_member_group_counts_exclude_foreign_automation_results(client):
    db = SessionLocal()
    try:
        suffix = uuid.uuid4().hex[:10]
        owner, owner_token = operator_service.create(
            db, f"group-owner-{suffix}", role=operator_service.ROLE_MEMBER)
        other, _other_token = operator_service.create(
            db, f"group-other-{suffix}", role=operator_service.ROLE_MEMBER)
        account = Account(name=f"group-account-{suffix}",
                          app_key=f"group-key-{suffix}", app_secret_enc="",
                          shops="[]", is_default=False)
        group = Group(name=f"group-{suffix}")
        db.add_all([account, group])
        db.flush()

        def add_product(label: str, *, automation_result: bool,
                        product_owner=None):
            product = Product(
                warehouse_key=f"group-{label}-{suffix}",
                detail_id=f"group-{label}-{suffix}", shop_id="group-shop",
                account_id=account.id, title=f"{label} title", payload={},
                group_id=group.id, group_name=group.name,
                automation_result=automation_result,
            )
            db.add(product)
            if automation_result:
                run = AutomationRun(
                    id=f"group-{label}-{suffix}",
                    owner_operator_id=product_owner.id,
                    account_id=account.id, shop_id="group-shop", settings={})
                db.add(run)
                db.flush()
                db.add(AutomationRunItem(
                    run_id=run.id, key=f"{label}-{suffix}",
                    detail_id=product.detail_id, source_shop_id="group-shop",
                    product_id=product.id,
                ))
            return product

        add_product("own-result", automation_result=True, product_owner=owner)
        add_product("foreign-result", automation_result=True, product_owner=other)
        add_product("shared-finished", automation_result=False)
        db.commit()
        group_id = group.id
    finally:
        db.close()

    response = client.get(API + "/groups", headers=_headers(owner_token))
    assert response.status_code == 200, response.text
    visible_group = next(
        item for item in response.json()["groups"]
        if item["id"] == str(group_id))
    assert visible_group["count"] == 2
