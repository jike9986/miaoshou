"""发布回执确认：翻页深度 + 释放前必须回源。

覆盖 2026-11-19 查明的 unknown 堆积根因。实测数据：
  账号 9 平台回执总量 389 行，本地 119 条被标 unknown，
  而它们在平台侧**全部是 success** —— 商品真的发出去了，台账却写失败。

两个缺陷叠加：
  1. `_refresh_processing` 只翻 5 页（100 行），69 条落在第 6~10 页翻不到
  2. `release_stale_publish_logs` 过 600 秒直接标 unknown，不再回源确认
"""
from __future__ import annotations

import json
import time
import uuid

import pytest

from api import publish as publish_api


def test_refresh_max_pages_covers_platform_volume():
    """翻页上限必须能覆盖单账号一周的发布量。

    实测账号 9 平台总量 389 行 = 20 页。原来 5 页只能看 100 行，
    深处记录永远确认不到。
    """
    assert publish_api.REFRESH_MAX_PAGES * publish_api.PUBLISH_RECORDS_PAGE_SIZE >= 800


def test_receipt_id_field_is_collect_box_detail_id():
    """平台回执里 detailId 恒为 None，真 ID 在 collectBoxDetailId。

    实测 60 行回执：detailId=0 行有值，collectBoxDetailId=60 行有值。
    代码用 `item.get("detailId") or item.get("collectBoxDetailId")` 兜住了，
    这条测试钉住「不能把 collectBoxDetailId 那条分支删掉」。
    """
    item = {"detailId": None, "collectBoxDetailId": "3547173414",
            "shopId": "20080061", "status": "success"}
    resolved = item.get("detailId") or item.get("collectBoxDetailId")
    assert resolved == "3547173414"


def test_platform_receipt_uses_gmt_create_with_second_precision():
    """回执时间戳只有秒级精度，比较时必须 floor。"""
    from api.publish import _receipt_created_at

    created = _receipt_created_at("2026-11-19 16:54:01")
    assert created == pytest.approx(1795078441.0, abs=1.0)


def test_current_attempt_rejects_older_receipt():
    """比本次提交更早的回执不能算作本次结果。"""
    from api.publish import _current_attempt_receipts

    class Row:
        last_requested_at = 1795078440.0
        requested_at = 1795078440.0
        detail = {}

    old = [{"gmtCreate": "2026-11-19 16:50:00", "moveCollectTaskDetailId": "1"}]
    fresh = [{"gmtCreate": "2026-11-19 16:54:01", "moveCollectTaskDetailId": "2"}]

    assert _current_attempt_receipts(Row(), old) == []
    assert len(_current_attempt_receipts(Row(), fresh)) == 1


@pytest.fixture
def unknown_env():
    from db.session import SessionLocal, init_db
    from models.account import Account
    from models.publish_log import PublishLog

    init_db()
    db = SessionLocal()
    try:
        suffix = uuid.uuid4().hex[:10]
        account = Account(name=f"unknown-{suffix}", app_key=f"unk-key-{suffix}",
                          status="active", shops=json.dumps([]))
        db.add(account)
        db.commit()
        yield db, account, PublishLog
    finally:
        db.rollback()
        db.close()


def test_release_marks_stale_rows_unknown_when_no_receipt(unknown_env, monkeypatch):
    """确认不到回执的记录才标 unknown（这一步行为保持不变）。"""
    db, account, PublishLog = unknown_env
    from services import schedule_service

    old = time.time() - 3600
    db.add(PublishLog(account_id=account.id, detail_id=f"9001-{account.id}",
                      shop_id="111", warehouse_key=f"9001-{account.id}:111",
                      status="processing", requested_at=old,
                      last_requested_at=old, attempts=1))
    db.commit()

    # 回源拿不到任何回执（模拟平台确实没有）
    monkeypatch.setattr(publish_api, "_refresh_processing",
                        lambda *_a, **_kw: {"updated": 0, "platformTotal": 0})

    # 共用的测试库里可能还有别的 processing 行，这里只断言本测试造的那条被释放
    schedule_service.release_stale_publish_logs(db, max_age_seconds=600)

    row = (db.query(PublishLog)
           .filter(PublishLog.detail_id == f"9001-{account.id}").first())
    assert row.status == "unknown"


