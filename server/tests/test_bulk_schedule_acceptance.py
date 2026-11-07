"""Exercise real batch/schedule services against a deterministic Miaoshou boundary.

The database and upstream drafts are isolated. No real product or shop is used.
"""
from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api import publish as publish_api
from core import errors
from db.session import Base
from models.account import Account
from models.account_binding import AccountBinding
from models.operator import Operator
from models.product import Product
from models.product_claim import ProductClaim
from models.publish_log import PublishLog
from models.schedule import Schedule
from services import (
    batch_service,
    carrier_service,
    claim_job_service,
    publish_service,
    schedule_service,
    setting_service,
    warehouse_service,
)
from services.miaoshou import MiaoshouError
from services.price_template import DEFAULT_TEMPLATE


class FakeMiaoshou:
    def __init__(self):
        self.drafts: dict[int, dict] = {}
        self.copy_sizes: list[int] = []
        self.published: list[list[int]] = []
        self.receipts: list[dict] = []
        self.saved: set[int] = set()
        self.next_id = 100000

    def common_box_list(self, **_kwargs):
        return {"data": {"detailList": [{
            "commonCollectBoxDetailId": 1, "status": "success",
        }]}}

    def claim_common_to_platform(self, items):
        assert len(items) == 1
        common_id, platform, copies = items[0]
        assert (common_id, platform) == (1, "tiktok")
        assert 1 <= copies <= 100
        self.copy_sizes.append(copies)
        ids = list(range(self.next_id, self.next_id + copies))
        self.next_id += copies
        for ident in ids:
            self.drafts[ident] = {"title": "Original carrier", "weight": "99", "price": "1"}
        return {"data": {"detailIds": ids}}

    def fetch_items(self, _urls):
        pytest.fail("An existing common-box product must be copied, not collected again")

    def warehouse_list(self, shop_ids):
        return {"data": {"shopWarehouseList": [{
            "shopId": shop_ids[0],
            "warehouseList": [{"warehouseId": "11", "isDefault": 1}],
        }]}}

    def category_tree(self, _site):
        return {"data": {"cateTree": {"1": {"cid": 1, "isLastLevel": 1}}}}

    def category_metadata(self, _site, _cid, _shop_ids=None):
        """类目销售规格定义（颜色 100000 / 尺码 100007）。

        生产代码 carrier_service 会调这个接口拿真实 attrId，缺了它
        只会在日志里留一条 warning 然后退回默认值——测试就再也验不到
        「规格归一化到底有没有按类目元数据走」，所以必须补上。
        """
        return {"data": {"categorySaleAttrList": [
            {"attrId": "100000", "attrName": "Warna"},
            {"attrId": "100007", "attrName": "Saiz"},
        ]}}

    def shop_detail(self, detail_id, _shop_id):
        return {"data": {"ossMd5": "test-version", "shopCollectItemInfo":
                         deepcopy(self.drafts[int(detail_id)])}}

    def save_shop(self, detail_id, _shop_id, _oss_md5, body):
        self.drafts[int(detail_id)] = deepcopy(body)
        self.saved.add(int(detail_id))
        return {"success": True}

    def collect_box_list(self, *, page_no=1, page_size=100, **_kwargs):
        ids = sorted(self.saved)
        start = (page_no - 1) * page_size
        rows = [{
            "collectBoxDetailId": ident,
            "collectBoxDetailShopList": [{"shopId": "9"}],
        } for ident in ids[start:start + page_size]]
        return {"data": {"total": len(ids), "detailList": rows}}

    def publish_items(self, detail_ids, shop_ids):
        assert shop_ids == [9]
        self.published.append(list(detail_ids))
        return {"success": True}

    def publish_records(self, **_kwargs):
        return {"data": {"total": len(self.receipts), "detailList": self.receipts}}

    def close(self):
        pass


