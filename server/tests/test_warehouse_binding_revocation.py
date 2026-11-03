from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from db.session import Base
from models.account import Account
from models.account_binding import AccountBinding
from models.operator import Operator
from models.product import Product
from models.product_claim import ProductClaim
from models.publish_log import PublishLog
from services import account_service, warehouse_service


@pytest.mark.parametrize("revocation", ["explicit_binding", "disabled_legacy", "reassigned"])
@pytest.mark.parametrize("claimed", [False, True])
def test_warehouse_ignores_publish_history_from_inaccessible_accounts(revocation, claimed):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            member = Operator(name="member", token="member-token", role="member")
            other = Operator(name="other", token="other-token", role="member")
            db.add_all([member, other])
            db.flush()
            current = Account(name="current", app_key="current-key", owner_operator_id=member.id)
            legacy = Account(
                name="legacy", app_key="legacy-key", owner_operator_id=member.id,
                legacy_owner_fallback_disabled=revocation == "disabled_legacy",
            )
            db.add_all([current, legacy])
            db.flush()
            if revocation == "explicit_binding":
                db.add(AccountBinding(account_id=current.id, operator_id=member.id))
            elif revocation == "reassigned":
                db.add(AccountBinding(account_id=legacy.id, operator_id=other.id))
            product = Product(
                warehouse_key="101:7", detail_id="101", shop_id="7",
                account_id=legacy.id, title="Shared finished product",
            )
            db.add(product)
            db.flush()
            db.add(PublishLog(
                account_id=legacy.id, product_id=product.id, detail_id="101",
                shop_id="7", status="success", operator_id=member.id,
            ))
            if claimed:
                db.add(ProductClaim(product_id=product.id, owner_operator_id=member.id))
            db.commit()

            assert account_service.has_account_access(db, legacy.id, member.id) is False
            detail = warehouse_service.detail(db, product.id, operator_id=member.id)
            assert detail["publishHistory"] == []
            assert detail["product"]["status"] == ("claimed" if claimed else "ready")
            assert detail["product"]["publishStatus"] is None

            published = warehouse_service.list_products(
                db, operator_id=member.id, status="published")
            assert published["total"] == 0
            assert published["stats"]["totalPublished"] == 0
            for status, expected in (("ready", not claimed), ("claimed", claimed)):
                result = warehouse_service.list_products(db, operator_id=member.id, status=status)
                assert result["total"] == int(expected)
                if expected:
                    assert result["items"][0]["status"] == status
                    assert result["items"][0]["publishedShopIds"] == []
    finally:
        engine.dispose()


@pytest.mark.parametrize("latest_status", ["failed", "processing"])
@pytest.mark.parametrize("successful_shop_id", ["A", ""])
def test_successful_publish_state_survives_a_later_unsuccessful_attempt(
    latest_status, successful_shop_id,
):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            member = Operator(name="member", token="member-token", role="member")
            db.add(member)
            db.flush()
            account = Account(name="current", app_key="current-key")
            db.add(account)
            db.flush()
            db.add(AccountBinding(account_id=account.id, operator_id=member.id))
            product = Product(
                warehouse_key="101:source", detail_id="101", shop_id="source",
                account_id=account.id, title="Shared finished product",
            )
            db.add(product)
            db.flush()
            db.add(ProductClaim(product_id=product.id, owner_operator_id=member.id))
            db.add_all([
                PublishLog(
                    account_id=account.id, product_id=product.id, detail_id="101",
                    shop_id=successful_shop_id, status="success", operator_id=member.id,
                    requested_at=100,
                ),
                PublishLog(
                    account_id=account.id, product_id=product.id, detail_id="101",
                    shop_id="B", status=latest_status, operator_id=member.id,
                    requested_at=200,
                ),
            ])
            db.commit()

            published = warehouse_service.list_products(
                db, operator_id=member.id, status="published")
            assert published["total"] == 1
            assert published["stats"]["totalPublished"] == 1
            detail = warehouse_service.detail(db, product.id, operator_id=member.id)
            for item in (published["items"][0], detail["product"]):
                assert item["status"] == "published"
                assert item["publishStatus"] == latest_status
                assert item["publishShopId"] == "B"
                assert item["publishedShopIds"] == ([successful_shop_id]
                                                      if successful_shop_id else [])
            for status in ("ready", "claimed"):
                result = warehouse_service.list_products(
                    db, operator_id=member.id, status=status)
                assert result["total"] == 0
    finally:
        engine.dispose()
