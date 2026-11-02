from __future__ import annotations

import json
import uuid

import pytest

from core import errors
from core.utils import now_ts
from db.session import SessionLocal, init_db
from models.account import Account
from models.account_binding import AccountBinding
from models.product import Product
from models.product_claim import ProductClaim
from models.schedule import Schedule
from services import operator_service, schedule_service


@pytest.fixture
def shared_account_products():
    init_db()
    db = SessionLocal()
    try:
        suffix = uuid.uuid4().hex[:10]
        first, _ = operator_service.create(
            db, f"schedule-first-{suffix}", role=operator_service.ROLE_MEMBER)
        second, _ = operator_service.create(
            db, f"schedule-second-{suffix}", role=operator_service.ROLE_MEMBER)
        account = Account(
            name=f"schedule-account-{suffix}", app_key=f"schedule-key-{suffix}",
            owner_operator_id=first.id, status="active", is_default=False,
            shops=json.dumps([{"shopId": "7", "name": "Shared shop", "site": "MY"}]),
        )
        db.add(account)
        db.flush()
        db.add_all([
            AccountBinding(account_id=account.id, operator_id=first.id,
                           bound_by_operator_id=first.id, bound_at=now_ts()),
            AccountBinding(account_id=account.id, operator_id=second.id,
                           bound_by_operator_id=first.id, bound_at=now_ts()),
        ])
        products = []
        for index in range(3):
            product = Product(
                warehouse_key=f"schedule-{suffix}-{index}:7", account_id=account.id,
                detail_id=f"{suffix}{index}", shop_id="7", title=f"Product {index}",
                status="ready", saved_at=now_ts() + index,
            )
            db.add(product)
            products.append(product)
        db.flush()
        db.add_all([
            ProductClaim(product_id=products[0].id, owner_operator_id=first.id),
            ProductClaim(product_id=products[1].id, owner_operator_id=second.id),
        ])
        task = Schedule(
            id=f"claim-{suffix}", name="Member schedule", account_id=account.id,
            owner_operator_id=first.id, shop_id="7", template_name="",
            frequency="daily", days=[], day_of_month=[], weekdays=[],
            start_date="", end_date="", time_of_day="09:00", per_run=10,
            claim_only=False, enabled=True, last_run_date="", attempt_date="",
            attempts=0, next_due_at=None, last_result={}, created_at=now_ts(),
            updated_at=now_ts(),
        )
        db.add(task)
        db.commit()
        yield db, account, first, second, products, task
    finally:
        db.rollback()
        db.close()


def test_member_schedule_only_selects_personally_claimed_products(shared_account_products):
    db, account, first, second, products, _task = shared_account_products

    first_items = schedule_service.pick_products(
        db, account_id=account.id, count=10, operator_id=first.id)
    second_items = schedule_service.pick_products(
        db, account_id=account.id, count=10, operator_id=second.id)

    assert [item["id"] for item in first_items] == [products[0].id]
    assert [item["id"] for item in second_items] == [products[1].id]


def test_member_schedule_publish_carries_owner_for_publish_guards(
        shared_account_products, monkeypatch):
    db, _account, first, _second, products, task = shared_account_products
    calls = []

    monkeypatch.setattr("services.setting_service.is_read_only", lambda _db: False)

    def publish(_db, payload, **kwargs):
        calls.append((payload, kwargs))
        return {"published": [products[0].detail_id], "failures": []}

    monkeypatch.setattr("services.publish_service.publish", publish)

    result = schedule_service.execute(db, task, with_results=True)

    assert result["status"] == schedule_service.RUN_OK
    assert calls[0][0]["items"] == [{"warehouseId": str(products[0].id)}]
    assert calls[0][1] == {
        "operator_id": first.id,
        "_log_operator_id": first.id,
    }


def test_member_cannot_schedule_claim_only_jobs(shared_account_products):
    db, account, first, _second, _products, _task = shared_account_products

    with pytest.raises(errors.AppError) as caught:
        schedule_service.pick_products(
            db, account_id=account.id, count=1, claim_only=True,
            operator_id=first.id)

    assert getattr(caught.value, "status", None) == 403