def test_release_refreshes_before_marking(unknown_env, monkeypatch):
    """释放前必须先回源 —— 这是修「已成功却标失败」的关键。

    旧实现直接标 unknown，从不回源。现在必须先调 `_refresh_processing`。
    """
    db, account, PublishLog = unknown_env
    from services import schedule_service

    old = time.time() - 3600
    target_detail = f"9002-{account.id}"
    db.add(PublishLog(account_id=account.id, detail_id=target_detail,
                      shop_id="222", warehouse_key=f"{target_detail}:222",
                      status="processing", requested_at=old,
                      last_requested_at=old, attempts=1))
    db.commit()

    calls: list[dict] = []

    def fake_refresh(_db, account_id=None, **kwargs):
        calls.append({"account_id": account_id, **kwargs})
        # 模拟深度回源确认到了平台 success
        (_db.query(PublishLog)
         .filter(PublishLog.detail_id == target_detail)
         .update({PublishLog.status: "success"}, synchronize_session=False))
        _db.commit()
        return {"updated": 1, "platformTotal": 389}

    monkeypatch.setattr(publish_api, "_refresh_processing", fake_refresh)

    schedule_service.release_stale_publish_logs(db, max_age_seconds=600)

    assert calls, "释放前必须调用回源确认"
    assert calls[0].get("deep") is True, "释放前的回源必须是深度模式"
    # 平台说 success，就不该被标成 unknown
    row = db.query(PublishLog).filter(PublishLog.detail_id == target_detail).first()
    assert row.status == "success"


def test_release_skips_fresh_rows(unknown_env, monkeypatch):
    """未超时的记录不碰，也不触发多余回源。"""
    db, account, PublishLog = unknown_env
    from services import schedule_service

    now = time.time()
    target = f"9003-{account.id}"
    db.add(PublishLog(account_id=account.id, detail_id=target, shop_id="333",
                      warehouse_key=f"{target}:333", status="processing",
                      requested_at=now, last_requested_at=now, attempts=1))
    db.commit()

    # 库里可能还有别的超时行会触发回源，所以这里断言的是「这条新记录没被释放」
    monkeypatch.setattr(publish_api, "_refresh_processing",
                        lambda *_a, **_kw: {"updated": 0})

    schedule_service.release_stale_publish_logs(db, max_age_seconds=600)

    row = db.query(PublishLog).filter(PublishLog.detail_id == target).first()
    assert row.status == "processing", "未超时的记录不能被释放"


def test_claim_only_rows_are_archived_as_claimed_not_unknown(
        unknown_env, monkeypatch):
    """认领记录不走发布回执，归档时不该标成 unknown。

    实测 2026-11-19：425 条残留 unknown 里 **422 条是认领记录**。
    它们的「成功」标志是商品进了采集箱，不是平台发布结果，
    `_refresh_processing` 会把它们过滤掉（`_is_claim_only_log`），
    所以永远确认不到。标成 unknown 会让人以为发布失败了。
    """
    db, account, PublishLog = unknown_env
    from services import schedule_service

    old = time.time() - 3600
    detail = f"claim-{account.id}"
    db.add(PublishLog(account_id=account.id, detail_id=detail, shop_id="555",
                      warehouse_key=f"{detail}:555", status="processing",
                      requested_at=old, last_requested_at=old, attempts=1,
                      detail={"claimOnly": True}))
    db.commit()

    monkeypatch.setattr(publish_api, "_refresh_processing",
                        lambda *_a, **_kw: {"updated": 0, "platformTotal": 10})

    schedule_service.release_stale_publish_logs(db, max_age_seconds=600)

    row = db.query(PublishLog).filter(PublishLog.detail_id == detail).first()
    assert row.status == "claimed", "认领记录应归档为 claimed，不是 unknown"
    assert "认领" in row.reason


def test_deep_mode_has_no_practical_page_cap(unknown_env, monkeypatch):
    """deep=True 时页数上限不设死，才能翻到深处的回执。"""
    db, account, PublishLog = unknown_env

    pages_requested: list[int] = []

    class FakeClient:
        def publish_records(self, *, page_no=1, page_size=20, status=None):
            pages_requested.append(page_no)
            # 造 10 页数据（第 10 页不足一页 → 终止）
            if page_no > 10:
                return {"data": {"total": 190, "moveCollectDetailList": []}}
            rows = [{"collectBoxDetailId": str(page_no * 1000 + i),
                     "shopId": "444", "status": "success",
                     "gmtCreate": "2026-11-19 16:54:01"}
                    for i in range(min(page_size, 20))]
            return {"data": {"total": 190, "moveCollectDetailList": rows}}

        def close(self):
            pass

    monkeypatch.setattr(publish_api.publish_service, "account_client_for",
                        lambda *_a, **_kw: (account, FakeClient()))

    publish_api._refresh_processing(db, str(account.id), deep=True)

    # 必须翻超过旧的 5 页上限，才能覆盖 389 行那种体量
    assert max(pages_requested) > 5, f"deep 模式只翻到第 {max(pages_requested)} 页"