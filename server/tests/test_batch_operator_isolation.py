from __future__ import annotations

import json
import uuid

import pytest

from core import errors
from core.utils import now_ts
from db.session import SessionLocal, init_db
from models.account import Account
from models.account_binding import AccountBinding
from models.batch import Batch, BatchItem
from services import batch_service, operator_service


def test_member_batch_service_access_is_owner_scoped():
    init_db()
    db = SessionLocal()
    try:
        suffix = uuid.uuid4().hex[:10]
        first, _ = operator_service.create(
            db, f"batch-owner-{suffix}", role=operator_service.ROLE_MEMBER)
        second, _ = operator_service.create(
            db, f"batch-other-{suffix}", role=operator_service.ROLE_MEMBER)
        teacher, _ = operator_service.create(
            db, f"batch-teacher-{suffix}", role=operator_service.ROLE_TEACHER)
        account = Account(
            name=f"batch-account-{suffix}", app_key=f"batch-key-{suffix}",
            owner_operator_id=first.id, status="active", is_default=False,
            shops=json.dumps([{"shopId": "7", "name": "Shared shop", "site": "MY"}]),
        )
        db.add(account)
        db.flush()
        db.add_all([
            AccountBinding(account_id=account.id, operator_id=first.id,
                           bound_by_operator_id=first.id, bound_at=now_ts()),
            AccountBinding(account_id=account.id, operator_id=second.id,
                           bound_by_operator_id=teacher.id, bound_at=now_ts()),
        ])
        batch = batch_service.create(
            db, account_id=account.id, shop_id="7", detail_ids=["101"],
            settings={}, operator=first)
        batch_id = batch.id
        db.commit()

        assert batch_service.get(db, batch_id, operator=first).id == batch_id
        assert [row["id"] for row in batch_service.list_batches(
            db, account.id, operator=first)["items"]] == [batch_id]
        assert batch_service.get(db, batch_id, operator=teacher).id == batch_id
        assert batch_service.list_batches(
            db, account.id, operator=teacher)["total"] == 1

        with pytest.raises(errors.AppError) as cross_owner_read:
            batch_service.get(db, batch_id, operator=second)
        assert cross_owner_read.value.code == errors.NOT_FOUND

        denied_operations = (
            lambda: batch_service.run_once(db, batch_id, operator=second),
            lambda: batch_service.control(db, batch_id, "pause", operator=second),
            lambda: batch_service.retry_failed(db, batch_id, operator=second),
            lambda: batch_service.delete(db, batch_id, operator=second),
        )
        for operation in denied_operations:
            with pytest.raises(errors.AppError) as cross_owner_write:
                operation()
            assert cross_owner_write.value.code == errors.NOT_FOUND

        with pytest.raises(errors.AppError) as forged_creator:
            batch_service.create(
                db, account_id=account.id, shop_id="7", detail_ids=["102"],
                settings={}, operator_id=second.id, operator=first)
        assert forged_creator.value.code == errors.FORBIDDEN

        persisted = db.query(Batch).filter(Batch.id == batch_id).one()
        item = db.query(BatchItem).filter(BatchItem.batch_id == batch_id).one()
        assert persisted.operator_id == first.id
        assert persisted.status == batch_service.STATUS_PENDING
        assert item.status == batch_service.ITEM_PENDING
    finally:
        db.rollback()
        db.close()
