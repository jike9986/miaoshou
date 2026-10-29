from __future__ import annotations

import pytest

from core import errors
from db.session import SessionLocal, init_db
from models.account import Account
from models.batch import BatchItem
from services import batch_service


class SimulatedProcessCrash(BaseException):
    pass


def test_running_intent_survives_processor_crash_and_blocks_duplicate() -> None:
    init_db()
    db = SessionLocal()
    try:
        batch = batch_service.create(
            db, account_id=None, shop_id="123", detail_ids=["456"],
            settings={"save": True},
        )
        batch_id = batch.id
        db.commit()
    finally:
        db.close()

    snapshot = {"detailId": "456", "title": "product"}
    processor_calls = 0

    def crash_after_intent(db, account, client, detail_id, shop_id, settings):
        nonlocal processor_calls
        processor_calls += 1
        observer = SessionLocal()
        try:
            item = observer.query(BatchItem).filter(
                BatchItem.batch_id == batch_id,
                BatchItem.detail_id == detail_id,
            ).one()
            assert item.status == batch_service.ITEM_RUNNING
            assert item.payload_hash == batch_service.snapshot_hash(snapshot)
            assert batch_service.get(observer, batch_id).status == batch_service.STATUS_RUNNING
        finally:
            observer.close()
        raise SimulatedProcessCrash()

    db = SessionLocal()
    try:
        with pytest.raises(SimulatedProcessCrash):
            batch_service.run_once(
                db, batch_id, loader=lambda *_: snapshot,
                processor=crash_after_intent, account=Account(), client=object(),
            )
    finally:
        db.close()

    reopened = SessionLocal()
    try:
        with pytest.raises(errors.AppError) as raised:
            batch_service.run_once(
                reopened, batch_id, loader=lambda *_: snapshot,
                processor=lambda *_: pytest.fail("uncertain item must not be rerun"),
                account=Account(), client=object(),
            )
        assert raised.value.code == errors.RETRY_UNSAFE
        assert processor_calls == 1
    finally:
        reopened.close()
