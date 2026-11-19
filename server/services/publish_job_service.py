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


def _split_key(key: str) -> tuple[str, str]:
    """`<shopId>:<warehouseId>` → 两段。拆不出来就返回空串对，调用方跳过。

    店铺 id 与仓库 id 都是纯数字，冒号只可能出现一次；按第一个冒号切即可。
    """
    text = str(key or "")
    shop_id, _, warehouse_id = text.partition(":")
    if not shop_id.isdigit() or not warehouse_id.isdigit():
        return "", ""
    return shop_id, warehouse_id


def _targets_of(settings: dict[str, Any],
                fallback_keys: list[str] | None = None) -> list[dict[str, str]]:
    """这次任务原本要发哪些店铺（带各自站点的模板）。

    `settings.targets` 是正路。老批次（本函数出现之前落的）万一缺这一项，
    就从 BatchItem 的 `<shopId>:<warehouseId>` 键里把店铺还原出来 ——
    少一个店铺就等于用户点了重试却少发一个店，比报错更糟。
    """
    targets: list[dict[str, str]] = []
    for raw in settings.get("targets") or []:
        if not isinstance(raw, dict):
            continue
        shop_id = str(raw.get("shopId") or "").strip()
        if not shop_id:
            continue
        targets.append({"shopId": shop_id,
                        "template": str(raw.get("template") or "").strip()})
    if targets:
        return targets
    seen: set[str] = set()
    for key in fallback_keys or []:
        shop_id, _ = _split_key(key)
        if shop_id and shop_id not in seen:
            seen.add(shop_id)
            targets.append({"shopId": shop_id, "template": ""})
    return targets


def _merge_result(rows: list[dict[str, Any]], row: dict[str, Any]) -> None:
    """把一件的结果写回 results：同一件只留最新一条。

    重试会把「上次失败、这次成功」的件再写一遍。直接 append 的话 results 里
    同一件会出现两条（一条 failed、一条 ok），进度里的成功/失败计数就会翻倍，
    前端也会看到一条假的失败记录。所以按 detailId 原地覆盖。
    """
    wanted = str(row.get("detailId") or "")
    for index, existing in enumerate(rows):
        if str(existing.get("detailId") or "") == wanted:
            rows[index] = row
            return
    rows.append(row)


def _run_plan(settings: dict[str, Any]) -> list[dict[str, Any]]:
    """这次要跑的「店铺 × 商品」清单。

    默认是 settings.targets × settings.warehouseIds 的完整叉乘（首次发布）。
    重试时 retry() 会写 settings.runPlan，只包含失败店铺里失败的那几件 ——
    已经成功的件绝不重发（发布不可撤销，重发就是多建一件商品）。
    """
    plan = settings.get("runPlan")
    if isinstance(plan, list) and plan:
        groups: list[dict[str, Any]] = []
        for raw in plan:
            if not isinstance(raw, dict):
                continue
            shop_id = str(raw.get("shopId") or "").strip()
            warehouse_ids = [str(value) for value in (raw.get("warehouseIds") or [])]
            if shop_id and warehouse_ids:
                groups.append({"shopId": shop_id,
                               "template": str(raw.get("template") or "").strip(),
                               "warehouseIds": warehouse_ids})
        return groups
    warehouse_ids = [str(value) for value in (settings.get("warehouseIds") or [])]
    if not warehouse_ids:
        # 老批次没有 warehouseIds 时从 BatchItem 键里还原，否则重试会发 0 件
        recovered: list[str] = []
        for key in (settings.get("detailKeys") or []):
            _, warehouse_id = _split_key(str(key))
            if warehouse_id and warehouse_id not in recovered:
                recovered.append(warehouse_id)
        warehouse_ids = recovered
    return [{"shopId": str(target.get("shopId") or ""),
             "template": str(target.get("template") or "").strip(),
             "warehouseIds": list(warehouse_ids)}
            for target in _targets_of(settings, settings.get("detailKeys"))
            if str(target.get("shopId") or "").strip()]


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
    # 受限分组闸门：学员不能发布「自营海外仓 / 海外仓定制款 / 精品」三组的货。
    #
    # 负责人 2026-11-19：「这三个分组里面的鞋不能随便给用户上都是精品…
    # 学员账号都看不到这三个分组也上不了这三个分组的鞋子」。
    # 这三组走合作厂家、拿货价标高，学员发出去等于亏本铺货。
    #
    # 必须在 service 层拦：前端把分组藏起来只是体验，
    # 直接构造 warehouseId 调 API 才是真实越权路径。
    from services import group_access

    if not group_access.may_access_restricted(operator):
        from models.product import Product as _Product

        rows = (db.query(_Product.group_id)
                .filter(_Product.id.in_([int(v) for v in ids]),
                        _Product.group_id.isnot(None))
                .distinct().all())
        group_access.guard_products(db, operator, [row[0] for row in rows],
                                    action="发布")
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
                  # 店铺 × 商品的完整键表。重试要靠它还原「哪个店发哪几件」，
                  # 老批次没有 warehouseIds 时也靠它兜底。
                  "detailKeys": keys,
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


