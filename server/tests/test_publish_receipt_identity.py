"""A receipt must identify this request, never an older publish of the same draft."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from api import publish as publish_api
from db.session import Base
from models.account import Account
from models.publish_log import PublishLog

REQUESTED_AT = 1793678813.908
BEIJING = timezone(timedelta(hours=8))


def created_at(stamp):
    return datetime.fromtimestamp(stamp, BEIJING).strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture
def receipt_db(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        account = Account(name="Receipt identity test", app_key="test")
        db.add(account)
        db.flush()
        receipts = []

        class Client:
            def publish_records(self, **_kwargs):
                return {"data": {"moveCollectDetailList": receipts, "total": len(receipts)}}

            def close(self):
                pass

        monkeypatch.setattr(publish_api.publish_service, "account_client_for",
                            lambda *_args: (account, Client()))
        yield db, account, receipts
    engine.dispose()


def pending_log(db, account, *, owner=1, **kwargs):
    row = PublishLog(
        account_id=account.id, operator_id=owner, detail_id="123", shop_id="9",
        batch_id="current-batch", status="processing", reason="Waiting for receipt",
        requested_at=REQUESTED_AT, last_requested_at=REQUESTED_AT,
        detail={"claimOnly": False},
    )
    for key, value in kwargs.items():
        setattr(row, key, value)
    db.add(row)
    db.commit()
    return row


def receipt(*, stamp=REQUESTED_AT + 1, receipt_id="task-current", **kwargs):
    return {"collectBoxDetailId": "123", "shopId": "9", "status": "success",
            "moveCollectTaskDetailId": receipt_id, "gmtCreate": created_at(stamp),
            "platformItemId": "platform-123", **kwargs}


def refresh(db, account, **kwargs):
    return publish_api._refresh_processing(db, str(account.id), **kwargs)["updated"]


def test_current_receipt_keeps_upstream_request_and_platform_identity(receipt_db):
    db, account, receipts = receipt_db
    row = pending_log(db, account)
    receipts.append(receipt())
    assert refresh(db, account) == 1
    assert (row.status, row.platform_item_id) == ("success", "platform-123")
    assert row.detail == {"claimOnly": False, "moveCollectTaskDetailId": "task-current",
                          "receiptAttemptAt": REQUESTED_AT}


def test_old_receipt_modified_after_retry_does_not_complete_new_attempt(receipt_db):
    db, account, receipts = receipt_db
    row = pending_log(db, account, attempts=2, requested_at=REQUESTED_AT - 3600)
    receipts.append(receipt(stamp=REQUESTED_AT - 60,
                            gmtModified=created_at(REQUESTED_AT + 60)))
    assert refresh(db, account) == 0
    assert (row.status, row.reason, row.finished_at) == (
        "processing", "Waiting for receipt", None)


@pytest.mark.parametrize("timestamp", [None, "", "not-a-time", "2026-02-30 12:00:00", 1793678815])
def test_missing_or_unreadable_request_time_remains_pending(receipt_db, timestamp):
    db, account, receipts = receipt_db
    row = pending_log(db, account)
    receipts.append(receipt(gmtCreate=timestamp))
    assert refresh(db, account) == 0
    assert row.status == "processing"
    assert "moveCollectTaskDetailId" not in row.detail


def test_second_precision_accepts_current_second_in_beijing_timezone(receipt_db):
    db, account, receipts = receipt_db
    row = pending_log(db, account)
    receipts.append(receipt(stamp=REQUESTED_AT))
    assert refresh(db, account) == 1
    assert row.status == "success"


def test_only_current_receipt_is_selected_when_old_attempt_also_exists(receipt_db):
    db, account, receipts = receipt_db
    row = pending_log(db, account, attempts=2)
    receipts.extend([receipt(stamp=REQUESTED_AT - 60, receipt_id="task-old", status="fail"),
                     receipt()])
    assert refresh(db, account) == 1
    assert row.status == "success"
    assert row.detail["moveCollectTaskDetailId"] == "task-current"


def test_multiple_current_requests_cannot_be_guessed(receipt_db):
    db, account, receipts = receipt_db
    row = pending_log(db, account)
    receipts.extend([receipt(), receipt(receipt_id="task-other")])
    assert refresh(db, account) == 0
    assert row.status == "processing"


def test_owner_filter_does_not_hide_ambiguous_request_of_another_user(receipt_db):
    db, account, receipts = receipt_db
    first = pending_log(db, account, owner=1)
    second = pending_log(db, account, owner=2)
    receipts.append(receipt())
    assert refresh(db, account, operator_id=first.operator_id) == 0
    assert (first.status, second.status) == ("processing", "processing")


def test_first_processing_receipt_pins_upstream_request_id(receipt_db):
    db, account, receipts = receipt_db
    row = pending_log(db, account)
    receipts.append(receipt(status="processing"))
    assert refresh(db, account) == 1
    receipts[:] = [receipt(receipt_id="task-other")]
    assert refresh(db, account) == 0
    assert row.status == "processing"
    receipts[:] = [receipt()]
    assert refresh(db, account) == 1
    assert row.status == "success"


def test_retry_rebinds_receipt_id_for_new_attempt(receipt_db):
    db, account, receipts = receipt_db
    row = pending_log(db, account, attempts=2,
                      detail={"claimOnly": False, "moveCollectTaskDetailId": "task-old",
                              "receiptAttemptAt": REQUESTED_AT - 3600})
    receipts.append(receipt())
    assert refresh(db, account) == 1
    assert row.detail["moveCollectTaskDetailId"] == "task-current"


def test_same_second_retry_never_reuses_previous_attempt_receipt_id(receipt_db):
    db, account, receipts = receipt_db
    row = pending_log(db, account, attempts=2,
                      detail={"claimOnly": False, "moveCollectTaskDetailId": "task-old",
                              "receiptAttemptAt": REQUESTED_AT - 0.1})
    receipts.append(receipt(stamp=REQUESTED_AT - 0.1, receipt_id="task-old"))
    assert refresh(db, account) == 0
    assert row.status == "processing"


def test_legacy_request_without_last_requested_at_uses_original_time(receipt_db):
    db, account, receipts = receipt_db
    row = pending_log(db, account, last_requested_at=None)
    receipts.append(receipt())
    assert refresh(db, account) == 1
    assert row.status == "success"


def test_claim_only_ledger_does_not_steal_receipt_or_block_real_publish(receipt_db):
    db, account, receipts = receipt_db
    claim = pending_log(db, account, owner=2, detail={"claimOnly": True})
    row = pending_log(db, account)
    receipts.append(receipt())
    assert refresh(db, account) == 1
    assert (row.status, claim.status) == ("success", "processing")
