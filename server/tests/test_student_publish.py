from __future__ import annotations

import uuid

import pytest

from core import errors
from db.session import SessionLocal, init_db
from models.account import Account
from models.product import Product
from models.product_claim import ProductClaim
from models.publish_log import PublishLog
from services import operator_service, publish_service, setting_service, warehouse_service
from services.price_template import DEFAULT_TEMPLATE


@pytest.fixture
def publish_db():
    init_db()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


def _identity(db, suffix: str, *, role: str = operator_service.ROLE_MEMBER):
    operator, _token = operator_service.create(db, f"student-{suffix}", role=role)
    account = Account(name=f"account-{suffix}", shops="[]")
    db.add(account)
    db.flush()
    return operator, account


def _product(db, suffix: str, account: Account) -> Product:
    row = Product(
        warehouse_key=f"{suffix}:source-shop", account_id=account.id,
        detail_id="12345", shop_id="123", shop_name="Target shop",
        title="Finished title", status="ready", claimed=False,
        claimed_at=None, claimed_shop_id=None, collected={},
        price_min=10, price_max=10, payload={
            "weight": "0.5", "price": "10", "originPrice": "10",
            "skuMap": {"sku-1": {"price": "10", "originPrice": "10", "weight": "0.5"}},
        },
    )
    db.add(row)
    db.flush()
    return row


def _publish_payload(product: Product, account: Account, **overrides):
    return {
        "confirm": True,
        "accountId": str(account.id),
        "shopId": "123",
        "template": "Student pricing",
        "items": [{"warehouseId": str(product.id)}],
        **overrides,
    }


def _stub_member_publish(monkeypatch, db, account, *, received_payloads: list | None = None):
    class FakeClient:
        def publish_items(self, detail_ids, shop_ids):
            self.published = (detail_ids, shop_ids)

    client = FakeClient()
    monkeypatch.setattr(publish_service, "account_client_for", lambda *_args: (account, client))
    monkeypatch.setattr(publish_service, "require_shop", lambda *_args: {
        "shopId": "123", "name": "Target shop", "site": "MY",
    })
    monkeypatch.setattr(publish_service.setting_service, "publish_enabled", lambda *_args: True)
    monkeypatch.setattr(publish_service, "_in_flight_blocker", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(publish_service, "hold_for_session", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(publish_service.carrier_service, "harvest_from_payloads",
                        lambda *_args, **_kwargs: None)
    monkeypatch.setattr(publish_service.carrier_service, "collect_carriers",
                        lambda *_args, **_kwargs: [90001])

    def fill_carriers(_client, _carriers, payloads, *, shop_id):
        if received_payloads is not None:
            received_payloads.extend(payloads)
        assert shop_id == 123
        return {"saved": [98765], "savedSources": [0], "failures": []}

    monkeypatch.setattr(publish_service.carrier_service, "fill_carriers", fill_carriers)
    template = dict(DEFAULT_TEMPLATE)
    template.update({"name": "Student pricing", "site": "MY", "profitPercent": 10})
    setting_service.save_template(db, template)
    return client


def test_member_can_publish_claimed_product_with_template_without_mutating_shared_product(
    publish_db, monkeypatch,
):
    db = publish_db
    suffix = uuid.uuid4().hex[:8]
    operator, account = _identity(db, suffix)
    product = _product(db, suffix, account)
    db.add(ProductClaim(product_id=product.id, owner_operator_id=operator.id))
    db.flush()
    original_payload = dict(product.payload)
    received_payloads: list[dict] = []
    _stub_member_publish(monkeypatch, db, account, received_payloads=received_payloads)

    result = publish_service.publish(
        db, _publish_payload(product, account), operator_id=operator.id)

    assert result["mode"] == "carrier"
    assert result["repriced"]
    assert received_payloads[0]["skuMap"]["sku-1"]["price"] != "10"
    assert product.payload == original_payload
    assert product.status == "ready"
    assert product.claimed is False
    assert product.claimed_at is None
    assert product.claimed_shop_id is None
    log = db.query(PublishLog).filter(PublishLog.product_id == product.id).one()
    assert log.operator_id == operator.id


@pytest.mark.parametrize("case", ["unclaimed", "owned-by-other", "detail-id-bypass"])
def test_member_cannot_publish_unclaimed_foreign_or_direct_detail_item(
    publish_db, monkeypatch, case,
):
    db = publish_db
    suffix = uuid.uuid4().hex[:8]
    operator, account = _identity(db, suffix)
    product = _product(db, suffix, account)
    if case == "owned-by-other":
        other, _ = _identity(db, f"other-{suffix}")
        db.add(ProductClaim(product_id=product.id, owner_operator_id=other.id))
    db.flush()
    _stub_member_publish(monkeypatch, db, account)
    payload = _publish_payload(product, account)
    if case == "detail-id-bypass":
        payload["items"] = [{"detailId": "12345", "shopId": "123"}]

    with pytest.raises(errors.AppError) as exc:
        publish_service.publish(db, payload, operator_id=operator.id)
    assert exc.value.status == 403


def test_member_publish_status_and_history_are_owner_scoped(publish_db):
    db = publish_db
    suffix = uuid.uuid4().hex[:8]
    first, account = _identity(db, suffix)
    second, _ = _identity(db, f"second-{suffix}")
    product = _product(db, suffix, account)
    db.add_all([
        ProductClaim(product_id=product.id, owner_operator_id=first.id),
        ProductClaim(product_id=product.id, owner_operator_id=second.id),
        PublishLog(account_id=account.id, product_id=product.id,
                   detail_id="12345", shop_id="456", title="first",
                   status="success", operator_id=first.id),
    ])
    product.status = "published"  # 模拟旧版/老师的共享全局状态
    db.flush()

    first_list = warehouse_service.list_products(db, operator_id=first.id)["items"]
    second_list = warehouse_service.list_products(db, operator_id=second.id)["items"]
    first_item = next(item for item in first_list if item["id"] == str(product.id))
    second_item = next(item for item in second_list if item["id"] == str(product.id))
    assert first_item["status"] == "published"
    assert first_item["publishStatus"] == "success"
    assert second_item["status"] == "claimed"
    assert second_item["publishStatus"] is None
    assert warehouse_service.stats(db, operator_id=first.id)["totalPublished"] == 1
    assert warehouse_service.stats(db, operator_id=second.id)["totalPublished"] == 0

    first_detail = warehouse_service.detail(db, product.id, operator_id=first.id)
    second_detail = warehouse_service.detail(db, product.id, operator_id=second.id)
    assert [row["shopId"] for row in first_detail["publishHistory"]] == ["456"]
    assert first_detail["publishHistory"][0]["status"] == "success"
    assert second_detail["publishHistory"] == []
    assert second_detail["product"]["status"] == "claimed"
