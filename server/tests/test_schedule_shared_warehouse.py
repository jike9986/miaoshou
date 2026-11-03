from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from core import errors
from db.session import Base
from models.account import Account
from models.account_binding import AccountBinding
from models.operator import Operator
from models.product import Product
from models.product_claim import ProductClaim
from models.publish_log import PublishLog
from models.schedule import Schedule
from services import schedule_service


@pytest.fixture
def shared_warehouse():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            first = Operator(name="first", token="first-token", role="member")
            second = Operator(name="second", token="second-token", role="member")
            db.add_all([first, second])
            db.flush()
            source = Account(name="teacher-source", app_key="source-key")
            target = Account(
                name="member-target", app_key="target-key",
                owner_operator_id=first.id,
                shops=json.dumps([{"shopId": "9", "name": "Target shop", "site": "MY"}]),
            )
            db.add_all([source, target])
            db.flush()
            db.add_all([
                AccountBinding(account_id=target.id, operator_id=first.id),
                AccountBinding(account_id=target.id, operator_id=second.id),
            ])
            db.commit()
            yield db, first, second, source, target
    finally:
        engine.dispose()


def _product(db, source, detail_id, **fields):
    product = Product(
        account_id=source.id, warehouse_key=f"{detail_id}:7",
        detail_id=detail_id, shop_id="7", title="Edited finished product", **fields,
    )
    db.add(product)
    db.flush()
    return product


def test_member_schedule_selects_claimed_shared_goods_independently_of_source_state(
        shared_warehouse):
    db, first, second, source, target = shared_warehouse
    shared = _product(db, source, "shared", status="published")
    others = _product(db, source, "others")
    revoked = _product(db, source, "revoked")
    archived = _product(db, source, "archived", status="archived")
    deleted = _product(db, source, "deleted", deleted_at=1)
    private_result = _product(db, source, "private", automation_result=True)
    _product(db, source, "unclaimed")
    db.add_all([
        ProductClaim(product_id=product.id, owner_operator_id=first.id)
        for product in (shared, archived, deleted, private_result)
    ])
    db.add_all([
        ProductClaim(product_id=others.id, owner_operator_id=second.id),
        ProductClaim(product_id=revoked.id, owner_operator_id=first.id, status="cancelled"),
        PublishLog(account_id=target.id, product_id=shared.id, detail_id="other-carrier",
                   warehouse_key=shared.warehouse_key, shop_id="9", status="success",
                   operator_id=second.id),
    ])
    db.commit()

    items = schedule_service.pick_products(
        db, account_id=target.id, count=10, operator_id=first.id)

    assert [item["id"] for item in items] == [shared.id]


@pytest.mark.parametrize("status, eligible", [
    ("processing", False), ("success", False), ("fail", True), ("cancel", True),
])
def test_member_schedule_uses_own_target_publish_ledger(shared_warehouse, status, eligible):
    db, first, _second, source, target = shared_warehouse
    product = _product(db, source, "claimed", status="published")
    db.add(ProductClaim(product_id=product.id, owner_operator_id=first.id))
    db.add(PublishLog(
        account_id=target.id, product_id=product.id, detail_id="rebuilt-carrier",
        warehouse_key=product.warehouse_key, shop_id="9", status=status,
        operator_id=first.id,
    ))
    db.commit()

    items = schedule_service.pick_products(
        db, account_id=target.id, count=10, operator_id=first.id)

    assert [item["id"] for item in items] == ([product.id] if eligible else [])


def test_old_account_and_ownerless_publish_logs_do_not_block_member_schedule(shared_warehouse):
    db, first, _second, source, target = shared_warehouse
    product = _product(db, source, "claimed", status="published")
    db.add(ProductClaim(product_id=product.id, owner_operator_id=first.id))
    db.add_all([
        PublishLog(account_id=source.id, product_id=product.id, detail_id="old-carrier",
                   warehouse_key=product.warehouse_key, shop_id="7", status="success",
                   operator_id=first.id),
        PublishLog(account_id=target.id, product_id=product.id, detail_id="legacy-carrier",
                   warehouse_key=product.warehouse_key, shop_id="9", status="success"),
    ])
    db.commit()

    items = schedule_service.pick_products(
        db, account_id=target.id, count=10, operator_id=first.id)

    assert [item["id"] for item in items] == [product.id]


def test_revoked_target_binding_cannot_select_claimed_shared_goods(shared_warehouse):
    db, first, _second, source, target = shared_warehouse
    product = _product(db, source, "claimed")
    db.add(ProductClaim(product_id=product.id, owner_operator_id=first.id))
    binding = db.query(AccountBinding).filter_by(
        account_id=target.id, operator_id=first.id).one()
    db.delete(binding)
    db.commit()

    with pytest.raises(errors.AppError) as caught:
        schedule_service.pick_products(
            db, account_id=target.id, count=10, operator_id=first.id)

    assert caught.value.status == 403


def test_shared_product_execution_keeps_target_shop_and_owner(shared_warehouse, monkeypatch):
    db, first, _second, source, target = shared_warehouse
    product = _product(db, source, "claimed", status="published")
    db.add(ProductClaim(product_id=product.id, owner_operator_id=first.id))
    task = Schedule(
        id="shared-schedule", name="Shared product schedule", account_id=target.id,
        owner_operator_id=first.id, shop_id="9", template_name="member-template",
        frequency="daily", time_of_day="09:00", per_run=10, claim_only=False,
    )
    db.add(task)
    db.commit()
    calls = []
    monkeypatch.setattr("services.setting_service.is_read_only", lambda _db: False)

    def publish(_db, payload, **kwargs):
        calls.append((payload, kwargs))
        return {"published": ["carrier"], "failures": []}

    monkeypatch.setattr("services.publish_service.publish", publish)

    result = schedule_service.execute(db, task)

    assert result["status"] == schedule_service.RUN_OK
    assert result["selected"] == 1
    assert calls[0][0]["items"] == [{"warehouseId": str(product.id)}]
    assert calls[0][0]["accountId"] == target.id
    assert calls[0][0]["shopId"] == "9"
    assert calls[0][0]["template"] == "member-template"
    assert calls[0][1] == {"operator_id": first.id, "_log_operator_id": first.id}