def retry(db: Session, batch_id: str, account_id: int,
          operator: Operator | None = None) -> dict[str, Any]:
    """只重跑失败的店铺 / 失败的商品，已成功的一件都不碰。

    为什么必须只跑失败的（负责人 2026-11-19）：
      5 个店发 10 件，1 个店失败时「整批重发」会把已经发成功的 4 个店
      再发一遍 —— 发布不可撤销，那等于凭空多建 40 件商品。
      所以重试的清单必须精确到「失败店铺 × 该店铺失败的那几件」。

    清单来源是 BatchItem：那才是逐件真状态（results 里是给人看的快照）。
    重试范围：
      · `failed` 的件 —— 重跑；
      · `pending` 的件 —— 上一轮整批异常终止时没轮到的，一并重跑；
      · `ok` / `running` 的件 —— 不碰。running 说明还有线程在跑，
        抢过来重发就是重复发布。

    返回 `retry` 的摘要（几个店几件）；调用方负责投递 `run()`。
    """
    batch = get(db, batch_id, account_id, operator)
    if batch.status in {"pending", "running"}:
        raise errors.busy("任务还在跑，等它结束再重试", batchId=batch.id)
    # 连点两次「重试」不能投两次：两个线程同时把同一批 failed 改成 pending，
    # 就会各投一次 run()，第二次虽然会被 pending→running 挡住，但清单已经被
    # 覆盖成「全部件」，等于把成功的店又发一遍。锁活到路由 commit。
    hold_for_session(db, [f"publish-job-retry:{account_id}:{batch.id}"],
                     blocking=False, busy_message="这个任务正在重试，请稍后刷新查看")
    settings = dict(batch.settings or {})
    items = batch_service.items_of(db, batch.id)
    if not items:
        raise errors.validation("这个任务没有明细，无法重试", field="batchId",
                                value=batch.id)
    retry_keys = {item.detail_id for item in items
                  if item.status in {"failed", "pending"}}
    if not retry_keys:
        raise errors.validation("没有需要重试的商品：所有商品都已成功",
                                field="batchId", value=batch.id)

    targets = _targets_of(settings, [item.detail_id for item in items])
    # 先按店铺归集「失败的件」：一次遍历，避免每个店铺都全表扫一遍 items
    failed_by_shop: dict[str, list[str]] = {}
    for item in items:
        if item.detail_id not in retry_keys:
            continue
        shop_id, warehouse_id = _split_key(item.detail_id)
        if shop_id and warehouse_id:
            failed_by_shop.setdefault(shop_id, []).append(warehouse_id)
    plan: list[dict[str, Any]] = []
    for target in targets:
        shop_id = target["shopId"]
        warehouse_ids = failed_by_shop.get(shop_id) or []
        if warehouse_ids:
            plan.append({"shopId": shop_id,
                         "template": target["template"],
                         "warehouseIds": warehouse_ids})
    if not plan:
        raise errors.validation("没有需要重试的商品：所有商品都已成功",
                                field="batchId", value=batch.id)

    attempted = {_item_key(group["shopId"], wid)
                 for group in plan for wid in group["warehouseIds"]}
    for item in items:
        if item.detail_id in attempted:
            item.status = "pending"
            item.message = ""
            item.updated_at = now_ts()
    failed_shops = [group["shopId"] for group in plan]
    batch.status = "pending"
    batch.finished_at = None
    batch.errors = []
    batch.current_index = 0
    batch.settings = {**settings,
                      "runPlan": plan,
                      "progress": {"stage": "pending", "shopIndex": 0,
                                   "shopTotal": len(plan), "attempted": len(attempted),
                                   "succeeded": 0, "failed": 0}}
    batch.updated_at = now_ts()
    db.flush()
    return {"batch": batch_service.serialize(batch),
            "retried": len(attempted),
            "shops": failed_shops,
            "message": (f"将重试 {len(failed_shops)} 个店铺的 "
                        f"{len(attempted)} 件商品")}


