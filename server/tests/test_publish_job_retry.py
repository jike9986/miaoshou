"""发布任务单店重试：只重跑失败的店铺 / 失败的商品。

负责人 2026-11-19：5 个店发 10 件，1 个店失败只能整批重发，会把已经发成功的
4 个店再发一遍 —— 发布不可撤销，那等于凭空多建 40 件商品。

这里钉住四件事：
  1. `retry()` 只把 failed / pending 的件写进 runPlan，ok 的一件都不碰；
  2. 重跑时发给妙手的 items 只包含失败店铺的失败件（按店铺分组）；
  3. 失败件重试成功后，整批状态变 done，results 里同一件不留两条（不会翻倍计数）；
  4. 正在跑的批次不许重试，全部成功的批次也不许重试。
"""
from __future__ import annotations

import json
import uuid

import pytest

from core import errors
from db.session import SessionLocal, init_db
from models.account import Account
from services import batch_service, publish_job_service


@pytest.fixture
def account():
    init_db()
    db = SessionLocal()
    try:
        suffix = uuid.uuid4().hex[:10]
        row = Account(
            name=f"publish-job-{suffix}", app_key=f"publish-job-key-{suffix}",
            status="active",
            shops=json.dumps([
                {"shopId": "111", "name": "Shop A", "site": "MY"},
                {"shopId": "222", "name": "Shop B", "site": "PH"},
                {"shopId": "333", "name": "Shop C", "site": "TH"},
            ]),
        )
        db.add(row)
        db.commit()
        yield db, row
    finally:
        db.rollback()
        db.close()


def _create(db, account, shop_ids: list[str], warehouse_ids: list[str]):
    body = {
        "confirm": True,
        "targets": [{"shopId": shop_id, "template": f"模板{shop_id}"}
                    for shop_id in shop_ids],
        "items": [{"warehouseId": warehouse_id} for warehouse_id in warehouse_ids],
    }
    batch = publish_job_service.create(db, body, account.id, None)
    db.commit()
    return batch


def _fake_publish(failing: dict[str, set[str]]):
    """造一个假 publish：某个店铺的某几件失败，其余成功。

    `failing = {"222": {"2"}}` 表示店铺 222 的仓库商品 2 会失败。
    """
    calls: list[dict] = []

    def publish(_db, payload, **_kwargs):
        shop_id = str(payload["shopId"])
        warehouse_ids = [str(item["warehouseId"]) for item in payload["items"]]
        calls.append({"shopId": shop_id, "warehouseIds": warehouse_ids})
        bad = failing.get(shop_id, set())
        published = [f"9{shop_id[-2:]}{wid}" for wid in warehouse_ids
                     if wid not in bad]
        failures = [{"warehouseId": wid, "reason": f"店铺 {shop_id} 报错"}
                    for wid in warehouse_ids if wid in bad]
        records = [{
            "logId": f"{shop_id}-{wid}",
            "warehouseId": wid,
            "status": "fail" if wid in bad else "processing",
            "reason": f"店铺 {shop_id} 报错" if wid in bad else "",
        } for wid in warehouse_ids]
        return {"published": published, "failures": failures,
                "records": {"total": len(records), "items": records},
                "repriced": [{"warehouseId": wid, "priceAfter": "47.56",
                              "currency": "MYR"}
                             for wid in warehouse_ids if wid not in bad]}

    publish.calls = calls  # type: ignore[attr-defined]
    return publish


def test_retry_only_reruns_failed_shop_and_failed_items(account, monkeypatch):
    db, row = account
    batch = _create(db, row, ["111", "222", "333"], ["1", "2"])
    failing = {"222": {"2"}}
    fake = _fake_publish(failing)
    monkeypatch.setattr(publish_job_service.publish_service, "publish", fake)

    publish_job_service.run(batch.id)
    db.expire_all()
    batch = publish_job_service.get(db, batch.id, row.id)
    assert batch.status == "done"
    assert [item.status for item in batch_service.items_of(db, batch.id)] == [
        "ok", "ok", "ok", "failed", "ok", "ok"]
    assert len(fake.calls) == 3
    fake.calls.clear()

    result = publish_job_service.retry(db, batch.id, row.id)
    db.commit()
    assert result["retried"] == 1
    assert result["shops"] == ["222"]
    # runPlan 精确到「店铺 222 的商品 2」，其余件一件都不在里面
    plan = batch_service.get(db, batch.id).settings["runPlan"]
    assert plan == [{"shopId": "222", "template": "模板222",
                     "warehouseIds": ["2"]}]

    # 这一轮不再失败（模拟把原因修好了）
    failing.clear()
    publish_job_service.run(batch.id)
    db.expire_all()
    # 第二轮只发了店铺 222 的那一件
    assert fake.calls == [{"shopId": "222", "warehouseIds": ["2"]}]
    batch = publish_job_service.get(db, batch.id, row.id)
    assert batch.status == "done"
    assert all(item.status == "ok" for item in batch_service.items_of(db, batch.id))
    # 重试成功后 runPlan 要清掉，否则下次删除/展示会带着一次性字段
    assert "runPlan" not in (batch.settings or {})