@pytest.fixture
def isolated_services(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'acceptance.db').as_posix()}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    fake = FakeMiaoshou()
    monkeypatch.setattr(claim_job_service, "SessionLocal", sessions)
    # Remove the production rate delay only; copy/save/readback remain real.
    monkeypatch.setattr(carrier_service.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(setting_service, "publish_enabled", lambda _db: True)
    monkeypatch.setattr(setting_service, "is_read_only", lambda _db: False)
    monkeypatch.setattr(setting_service, "price_templates", lambda _db: {
        "Acceptance pricing": {**DEFAULT_TEMPLATE, "name": "Acceptance pricing"},
    })
    try:
        with sessions() as db:
            first = Operator(name="first", token="first", role="member", enabled=True)
            second = Operator(name="second", token="second", role="member", enabled=True)
            db.add_all([first, second])
            db.flush()
            source = Account(name="Teacher warehouse", app_key="source")
            account = Account(
                name="Shared Miaoshou", app_key="shared", owner_operator_id=first.id,
                shops=json.dumps([{"shopId": "9", "name": "Test shop", "site": "MY"}]),
            )
            db.add_all([source, account])
            db.flush()
            db.add_all([AccountBinding(account_id=account.id, operator_id=member.id)
                        for member in (first, second)])
            db.commit()
            monkeypatch.setattr(publish_service, "account_client_for",
                                lambda *_args: (account, fake))
            yield db, sessions, fake, source, account, first, second
    finally:
        engine.dispose()


def finished_product(db, source, index):
    product = Product(
        account_id=source.id, warehouse_key=f"{index + 1}:7",
        detail_id=str(index + 1), shop_id="7", title=f"Finished product {index}",
        status="ready", saved_at=index, revision=1,
        quality_review={"verifiedRevision": 1},
        payload={
            "title": f"Finished product {index}", "cid": 1, "site": "MY",
            "weight": "0.5", "price": "10", "originPrice": "10",
            "packageLength": "17", "packageWidth": "12", "packageHeight": "8",
            "skuMap": {"sku-1": {"price": "10", "originPrice": "10", "stock": 10}},
        },
    )
    db.add(product)
    return product


@pytest.mark.parametrize("quantity", [200, 500, 1000])
def test_bulk_copy_overwrite_progress_and_shared_key_isolation(isolated_services, quantity):
    db, sessions, fake, source, account, first, second = isolated_services
    products = [finished_product(db, source, index) for index in range(quantity)]
    db.flush()
    db.add_all([ProductClaim(product_id=product.id, owner_operator_id=first.id)
                for product in products])
    db.commit()
    batch = claim_job_service.create(db, {
        "confirm": True, "claimOnly": True, "shopId": "9",
        "items": [{"warehouseId": str(product.id)} for product in products],
    }, account.id, first.id)
    batch_id = batch.id
    db.commit()

    claim_job_service.run(batch_id)

    with sessions() as reopened:
        done = claim_job_service.get(reopened, batch_id, account.id, operator=first)
        assert (done.status, done.current_index) == ("done", quantity)
        assert len(done.results) == quantity
        assert all(item.status == "ok" for item in batch_service.items_of(reopened, batch_id))
        assert claim_job_service.list_jobs(reopened, account.id, 20, operator=first)["total"] == 1
        assert claim_job_service.list_jobs(reopened, account.id, 20, operator=second)["total"] == 0
        with pytest.raises(errors.AppError) as denied:
            claim_job_service.get(reopened, batch_id, account.id, operator=second)
        assert denied.value.status == 404
        assert reopened.query(PublishLog).filter(PublishLog.operator_id == second.id).count() == 0
        first_claimed = warehouse_service.list_products(
            reopened, operator_id=first.id, claim_account_id=str(account.id), claimed=True)
        second_claimed = warehouse_service.list_products(
            reopened, operator_id=second.id, claim_account_id=str(account.id), claimed=True)
        assert first_claimed["total"] == quantity
        assert second_claimed["total"] == 0
    assert fake.copy_sizes == [1] * quantity
    assert len(fake.drafts) == quantity
    assert fake.published == []
    assert {draft["title"] for draft in fake.drafts.values()} == {
        f"Finished product {index}" for index in range(quantity)}
    assert all((draft["weight"], draft["price"]) == ("0.5", "10")
               for draft in fake.drafts.values())


def scheduled(db, account, owner):
    task = Schedule(
        id=f"schedule-{owner.id}", name=f"Schedule {owner.id}", account_id=account.id,
        owner_operator_id=owner.id, shop_id="9", template_name="Acceptance pricing",
        frequency="daily", time_of_day="09:00", per_run=20, enabled=True,
    )
    db.add(task)
    return task


def test_due_schedule_waits_for_receipt_and_isolates_users_of_same_key(isolated_services):
    db, _sessions, fake, source, account, first, second = isolated_services
    product = finished_product(db, source, 0)
    db.flush()
    db.add_all([ProductClaim(product_id=product.id, owner_operator_id=owner.id)
                for owner in (first, second)])
    tasks = [scheduled(db, account, owner) for owner in (first, second)]
    db.commit()

    assert schedule_service.run_due(db, now=datetime(2026, 11, 3, 8, 59)) == []
    summaries = schedule_service.run_due(db, now=datetime(2026, 11, 3, 9, 0))
    assert len(summaries) == 2
    assert [summary["status"] for summary in summaries] == ["submitted", "submitted"]
    assert all((summary["published"], summary["submitted"], summary["failed"]) == (0, 1, 0)
               for summary in summaries)
    assert fake.copy_sizes == [1, 1]
    assert all(float(draft["skuMap"]["sku-1"]["price"]) > 10
               for draft in fake.drafts.values())
    assert all((draft["title"], draft["weight"]) == (product.title, "0.5")
               for draft in fake.drafts.values())
    assert len(fake.published) == 2
    assert schedule_service.run_due(db, now=datetime(2026, 11, 3, 9, 1)) == []
    logs = db.query(PublishLog).order_by(PublishLog.id).all()
    assert {log.operator_id for log in logs} == {first.id, second.id}
    assert len({log.detail_id for log in logs}) == 2
    assert all(log.status == "processing" for log in logs)

    fake.receipts = [{"detailId": log.detail_id, "shopId": log.shop_id,
                      "status": "success" if log.operator_id == first.id else "fail",
                      "gmtCreate": datetime.fromtimestamp(
                          log.last_requested_at or log.requested_at, timezone(timedelta(hours=8))
                      ).strftime("%Y-%m-%d %H:%M:%S"),
                      "itemId": f"item-{log.id}", "reason": "" if log.operator_id == first.id
                      else "Test platform rejection"} for log in logs]
    # Refreshing one user's receipts must leave the other user's ledger pending.
    assert publish_api._refresh_processing(
        db, str(account.id), operator_id=first.id)["updated"] == 1
    first_result = schedule_service.list_schedules(db, operator=first)["items"][0]["lastResult"]
    second_result = schedule_service.list_schedules(db, operator=second)["items"][0]["lastResult"]
    assert (first_result["status"], first_result["published"], first_result["submitted"]) == (
        "ok", 1, 0)
    assert second_result["status"] == "submitted"
    assert publish_api._refresh_processing(
        db, str(account.id), operator_id=second.id)["updated"] == 1
    second_history = schedule_service.list_logs(db, operator=second)["items"]
    assert len(second_history) == 1
    assert (second_history[0]["status"], second_history[0]["published"],
            second_history[0]["failed"], second_history[0]["submitted"]) == ("failed", 0, 1, 0)
    assert {row["taskId"] for row in second_history} == {tasks[1].id}
    assert schedule_service.run_due(db, now=datetime(2026, 11, 3, 9, 2)) == []
    assert len(fake.published) == 2


# 「category」不在失败清单里：载体是妙手复制出来的草稿，类目跟着载体走，
# 与仓库快照口径天然不同（CLAUDE_STANDARD §3.6）。把 categoryCid 纳入严格核验
# 会让每一次定时发布都失败，所以它由下面单独一条用例锁住。
@pytest.mark.parametrize("failure", [
    "timeout", "empty", "invalid_shape", "comparison", "sale_price", "purchase_price",
])
def test_unverified_carrier_is_never_published(isolated_services, monkeypatch, failure):
    db, _sessions, fake, source, account, first, _second = isolated_services
    product = finished_product(db, source, 0)
    db.flush()
    db.add(ProductClaim(product_id=product.id, owner_operator_id=first.id))
    db.commit()
    original_read = fake.shop_detail

    def readback(detail_id, shop_id):
        if int(detail_id) not in fake.saved:
            return original_read(detail_id, shop_id)
        if failure == "timeout":
            raise MiaoshouError("readback timed out", kind="timeout")
        if failure == "empty":
            return {"data": {}}
        if failure == "invalid_shape":
            return {"data": []}
        result = original_read(detail_id, shop_id)
        actual = result["data"]["shopCollectItemInfo"]
        if failure == "sale_price":
            actual["skuMap"]["sku-1"]["price"] = "1"
        elif failure == "purchase_price":
            actual["skuMap"]["sku-1"]["originPrice"] = "1"
        return result

    monkeypatch.setattr(fake, "shop_detail", readback)
    if failure == "comparison":
        from services import readback_check

        def bad_comparison(*_args, **_kwargs):
            raise ValueError("unrecognized readback shape")

        monkeypatch.setattr(readback_check, "readback_issues", bad_comparison)
    with pytest.raises(errors.AppError) as blocked:
        publish_service.publish(db, {
            "confirm": True, "accountId": account.id, "shopId": "9",
            "template": "Acceptance pricing", "items": [{"warehouseId": str(product.id)}],
        }, operator_id=first.id)
    assert blocked.value.code == errors.CARRIER_UNAVAILABLE
    assert fake.saved
    assert fake.published == []
    assert db.query(PublishLog).count() == 0


def test_schedule_unknown_response_is_pending_and_does_not_resubmit(isolated_services, monkeypatch):
    db, sessions, fake, source, account, first, _second = isolated_services
    product = finished_product(db, source, 0)
    db.flush()
    db.add(ProductClaim(product_id=product.id, owner_operator_id=first.id))
    task = scheduled(db, account, first)
    db.commit()
    publish = fake.publish_items

    def lost_response(detail_ids, shop_ids):
        publish(detail_ids, shop_ids)
        raise MiaoshouError("response lost after submission", kind="timeout")

    monkeypatch.setattr(fake, "publish_items", lost_response)
    summary = schedule_service.run_due(db, now=datetime(2026, 11, 3, 9, 0))[0]
    assert (summary["status"], summary["published"], summary["submitted"], summary["failed"]) == (
        "submitted", 0, 1, 0)
    assert schedule_service.run_due(db, now=datetime(2026, 11, 3, 9, 1)) == []
    with sessions() as reopened:
        saved = reopened.get(Schedule, task.id)
        assert saved.last_result["status"] == "submitted"
        ledger = reopened.query(PublishLog).one()
        assert saved.last_result["publishStatuses"] == {str(ledger.id): "processing"}
    log = db.query(PublishLog).one()
    fake.receipts = [{"detailId": log.detail_id, "shopId": log.shop_id, "status": "success",
                      "gmtCreate": datetime.fromtimestamp(
                          log.last_requested_at or log.requested_at, timezone(timedelta(hours=8))
                      ).strftime("%Y-%m-%d %H:%M:%S")}]
    assert publish_api._refresh_processing(
        db, str(account.id), operator_id=first.id)["updated"] == 1
    assert task.last_result["status"] == "ok"
    assert len(fake.published) == 1


def test_schedule_receipts_keep_exact_execution_and_owner(isolated_services):
    from models.schedule_log import ScheduleLog

    db, _sessions, _fake, _source, account, first, second = isolated_services
    task = scheduled(db, account, first)
    db.flush()
    log = PublishLog(account_id=account.id, operator_id=first.id, schedule_id=task.id,
                     detail_id="11", shop_id="9", batch_id="older", status="processing")
    db.add(log)
    db.flush()
    for batch_id, at in (("older", datetime(2026, 11, 2)), ("newer", datetime(2026, 11, 3))):
        result = schedule_service._publish_summary({"records": {"items": [{
            "logId": log.id, "detailId": log.detail_id, "status": "processing",
        }]}}, task=task, requested=1, selected=1, batch_id=batch_id, with_results=True)
        schedule_service.record_log(db, task, result, moment=at)
        task.last_result = result
    db.flush()
    log.batch_id, log.status = "newer", "success"
    log.operator_id = second.id
    assert not schedule_service.reconcile_publish_log(db, log)
    log.operator_id = first.id
    assert schedule_service.reconcile_publish_log(db, log)
    rows = db.query(ScheduleLog).order_by(ScheduleLog.at).all()
    assert [row.status for row in rows] == ["submitted", "ok"]
    assert task.last_result["status"] == "ok"


def test_schedule_partial_submission_counts_are_not_successes():
    task = Schedule(id="mixed", claim_only=False)
    summary = schedule_service._publish_summary({
        "records": {"items": [{"logId": 1, "status": "success"},
                               {"logId": 2, "status": "processing"},
                               {"logId": 3, "status": "fail"}]},
        "failures": [{"reason": "save rejected"}],
    }, task=task, requested=4, selected=4, batch_id="mixed-run", with_results=True)
    assert (summary["status"], summary["published"], summary["submitted"], summary["failed"]) == (
        "partial", 1, 1, 2)
    assert [item["status"] for item in summary["results"]] == [
        "success", "processing", "fail", "failed"]

def test_carrier_category_divergence_is_not_a_publish_blocker(isolated_services, monkeypatch):
    """只差 categoryCid 不算核验失败，发布必须照常发出去。

    2026-11-07 的回归：把 categoryCid 从忽略清单里去掉后，定时任务每一轮都
    「载体覆盖后读回不一致 detailId=… 差异=['categoryCid']」，任务 failed、
    产品一件都发不出去。载体草稿的类目跟着载体走，与仓库快照口径天然不同，
    所以严格读回核验仍要忽略它 —— 价格、图片、规格这些必须继续严格核对
    （见 test_unverified_carrier_is_never_published）。
    """
    db, _sessions, fake, source, account, first, _second = isolated_services
    product = finished_product(db, source, 0)
    db.flush()
    db.add(ProductClaim(product_id=product.id, owner_operator_id=first.id))
    db.commit()
    original_read = fake.shop_detail

    def readback(detail_id, shop_id):
        if int(detail_id) not in fake.saved:
            return original_read(detail_id, shop_id)
        result = original_read(detail_id, shop_id)
        result["data"]["shopCollectItemInfo"]["cid"] = "unexpected"
        return result

    monkeypatch.setattr(fake, "shop_detail", readback)
    outcome = publish_service.publish(db, {
        "confirm": True, "accountId": account.id, "shopId": "9",
        "template": "Acceptance pricing", "items": [{"warehouseId": str(product.id)}],
    }, operator_id=first.id)
    assert len(fake.saved) == 1
    assert len(outcome["published"]) == 1
    assert len(fake.published) == 1
    assert db.query(PublishLog).count() == 1


def test_waiting_for_receipt_does_not_consume_daily_attempts(isolated_services):
    """等平台回执不是失败：不能吃掉当天的尝试次数。

    发布提交后平台回执要 1–3 分钟才建单，而调度器每 60s 扫一轮。按失败记账
    的话三次就吃满 SCHEDULE_MAX_ATTEMPTS_PER_DAY=3，任务当天直接不再执行，
    表现就是「定时任务设置好了不执行」。
    """
    from core.utils import now_ts
    from models.schedule_log import ScheduleLog

    db, sessions, fake, source, account, first, _second = isolated_services
    task = scheduled(db, account, first)
    db.flush()
    pending = PublishLog(account_id=account.id, operator_id=first.id,
                         schedule_id=task.id, detail_id="3516485793", shop_id="9",
                         batch_id="sched-previous", status="processing",
                         requested_at=now_ts(), last_requested_at=now_ts())
    db.add(pending)
    db.commit()

    ticks = (schedule_service.run_due(db, now=datetime(2026, 11, 3, 9, 0))
             + schedule_service.run_due(db, now=datetime(2026, 11, 3, 9, 1))
             + schedule_service.run_due(db, now=datetime(2026, 11, 3, 9, 2)))
    assert len(ticks) == 3
    for summary in ticks:
        assert summary["status"] == "submitted"
        assert (summary["selected"], summary["published"], summary["failed"]) == (0, 0, 0)
        assert summary["pendingDetailId"] == "3516485793"
    assert fake.published == []
    with sessions() as reopened:
        saved = reopened.get(Schedule, task.id)
        assert saved.attempts == 0
        assert (saved.last_run_date or "") == ""
        assert saved.last_result["status"] == "submitted"
    assert db.query(ScheduleLog).count() == 0


def test_receipt_clears_waiting_and_publishes_on_next_tick(isolated_services):
    """回执到了之后下一轮照常发布，不再被待确认挡住。"""
    from core.utils import now_ts

    db, _sessions, fake, source, account, first, _second = isolated_services
    product = finished_product(db, source, 0)
    task = scheduled(db, account, first)
    task.per_run = 1
    db.flush()
    db.add(ProductClaim(product_id=product.id, owner_operator_id=first.id))
    pending = PublishLog(account_id=account.id, operator_id=first.id,
                         schedule_id=task.id, detail_id="3516485793", shop_id="9",
                         batch_id="sched-previous", status="processing",
                         requested_at=now_ts(), last_requested_at=now_ts())
    db.add(pending)
    db.commit()

    assert schedule_service.run_due(
        db, now=datetime(2026, 11, 3, 9, 0))[0]["status"] == "submitted"
    fake.receipts = [{"detailId": "3516485793", "shopId": "9", "status": "success",
                      "gmtCreate": datetime.fromtimestamp(
                          pending.last_requested_at, timezone(timedelta(hours=8))
                      ).strftime("%Y-%m-%d %H:%M:%S")}]
    summary = schedule_service.run_due(db, now=datetime(2026, 11, 3, 9, 1))[0]
    assert summary["status"] == "submitted"
    assert pending.status == "success"
    assert len(fake.published) == 1