def run(batch_id: str) -> None:
    """后台执行：按店铺逐个发布，每完成一个店铺就落一次进度。

    单个店铺失败不影响其他店铺继续发 —— 5 个店里有 1 个失败，
    剩下 4 个照常发出去，比整批中断有用得多。

    同一个函数也负责「重试」：`retry()` 把失败店铺的失败件写进
    `settings.runPlan` 并把批次改回 pending，再投一次这个任务即可。
    跑的是哪一份清单由 `_run_plan()` 决定，这里不关心是首次还是重试。
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
        plan = _run_plan(settings)
        if not plan:
            batch.status = "failed"
            batch.errors = [*(batch.errors or []), "任务没有可发布的商品，请重新创建"]
            batch.finished_at = now_ts()
            db.commit()
            return
        request = dict(settings.get("request") or {})
        # 重试时只统计这一轮的件：上一轮已成功的不该再算进「本轮成功」，
        # 否则进度条会显示「5 店 10 件成功 50」这种看不懂的数字。
        attempted = {key for group in plan
                     for key in (_item_key(group["shopId"], wid)
                                 for wid in group["warehouseIds"])}
        messages: list[str] = []

        batch.settings = {**settings, "progress": {
            "stage": "publishing", "shopIndex": 0, "shopTotal": len(plan),
            "succeeded": 0, "failed": 0, "attempted": len(attempted)}}
        batch.updated_at = now_ts()
        db.commit()

        for shop_index, target in enumerate(plan, start=1):
            shop_id = str(target.get("shopId") or "")
            template = str(target.get("template") or "").strip() or None
            warehouse_ids = [str(value) for value in (target.get("warehouseIds") or [])]
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
            # 逐件成败。**不能**用「这个店铺有几件成功」代表每一件：
            # 5 件里成功 4 件时，按店铺判会把第 5 件也记成已提交，
            # 用户点重试就再也补不回来（那一件其实从没发出去）。
            status_by_warehouse: dict[str, tuple[bool, str]] = {}
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
                # 台账记录是唯一能逐件对上「哪件成了」的地方：
                # serialize_log 的 warehouseId 就是这里传进去的仓库商品 id。
                for record in (result.get("records") or {}).get("items") or []:
                    warehouse_id = str(record.get("warehouseId") or "")
                    if not warehouse_id:
                        continue
                    state = str(record.get("status") or "")
                    if state in {"fail", "failed", "cancel", "cancelled"}:
                        status_by_warehouse[warehouse_id] = (
                            False, str(record.get("reason") or "发布失败")[:300])
                    else:
                        status_by_warehouse[warehouse_id] = (True, "")
                for failure in result.get("failures") or []:
                    warehouse_id = str(failure.get("warehouseId") or "")
                    if warehouse_id and warehouse_id not in status_by_warehouse:
                        status_by_warehouse[warehouse_id] = (
                            False, str(failure.get("reason") or "发布失败")[:300])
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
                # 逐件状态优先；整批异常（没有台账）时才回落到店铺级判断
                known = status_by_warehouse.get(str(warehouse_id))
                if known is None:
                    succeeded = ok_count > 0
                    reason = fail_message or "发布失败"
                else:
                    succeeded, reason = known
                item.status = "ok" if succeeded else "failed"
                if succeeded:
                    price = price_by_warehouse.get(str(warehouse_id))
                    item.message = (f"已提交 · 售价 {price}" if price
                                    else "已提交，等待平台回执")
                else:
                    item.message = reason or fail_message or "发布失败"
                _merge_result(rows, {"detailId": item.detail_id,
                                     "status": item.status,
                                     "message": item.message, "shopId": shop_id,
                                     "at": now_ts()})
            shop_failed = [str(wid) for wid in warehouse_ids
                           if (status_by_warehouse.get(str(wid)) or (ok_count > 0,))[0]
                           is False]
            if shop_failed:
                messages.append(
                    f"店铺 {shop_id}：{fail_message or '部分商品发布失败'}"
                    f"（{len(shop_failed)}/{len(warehouse_ids)} 件）")
            batch.results = rows
            batch.current_index = shop_index
            batch.errors = messages
            # 只统计本轮真正发过的件（重试时 attempted 是子集）
            done_rows = [r for r in rows
                         if str(r.get("detailId") or "") in attempted]
            batch.settings = {**(batch.settings or {}), "progress": {
                "stage": "publishing", "shopIndex": shop_index,
                "shopTotal": len(plan), "attempted": len(attempted),
                "succeeded": sum(1 for r in done_rows if r.get("status") == "ok"),
                "failed": sum(1 for r in done_rows if r.get("status") == "failed")}}
            batch.updated_at = now_ts()
            db.commit()

        batch = batch_service.get(db, batch_id)
        rows = list(batch.results or [])
        # 批次整体成败按**整批**判断：重试把失败店修好之后，整批就该是 done。
        # 只看本轮的话，「5 店里 1 店失败、重试成功」会永远停在 done 之前的状态。
        batch.status = "failed" if rows and all(
            r.get("status") == "failed" for r in rows) else "done"
        batch.finished_at = now_ts()
        batch.settings = {**{k: v for k, v in (batch.settings or {}).items()
                             if k != "runPlan"},
                          "progress": {
                              **(batch.settings or {}).get("progress", {}),
                              "stage": batch.status}}
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