def test_results_do_not_duplicate_after_retry(account, monkeypatch):
    """同一件重试成功后，results 里只能留最新那条，否则成功/失败计数会翻倍。"""
    db, row = account
    batch = _create(db, row, ["111"], ["1"])
    fake = _fake_publish({"111": {"1"}})
    monkeypatch.setattr(publish_job_service.publish_service, "publish", fake)

    publish_job_service.run(batch.id)
    db.expire_all()
    batch = publish_job_service.get(db, batch.id, row.id)
    assert batch.status == "failed"
    assert len(batch.results) == 1
    assert batch.results[0]["status"] == "failed"

    publish_job_service.retry(db, batch.id, row.id)
    db.commit()
    fake.calls.clear()
    # 这一轮不再失败
    monkeypatch.setattr(publish_job_service.publish_service, "publish",
                        _fake_publish({}))
    publish_job_service.run(batch.id)
    db.expire_all()
    batch = publish_job_service.get(db, batch.id, row.id)
    assert len(batch.results) == 1
    assert batch.results[0]["status"] == "ok"
    assert batch.status == "done"


def test_retry_rejects_running_job(account):
    db, row = account
    batch = _create(db, row, ["111"], ["1"])
    batch.status = "running"
    db.commit()
    with pytest.raises(errors.AppError) as exc:
        publish_job_service.retry(db, batch.id, row.id)
    assert exc.value.code == errors.CONFLICT_BUSY


def test_retry_rejects_job_without_failures(account, monkeypatch):
    db, row = account
    batch = _create(db, row, ["111"], ["1"])
    monkeypatch.setattr(publish_job_service.publish_service, "publish",
                        _fake_publish({}))
    publish_job_service.run(batch.id)
    db.expire_all()
    with pytest.raises(errors.AppError) as exc:
        publish_job_service.retry(db, batch.id, row.id)
    assert exc.value.code == errors.VALIDATION_ERROR


def test_retry_is_scoped_to_account(account):
    db, row = account
    batch = _create(db, row, ["111"], ["1"])
    other = Account(name=f"other-{uuid.uuid4().hex[:8]}",
                    app_key=f"other-key-{uuid.uuid4().hex[:8]}", status="active")
    db.add(other)
    db.commit()
    with pytest.raises(errors.AppError) as exc:
        publish_job_service.retry(db, batch.id, other.id)
    assert exc.value.code == errors.NOT_FOUND


def test_run_plan_recovers_old_batch_without_targets(account, monkeypatch):
    """老批次缺 targets / warehouseIds 时，从 BatchItem 的键还原出清单。

    少一个店铺等于用户点了重试却少发一个店，比报错更糟，所以必须兜底。
    """
    db, row = account
    batch = _create(db, row, ["111"], ["1"])
    settings = dict(batch.settings)
    settings.pop("targets")
    settings.pop("warehouseIds")
    batch.settings = settings
    db.commit()

    plan = publish_job_service._run_plan(batch.settings)
    assert plan == [{"shopId": "111", "template": "", "warehouseIds": ["1"]}]


def test_retry_keeps_template_per_shop(account, monkeypatch):
    """每个店铺重试时仍然套自己站点的模板 —— MYR 模板套到 PH 店必错。"""
    db, row = account
    batch = _create(db, row, ["111", "222"], ["1"])
    monkeypatch.setattr(publish_job_service.publish_service, "publish",
                        _fake_publish({"111": {"1"}, "222": {"1"}}))
    publish_job_service.run(batch.id)
    db.expire_all()
    publish_job_service.retry(db, batch.id, row.id)
    db.commit()
    plan = batch_service.get(db, batch.id).settings["runPlan"]
    assert {item["shopId"]: item["template"] for item in plan} == {
        "111": "模板111", "222": "模板222"}


def test_retry_route_is_wired():
    """路由存在、方法对、状态码 202（重试是「已受理」，不是同步跑完）。"""
    from main import app

    matched = [route for route in app.routes
               if getattr(route, "path", "") == "/api/v1/publish-jobs/{batch_id}/retry"]
    assert matched, "缺少 POST /publish-jobs/{id}/retry 路由"
    route = matched[0]
    assert "POST" in (getattr(route, "methods", set()) or set())
    assert route.status_code == 202
