from __future__ import annotations

import uuid

import pytest

from core import errors
from db.session import SessionLocal, init_db
from models.account import Account
from models.account_binding import AccountBinding
from models.automation_run import AutomationRun, AutomationRunItem
from models.product import Product
from services import automation_run_service, operator_service, warehouse_service


def test_revoking_account_binding_hides_automation_history_but_keeps_shared_product():
    init_db()
    db = SessionLocal()
    try:
        suffix = uuid.uuid4().hex[:10]
        member, _ = operator_service.create(
            db, f"revoke-member-{suffix}", role=operator_service.ROLE_MEMBER)
        other_member, _ = operator_service.create(
            db, f"revoke-other-{suffix}", role=operator_service.ROLE_MEMBER)
        teacher, _ = operator_service.create(
            db, f"revoke-teacher-{suffix}", role=operator_service.ROLE_TEACHER)
        account = Account(
            name=f"revoke-account-{suffix}", app_key=f"revoke-key-{suffix}",
            owner_operator_id=teacher.id, status="active", shops="[]",
            is_default=False,
        )
        db.add(account)
        db.flush()
        db.add_all([
            AccountBinding(account_id=account.id, operator_id=member.id),
            AccountBinding(account_id=account.id, operator_id=other_member.id),
        ])

        run = AutomationRun(
            id=f"arun_revoke_{suffix}", owner_operator_id=member.id,
            account_id=account.id, shop_id="7", status="completed", settings={},
        )
        db.add(run)
        db.flush()
        automation_result = Product(
            warehouse_key=f"revoke-result-{suffix}", account_id=account.id,
            detail_id=f"{suffix}1", shop_id="7", title="private automation result",
            automation_result=True,
        )
        shared_product = Product(
            warehouse_key=f"revoke-shared-{suffix}", account_id=account.id,
            detail_id=f"{suffix}2", shop_id="7", title="shared finished product",
            automation_result=False,
        )
        db.add_all([automation_result, shared_product])
        db.flush()
        db.add(AutomationRunItem(
            run_id=run.id, key=automation_result.warehouse_key,
            detail_id=automation_result.detail_id, source_shop_id="7",
            product_id=automation_result.id, process_status="succeeded",
        ))
        db.commit()

        assert automation_run_service.get(db, run.id, member).id == run.id
        visible_before_revoke = {
            product.id for product in warehouse_service._scope_member_products(
                db, db.query(Product), member.id).all()
        }
        assert automation_result.id in visible_before_revoke
        assert shared_product.id in visible_before_revoke
        assert warehouse_service.detail(db, shared_product.id, operator_id=member.id)

        db.query(AccountBinding).filter(
            AccountBinding.account_id == account.id,
            AccountBinding.operator_id == member.id,
        ).delete(synchronize_session=False)
        db.commit()
        db.expire_all()

        with pytest.raises(errors.AppError) as revoked_run:
            automation_run_service.get(db, run.id, member)
        assert revoked_run.value.code == errors.NOT_FOUND

        with pytest.raises(errors.AppError) as revoked_result:
            warehouse_service.detail(db, automation_result.id, operator_id=member.id)
        assert revoked_result.value.status == 403

        visible_after_revoke = {
            product.id for product in warehouse_service._scope_member_products(
                db, db.query(Product), member.id).all()
        }
        assert automation_result.id not in visible_after_revoke
        assert shared_product.id in visible_after_revoke
        assert warehouse_service.detail(db, shared_product.id, operator_id=member.id)
        assert automation_run_service.get(db, run.id, teacher).id == run.id
        assert warehouse_service.detail(db, automation_result.id, operator_id=teacher.id)
    finally:
        db.rollback()
        db.close()
