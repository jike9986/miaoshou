"""发布到店铺：一次任务可发多个店铺，后台跑，进度在批次页看。

为什么要有这个（负责人 2026-11-18）：
  多店铺账号一次发 5 个店铺时，前端串行 await 每个店铺 —— 后端每个店铺都要
  重建载体（实测单店 30~60 秒），5 个就是几分钟，前端 180 秒超时先断，
  用户看到的就是「正在提交…然后没反应」。

  改成任务化：点发布只做一件事 —— 落一条批次记录立刻返回，真正干活在后台
  线程里按店铺推进，进度写进 batch.settings，批次页轮询显示。

与 claim_job_service 的区别：那个是「认领到采集箱」（claimOnly=True）；
这个是「发布到店铺」（claimOnly=False），且支持一次任务多店铺。
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from core import errors
from core.locks import hold_for_session
from core.utils import new_batch_id, now_ts
from db.session import SessionLocal
from models.batch import Batch, BatchItem
from models.operator import Operator
from services import account_service, batch_service, operator_service, publish_service

logger = logging.getLogger(__name__)
KIND = "publish_job"
MAX_ITEMS = 500


def _item_key(shop_id: str, warehouse_id: str) -> str:
    """BatchItem.detail_id 的唯一键：店铺 × 商品。

    BatchItem 上有 (batch_id, detail_id) 唯一约束，而同一件商品要发到多个店铺，
    所以 detail_id 必须带上店铺，否则第二条会被数据库拒掉。
    """
    return f"{shop_id}:{warehouse_id}"


def create(db: Session, body: dict[str, Any], account_id: int,
           operator_id: int | None) -> Batch:
    if body.get("confirm") is not True:
        raise errors.validation("请确认发布", field="confirm")
    targets = body.get("targets") or []
    if not isinstance(targets, list) or not targets:
        raise errors.validation("请选择要发布的店铺", field="targets")
    items = body.get("items") or []
    ids = [str(item.get("warehouseId") or "").strip() for item in items]
    if not ids or any(not value.isdigit() for value in ids):
        raise errors.validation("请选择仓库商品", field="items")
    if len(ids) > MAX_ITEMS or len(set(ids)) != len(ids):
        raise errors.validation(f"商品重复或数量超过 {MAX_ITEMS} 件", field="items")

    account = account_service.require_account(db, str(account_id))
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in targets:
        if not isinstance(raw, dict):
            continue
        shop_id = str(raw.get("shopId") or "").strip()
        if not shop_id.isdigit():
            raise errors.validation("目标店铺无效", field="targets")
        account_service.require_shop(db, account, shop_id)
        if shop_id in seen:
            continue
        seen.add(shop_id)
        normalized.append({"shopId": shop_id,
                           "template": str(raw.get("template") or "").strip()})
    if not normalized:
        raise errors.validation("请选择要发布的店铺", field="targets")

    operator = operator_service.get(db, operator_id) if operator_id is not None else None
    del operator  # 只用于后续权限校验路径，这里保持与 claim_job_service 一致的签名
    keys: list[str] = []
    for target in normalized:
        for warehouse_id in ids:
            keys.append(_item_key(target["shopId"], warehouse_id))

    # 同一账号同一操作员不允许两个在途发布任务：载体池是共享资源，
    # 两个任务同时抢链接池会互相拖慢且难定位。
    hold_for_session(db, [f"publish-job-create:{account_id}:{operator_id}"],
                     blocking=False, busy_message="发布任务正在创建，请稍后查看批次")

    batch = Batch(
        id=new_batch_id("pub"), status="pending", account_id=account_id,
        shop_id=",".join(t["shopId"] for t in normalized)[:32],
        detail_ids=list(keys), current_index=0,
        settings={"kind": KIND,
                  "targets": normalized,
                  "warehouseIds": ids,
                  "request": {k: v for k, v in body.items()
                              if k not in {"items", "targets", "confirm"}}},
        results=[], errors=[], operator_id=operator_id)
    db.add(batch)
    db.flush()
    db.add_all([BatchItem(batch_id=batch.id, detail_id=key, status="pending")
                for key in keys])
    db.flush()
    return batch


def get(db: Session, batch_id: str, account_id: int,
        operator: Operator | None = None) -> Batch:
    batch = batch_service.get(db, batch_id)
    if (batch.account_id != account_id
            or (batch.settings or {}).get("kind") != KIND):
        raise errors.not_found("发布任务不存在", batchId=batch_id)
    if (operator is not None
            and operator_service.role_of(operator) == operator_service.ROLE_MEMBER
            and batch.operator_id != operator.id):
        raise errors.AppError(errors.FORBIDDEN, "无权查看该任务", status=403)
    return batch


def list_jobs(db: Session, account_id: int, limit: int = 50,
              operator: Operator | None = None) -> dict[str, Any]:
    query = db.query(Batch).filter(Batch.account_id == account_id)
    if (operator is not None
            and operator_service.role_of(operator) == operator_service.ROLE_MEMBER):
        query = query.filter(Batch.operator_id == operator.id)
    rows = query.order_by(Batch.created_at.desc()).all()
    jobs = [row for row in rows if (row.settings or {}).get("kind") == KIND]
    return {"items": [batch_service.serialize(row) for row in jobs[:limit]],
            "total": len(jobs)}


def delete(db: Session, batch_id: str, account_id: int,
           operator: Operator | None = None) -> dict[str, Any]:
    """删掉一条已完成/失败的发布任务记录（只是台账，不动妙手侧的商品）。"""
    batch = get(db, batch_id, account_id, operator)
    if batch.status in {"pending", "running"}:
        raise errors.busy("任务还在跑，等它结束再删", batchId=batch.id)
    db.query(BatchItem).filter(BatchItem.batch_id == batch.id).delete(
        synchronize_session=False)
    db.delete(batch)
    db.flush()
    return {"deleted": 1, "batchId": batch_id}


def run(batch_id: str) -> None:
    """后台执行：按店铺逐个发布，每完成一个店铺就落一次进度。

    单个店铺失败不影响其他店铺继续发 —— 5 个店里有 1 个失败，
    剩下 4 个照常发出去，比整批中断有用得多。
    """
    db = SessionLocal()
    try:
        batch = batch_service.get(db, batch_id)
        if (batch.settings or {}).get("kind") != KIND:
            return
        # 重复投递保护：只有 pending → running 抢到的那一次才往下走
        started = db.query(Batch).filter(
            Batch.id == batch_id, Batch.status == "pending").update(
                {Batch.status: "running"}, synchronize_session=False)
        db.commit()
        if started != 1:
            return
        db.refresh(batch)

        settings = dict(batch.settings or {})
        targets = list(settings.get("targets") or [])
        warehouse_ids = [str(value) for value in (settings.get("warehouseIds") or [])]
        request = dict(settings.get("request") or {})
        messages: list[str] = []

        batch.settings = {**settings, "progress": {
            "stage": "publishing", "shopIndex": 0, "shopTotal": len(targets),
            "succeeded": 0, "failed": 0}}
        batch.updated_at = now_ts()
        db.commit()

        for shop_index, target in enumerate(targets, start=1):
            shop_id = str(target.get("shopId") or "")
            template = str(target.get("template") or "").strip() or None
            keys = [_item_key(shop_id, wid) for wid in warehouse_ids]
            for key in keys:
                item = (db.query(BatchItem)
                        .filter(BatchItem.batch_id == batch_id,
                                BatchItem.detail_id == key).first())
                if item is not None:
                    item.status = "running"
                    item.attempt += 1
            db.commit()

            body = dict(request)
            body.update({
                "items": [{"warehouseId": wid} for wid in warehouse_ids],
                "shopId": shop_id,
                "accountId": str(batch.account_id),
                "batchId": batch_id,
                "template": template,
                "claimOnly": False,
                "confirm": True,
            })
            ok_count = 0
            fail_message = ""
            price_by_warehouse: dict[str, str] = {}
            try:
                result = publish_service.publish(
                    db, body, _log_operator_id=batch.operator_id)
                db.commit()
                ok_count = len(result.get("published") or [])
                # 负责人 2026-11-18：明细里要能直接看到发出去多少钱，
                # 不然每件都得进妙手后台一个个核对。
                for row in result.get("repriced") or []:
                    key = str(row.get("warehouseId") or "")
                    after = row.get("priceAfter")
                    if key and after:
                        currency = str(row.get("currency") or "").strip()
                        price_by_warehouse[key] = (
                            f"{after} {currency}".strip())
                failures = result.get("failures") or []
                if failures and ok_count == 0:
                    fail_message = str(failures[0].get("reason") or "发布失败")[:300]
            except Exception as exc:  # noqa: BLE001 - 单店失败不拖垮整批
                db.rollback()
                fail_message = str(exc)[:300]
                logger.warning("发布任务 %s 店铺 %s 失败", batch_id, shop_id,
                               exc_info=True)

            db.expire_all()
            batch = batch_service.get(db, batch_id)
            rows = list(batch.results or [])
            for key, warehouse_id in zip(keys, warehouse_ids, strict=False):
                item = (db.query(BatchItem)
                        .filter(BatchItem.batch_id == batch_id,
                                BatchItem.detail_id == key).first())
                if item is None:
                    continue
                item.status = "ok" if ok_count > 0 else "failed"
                if ok_count > 0:
                    price = price_by_warehouse.get(str(warehouse_id))
                    item.message = (f"已提交 · 售价 {price}" if price
                                    else "已提交，等待平台回执")
                else:
                    item.message = fail_message or "发布失败"
                rows.append({"detailId": item.detail_id, "status": item.status,
                             "message": item.message, "shopId": shop_id,
                             "at": now_ts()})
            if fail_message:
                messages.append(f"店铺 {shop_id}：{fail_message}")
            batch.results = rows
            batch.current_index = shop_index
            batch.errors = messages
            batch.settings = {**(batch.settings or {}), "progress": {
                "stage": "publishing", "shopIndex": shop_index,
                "shopTotal": len(targets),
                "succeeded": sum(1 for r in rows if r.get("status") == "ok"),
                "failed": sum(1 for r in rows if r.get("status") == "failed")}}
            batch.updated_at = now_ts()
            db.commit()

        batch = batch_service.get(db, batch_id)
        rows = list(batch.results or [])
        batch.status = "failed" if rows and all(
            r.get("status") == "failed" for r in rows) else "done"
        batch.finished_at = now_ts()
        batch.settings = {**(batch.settings or {}), "progress": {
            **(batch.settings or {}).get("progress", {}), "stage": batch.status}}
        batch.updated_at = now_ts()
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("发布任务 %s 异常终止", batch_id)
        try:
            batch = batch_service.get(db, batch_id)
            if batch.status == "running":
                batch.status = "failed"
                batch.errors = [*(batch.errors or []), "任务异常终止，请到发布记录核对"]
                batch.finished_at = now_ts()
                db.commit()
        except Exception:
            db.rollback()
    finally:
        db.close()
