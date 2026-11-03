"""发布：三重保险 + 载体模式 + 发布记录。

不可回归的规则（architecture.md 5.3 / docs/api.md 8）：
  1. settings['publish.enabled'] = true；
  2. 请求体 confirm 必须是字面量 true；
  3. items 非空，每项能解析到具体 detailId 或 warehouseId；
  4. 站点语言不匹配且未 allowMismatch → 422；
  5. 全部明细就在目标店铺 → mode=direct，此时模板不生效并必须返回 warning；
  6. 跨店铺 → mode=carrier（公共采集箱重建载体，改写 shopId 与库存映射）；
  7. 每次请求写 publish_logs（processing），回执后更新 success/fail。
"""
from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from core import errors
from core.constants import (
    PUBLISH_MAX_ITEMS,
    SITE_LANGUAGES,
    SITE_NAMES,
    SUPPORTED_LANGUAGES,
)
from core.locks import hold_for_session
from core.utils import (
    clean_text,
    money,
    new_batch_id,
    now_ts,
    product_key,
    redact,
)
from models.account import Account
from models.automation_run import AutomationRun, AutomationRunItem
from models.product import Product
from models.product_claim import ProductClaim
from models.publish_log import PublishLog
from services import (
    carrier_service,
    product_service,
    setting_service,
    warehouse_service,
)
from services import (
    price_template as price_template_module,
)
from services.account_service import (
    account_client,
    map_upstream_error,
    require_shop,
)
from services.miaoshou import MiaoshouClient, MiaoshouError

logger = logging.getLogger("aohuang.publish")

PUBLISH_STATUSES = ("processing", "success", "fail", "cancel")

# 仅用于提示等待时长；结果未确认的发布不会因超过此时长而自动解锁。
PUBLISH_PROCESSING_SECONDS = 600.0


def _is_claim_only_log(db: Session, row: PublishLog) -> bool:
    detail = row.detail if isinstance(row.detail, dict) else {}
    if "claimOnly" in detail:
        return detail["claimOnly"] is True
    # Older warehouse-claim jobs predate the explicit log marker.
    if row.batch_id:
        from models.batch import Batch

        batch = db.get(Batch, row.batch_id)
        return batch is not None and (batch.settings or {}).get("kind") == "warehouse_claim"
    return False


def _in_flight_blocker(db: Session, *, account: Account,
                       keys: list[str], operator_id: int | None) -> None:
    """本地台账里还有 processing 的同一件商品就拒绝。

    锁只管得住同一进程；进程重启、或者有人另起一个实例时锁是空的，
    台账里的 processing 行是唯一还留着的证据。
    processing 只代表结果未确认。超时、断网或进程退出都不能证明平台没有
    受理，因此即使记录很旧，也必须先核对回执，不能自动重新提交。
    """
    wanted = set(keys)
    if not wanted:
        return
    owner_filter = (PublishLog.operator_id.is_(None) if operator_id is None
                    else PublishLog.operator_id == operator_id)
    rows = (db.query(PublishLog)
            .filter(PublishLog.account_id == account.id,
                    owner_filter,
                    PublishLog.status == "processing")
            .all())
    for row in rows:
        key = str(row.warehouse_key or "") or product_key(row.detail_id, row.shop_id)
        if key not in wanted and row.product_id is not None:
            # Legacy carrier logs stored the source-shop warehouse key. The
            # product identity plus the actual target shop still identifies it.
            product = db.get(Product, row.product_id)
            if product is not None:
                key = product_key(product.detail_id, row.shop_id)
        if key not in wanted:
            continue
        if _is_claim_only_log(db, row):
            continue
        raise errors.busy(
            "这件商品的发布结果尚未确认，请先刷新发布记录核对",
            warehouseKey=key, detailId=row.detail_id, shopId=row.shop_id,
            status=row.status, requestedAt=row.requested_at,
            lastRequestedAt=row.last_requested_at)


def _already_published_blocker(db: Session, *, account: Account,
                               entries: list[dict[str, Any]],
                               target_shop_id: str,
                               operator_id: int | None) -> None:
    """拒绝将同一成品重复发布到同一店铺；失败/取消和其他店铺仍可重试。"""
    product_ids = {
        entry["product"].id for entry in entries
        if isinstance(entry.get("product"), Product)
    }
    raw_keys = {
        product_key(entry["detailId"], target_shop_id)
        for entry in entries if not isinstance(entry.get("product"), Product)
    }
    owner_filter = (PublishLog.operator_id.is_(None) if operator_id is None
                    else PublishLog.operator_id == operator_id)
    query = (db.query(PublishLog)
             .filter(PublishLog.account_id == account.id,
                     owner_filter,
                     PublishLog.shop_id == str(target_shop_id),
                     PublishLog.status == "success"))
    clauses = []
    if product_ids:
        clauses.append(PublishLog.product_id.in_(product_ids))
    if raw_keys:
        clauses.append(PublishLog.warehouse_key.in_(raw_keys))
    if not clauses:
        return
    rows = query.filter(or_(*clauses)).all()
    if rows:
        row = rows[0]
        raise errors.AppError(
            "ALREADY_PUBLISHED",
            "该成品已成功发布到此店铺，已阻止重复发布",
            status=409,
            details={"detailId": row.detail_id, "shopId": row.shop_id,
                     "warehouseId": str(row.product_id) if row.product_id else None},
        )


def _require_started_automation_publish(db: Session, *, entries: list[dict[str, Any]],
                                        account: Account, target_shop_id: str,
                                        operator_id: int | None = None) -> None:
    """Automation results can only publish through a started, matching run item."""
    products = [entry["product"] for entry in entries
                if isinstance(entry.get("product"), Product)
                and entry["product"].automation_result]
    if not products:
        return
    if any(product.account_id != account.id for product in products):
        raise errors.AppError(
            "CONFLICT", "自动化商品必须使用创建该自动化任务的妙手账号发布", status=409)
    if any(product.draft_verified_revision != product.revision for product in products):
        raise errors.AppError(
            "CONFLICT", "自动化商品没有与当前版本匹配的妙手草稿读回核验", status=409)

    product_ids = {product.id for product in products}
    query = (db.query(AutomationRunItem, AutomationRun)
            .join(AutomationRun, AutomationRun.id == AutomationRunItem.run_id)
            .filter(
                AutomationRunItem.product_id.in_(product_ids),
                AutomationRunItem.process_status == "succeeded",
                AutomationRunItem.publish_status == "running",
                AutomationRun.account_id == account.id,
                AutomationRun.shop_id == str(target_shop_id),
            ))
    if operator_id is not None:
        query = query.filter(AutomationRun.owner_operator_id == operator_id)
    rows = query.all()
    eligible = {
        item.product_id: run for item, run in rows
        if isinstance(run.settings, dict) and run.settings.get("publishEnabled") is True
    }
    if product_ids - set(eligible):
        raise errors.AppError(
            "CONFLICT", "自动化商品必须由已启动的匹配自动化任务发布", status=409)
    unverified_specs = []
    for product in products:
        quality_status, reasons = warehouse_service._quality_review_summary(product)
        confirmations = eligible[product.id].settings.get(
            "specificationConfirmations")
        confirmed_revision = (confirmations.get(str(product.id))
                              if isinstance(confirmations, dict) else None)
        confirmed_current_revision = (
            type(confirmed_revision) is int
            and confirmed_revision == int(product.revision or 0))
        if (quality_status != "verified"
                and not confirmed_current_revision):
            unverified_specs.append({"warehouseId": str(product.id),
                                     "revision": int(product.revision or 0),
                                     "reasons": reasons})
    if unverified_specs:
        raise errors.AppError(
            "QUALITY_REVIEW_REQUIRED",
            "商品重量或包裹尺寸尚未由老师按当前版本核验；明确确认后才能自动发布",
            status=409,
            details={"items": unverified_specs,
                     "field": "confirmedProductId, confirmedRevision"},
        )


# --- 记录 -----------------------------------------------------------------
def serialize_log(row: PublishLog, *, account_name: str = "",
                  operator_name: str = "") -> dict[str, Any]:
    product_id = str(row.product_id) if row.product_id else None
    return {
        "logId": str(row.id),
        "detailId": row.detail_id,
        "warehouseId": product_id,
        "warehouseKey": row.warehouse_key or "",
        "shopId": row.shop_id,
        "shopName": row.shop_name or "",
        "title": row.title or "",
        "status": row.status,
        "reason": row.reason or "",
        "mode": row.mode,
        "batchId": row.batch_id or "",
        "scheduleId": row.schedule_id,
        "accountId": str(row.account_id) if row.account_id else None,
        "account": account_name,
        # 提交这条记录的人。空串表示「没有操作员上下文」（定时任务 / 老数据），
        # 前端判空串即可，不用区分 null。定时任务发的也没有名字——
        # 那不是「忘了记」，是真的没有人点这一下。
        "operatorName": operator_name or "",
        "platformItemId": row.platform_item_id,
        "itemEditUrl": row.item_edit_url,
        "attempts": int(row.attempts or 1),
        # requestedAt 是首次提交、lastRequestedAt 是最近一次；两个都给出来，
        # 界面才能算「这单挂了多久」而不只是「上次点是什么时候」。
        "requestedAt": row.requested_at,
        "lastRequestedAt": row.last_requested_at or row.requested_at,
        "finishedAt": row.finished_at,
    }


def list_logs(db: Session, *, page: int = 1, page_size: int = 20,
              status: str | None = None, account_id: Any = None,
              shop_id: str | None = None, schedule_id: str | None = None,
              since: float | None = None,
              platform_total: int | None = None,
              degraded: bool = False,
              operator_id: int | None = None) -> dict[str, Any]:
    if status is not None and status != "" and status not in PUBLISH_STATUSES:
        raise errors.validation("发布状态筛选无效", field="status", value=status)
    if page < 1:
        raise errors.validation("页码必须不小于 1", field="page", value=page)
    if not 1 <= page_size <= 100:
        raise errors.validation("每页数量必须在 1 到 100 之间", field="pageSize", value=page_size)
    query = db.query(PublishLog)
    if status:
        query = query.filter(PublishLog.status == status)
    if account_id not in (None, ""):
        text = str(account_id)
        if text.isdigit():
            query = query.filter(PublishLog.account_id == int(text))
    if shop_id:
        query = query.filter(PublishLog.shop_id == str(shop_id))
    if schedule_id:
        query = query.filter(PublishLog.schedule_id == str(schedule_id))
    if since is not None:
        query = query.filter(PublishLog.requested_at >= float(since))
    if operator_id is not None:
        query = query.filter(PublishLog.operator_id == operator_id)
    total = query.count()
    rows = (query.order_by(PublishLog.requested_at.desc(), PublishLog.id.desc())
            .offset((page - 1) * page_size).limit(page_size).all())
    names = warehouse_service.account_names(db)
    operators = warehouse_service.operator_names(db)
    platform_total, total_source = _platform_total(db, account_id, upstream_total=platform_total)
    return {
        "items": [serialize_log(row, account_name=names.get(row.account_id or 0, ""),
                                operator_name=operators.get(row.operator_id or 0, ""))
                  for row in rows],
        "page": page,
        "pageSize": page_size,
        "total": total,
        "hasMore": page * page_size < total,
        "benchOnly": True,
        "platformTotal": platform_total,
        # 两个 total 语义完全不同：total 是本机台账的行数，platformTotal 是
        # 平台侧账号下的真实总量。界面要能说清「这个数从哪来」，否则回源
        # 失败退回本地 count 时用户会以为平台只有这几条。
        "totalSource": total_source,
        "degraded": degraded,
    }


def _platform_total(db: Session, account_id: Any,
                    *, upstream_total: Any = None) -> tuple[int, str]:
    """平台侧发布总数：优先上游回执的 total，取不到才退回本地行数。

    老版读的是上游 `data["total"]`（平台真实总量）。新版退回 `db.query(...).count()`
    只是本地台账，比平台少得多，界面上「平台侧 N 条」就成了假数。
    """
    value = upstream_total
    if isinstance(value, bool):  # bool 是 int 的子类，别把 true 当成 1
        value = None
    if value not in (None, ""):
        try:
            return max(int(value), 0), "platform"
        except (TypeError, ValueError):
            pass
    query = db.query(PublishLog)
    if account_id not in (None, ""):
        text = str(account_id)
        if text.isdigit():
            query = query.filter(PublishLog.account_id == int(text))
    return query.count(), "local"


def _upsert_log(db: Session, *, account: Account, product: Product | None,
                detail_id: str, shop_id: str, shop_name: str, title: str,
                mode: str, batch_id: str, status: str = "processing",
                schedule_id: str | None = None,
                warehouse_key: str | None = None,
                operator_id: int | None = None) -> PublishLog:
    stamp = now_ts()
    # 优先用调用方给的键：载体重建前的那次 detailId 才是这一单的稳定身份，
    # 重建后的新 id 每次都不一样，拿它当键就永远认不回同一行。
    stable_key = (str(warehouse_key or "").strip()
                  or (product.warehouse_key if product is not None else ""))
    row = _find_log(db, account_id=account.id, detail_id=str(detail_id),
                    shop_id=str(shop_id), warehouse_key=stable_key,
                    operator_id=operator_id)
    if row is None:
        row = PublishLog(account_id=account.id, detail_id=str(detail_id),
                         shop_id=str(shop_id), batch_id=batch_id,
                         attempts=1, requested_at=stamp, last_requested_at=stamp)
        db.add(row)
    else:
        row.attempts = int(row.attempts or 1) + 1
        row.batch_id = batch_id
        # requested_at 是「首次提交」，重发时绝不覆盖：覆盖掉就再也判断不出这单
        # 挂了多久（老版 publish_ledger 保留 requestedAt、另记 lastRequestedAt）。
        row.last_requested_at = stamp
        # 命中旧行时 detailId 以本次为准：载体重建后 detailId 会变，行还挂在
        # 旧 id 上就永远等不到平台回执。shopId 不改——下面的查找本来就按店铺
        # 收窄，改它只会把别的店铺那条历史顶掉。
        row.detail_id = str(detail_id)
    row.product_id = product.id if product is not None else None
    # 有载体的真实键 / 调用方给的稳定键就用它；都没有才退回详情键。
    # 反过来会让同一单在载体重建前后存成两个不同的 warehouse_key。
    row.warehouse_key = stable_key or product_key(detail_id, shop_id)
    row.shop_name = shop_name
    row.title = title[:512]
    row.mode = mode
    row.status = status
    row.reason = ""
    row.finished_at = None
    row.schedule_id = schedule_id
    # 用户触发的发布记录真实操作人；后台定时任务没有请求上下文，但会传入
    # 任务所有者用于学员隔离，不把后台执行伪装成一次手动操作。
    if operator_id is not None:
        row.operator_id = operator_id
    db.flush()
    return row


def _find_log(db: Session, *, account_id: int, detail_id: str, shop_id: str,
              warehouse_key: str, operator_id: int | None) -> PublishLog | None:
    """先按「明细 + 店铺」找，找不到再按「仓库键 + 店铺」找。

    载体模式会把商品重建成一个新 detailId，同一张仓库行因此先后用两个 id
    发布；只按 detailId 去重就会记成两条。warehouse_key 是重建后唯一不变的
    东西，所以它是第二判据。

    两级查找都必须带 shop_id：同一张仓库行可以发到不同店铺，那是两件不同的
    事、应该两行记录；只按仓库键命中会把 A 店铺那条改写成 B 店铺的，历史
    直接丢一次。命中多行时取最新一条，避免脏数据把发布流程打挂。
    """
    owner_filter = (PublishLog.operator_id.is_(None) if operator_id is None
                    else PublishLog.operator_id == operator_id)
    row = (db.query(PublishLog)
           .filter(PublishLog.account_id == account_id,
                   owner_filter,
                   PublishLog.detail_id == detail_id,
                   PublishLog.shop_id == shop_id)
           .order_by(PublishLog.id.desc()).first())
    if row is not None:
        return row
    key = str(warehouse_key or "").strip()
    if not key:
        return None
    return (db.query(PublishLog)
            .filter(PublishLog.account_id == account_id,
                    owner_filter,
                    PublishLog.warehouse_key == key,
                    PublishLog.shop_id == shop_id)
            .order_by(PublishLog.id.desc()).first())


def _submit_persisted_publish(db: Session, client: MiaoshouClient, *,
                              detail_ids: list[str], shop_id: str,
                              logs: list[PublishLog]) -> None:
    """Persist the exact submission before an irreversible upstream request.

    A caller rollback or process exit after submission must not erase the only
    evidence that the platform may already have accepted these products.
    """
    numeric_ids = [int(value) for value in detail_ids]
    numeric_shop_id = int(shop_id)
    if len(logs) != len(numeric_ids):
        raise errors.AppError(
            errors.UPSTREAM_UNKNOWN_RESULT,
            "发布商品与待提交记录不一致，已阻止发布", status=502)
    for row in logs:
        row.reason = "正在提交发布，等待平台回执；请勿重复发布"
    # The committed processing rows take over from the session locks when this
    # checkpoint releases them. Claim-only operations never enter this helper.
    db.commit()
    try:
        client.publish_items(numeric_ids, [numeric_shop_id])
    except MiaoshouError as exc:
        rejected = (
            exc.kind == "rate_limit"
            or (exc.status is not None and 400 <= exc.status < 500 and exc.status != 408)
            or (isinstance(exc.payload, dict)
                and bool(exc.code) and exc.code not in {"0", "200", "success"})
        )
        message = redact(str(exc))[:160]
        for row in logs:
            row.status = "fail" if rejected else "processing"
            row.reason = (message if rejected else
                          f"发布结果未知，请刷新发布记录核对，勿重复提交：{message}")
            row.finished_at = now_ts() if rejected else None
        db.commit()
        if rejected:
            raise map_upstream_error(exc) from None
        raise errors.AppError(
            errors.UPSTREAM_UNKNOWN_RESULT,
            "发布请求可能已被平台受理，请刷新发布记录核对；已阻止重复发布",
            status=502, details={"detailIds": detail_ids, "shopId": shop_id},
        ) from None


# --- 语言校验 -------------------------------------------------------------
def site_language(site: str) -> list[str]:
    return list(SITE_LANGUAGES.get(str(site or "").upper(), []))


def check_language(language: str | None, site: str, *,
                   allow_mismatch: bool) -> str | None:
    """校验商品语言与目标站点是否匹配。

    站点未知时不再静默放过，而是返回一条显式 warning（审查 P1-3）。

    判据优先级（P1 修复）：站点已知就以该站点的期望语言为准，全局白名单只在
    站点未知时兜底。以前是先过全局白名单、再比站点，于是两份表一旦不同步
    （SITE_LANGUAGES["ID"] = ["id"]，而 SUPPORTED_LANGUAGES 里没有 id），
    印尼站的合法语言会在走到站点比对之前就被 400 掉——站点自己的期望语言
    被一张更粗的表否掉了，这是自相矛盾的判据。反过来把站点放前面，站点表
    就是唯一事实来源，白名单只负责「站点未知时挡住乱填的语言」。
    """
    wanted = str(language or "").strip()
    # allow_mismatch 短路必须留在最前面：它是用户显式确认过的「我知道不匹配，
    # 仍然要发」，任何校验都不该越过这个决定。
    if not wanted or allow_mismatch:
        return None
    expected = site_language(site)
    if expected:
        # 站点已知：站点期望的语言天然合法，不再看全局白名单。
        if wanted not in expected:
            names = "、".join(expected)
            raise errors.AppError(
                errors.SITE_LANGUAGE_MISMATCH,
                f"商品文字是 {wanted}，但目标店铺站点 {site}"
                f"（{SITE_NAMES.get(site, site)}）应使用 {names}；"
                "确认无误请传 allowMismatch: true",
                status=422,
                details={"language": wanted, "site": site, "expected": expected},
            )
        return None
    # 站点未知：没有站点期望语言可依据，退回全局白名单挡住乱填的语言。
    if wanted not in SUPPORTED_LANGUAGES:
        raise errors.validation(
            f"语言只能是 {' / '.join(SUPPORTED_LANGUAGES)}",
            field="language", value=wanted)
    return ("目标店铺站点未知，已跳过站点语言比对；"
            "请先重新同步店铺信息或在设置里补全站点后再发布。")


# --- 发布解析 -------------------------------------------------------------
def _resolve_items(db: Session, raw: Iterable[Mapping[str, Any]]
                   ) -> list[dict[str, Any]]:
    resolved: list[dict[str, Any]] = []
    missing: list[str] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise errors.validation(f"第 {index + 1} 项不是对象", field=f"items[{index}]")
        warehouse_id = item.get("warehouseId")
        detail_id = item.get("detailId")
        shop_id = item.get("shopId")
        if warehouse_id not in (None, ""):
            try:
                product = warehouse_service.resolve_product(db, warehouse_id)
            except errors.AppError as exc:
                if exc.code == errors.NOT_FOUND:
                    missing.append(str(warehouse_id))
                    continue
                raise
            resolved.append({
                "product": product,
                "detailId": product.detail_id,
                "shopId": product.shop_id,
                "warehouseId": str(product.id),
                "title": product.title or "",
                "payload": product.payload if isinstance(product.payload, dict) else {},
            })
            continue
        if detail_id in (None, "") or shop_id in (None, ""):
            raise errors.validation(
                f"第 {index + 1} 项必须给 warehouseId，或 detailId + shopId 配对",
                field=f"items[{index}]")
        text = str(detail_id).strip()
        if not text.isdigit():
            raise errors.validation(f"第 {index + 1} 项的 detailId 必须是数字",
                                    field=f"items[{index}].detailId", value=text)
        key = product_key(text, shop_id)
        product = (db.query(Product)
                   .filter(Product.warehouse_key == key, Product.deleted_at.is_(None))
                   .one_or_none())
        resolved.append({
            "product": product,
            "detailId": text,
            "shopId": str(shop_id),
            "warehouseId": str(product.id) if product else None,
            "title": product.title if product else "",
            "payload": (product.payload if product and isinstance(product.payload, dict) else {}),
        })
    if missing:
        raise errors.not_found(f"有 {len(missing)} 件商品不在仓库里：{'、'.join(missing[:5])}",
                               missing=missing)
    if not resolved:
        raise errors.validation("请至少选择一件要发布的商品", field="items")
    return resolved


def _load_carrier_payloads(db: Session, entries: list[dict[str, Any]],
                           account: Account, client: MiaoshouClient | None
                           ) -> list[dict[str, Any]]:
    """没有仓库 payload 时回上游读详情（采集箱里的草稿也能跨店铺发布）。"""
    payloads: list[dict[str, Any]] = []
    for entry in entries:
        payload = entry.get("payload") or {}
        if payload:
            payloads.append(payload)
            continue
        if client is None:
            raise errors.not_found(
                f"商品 {entry['detailId']} 不在仓库里且无法读取上游详情",
                detailId=entry["detailId"])
        try:
            response = client.shop_detail(entry["detailId"], entry["shopId"])
        except MiaoshouError as exc:
            raise map_upstream_error(exc) from None
        info = product_service.detail_block(response)["info"]
        if not info:
            raise errors.not_found(f"上游查无明细 {entry['detailId']}",
                                   detailId=entry["detailId"])
        payloads.append(info)
    return payloads


def _repricing(payloads: list[dict[str, Any]], entries: list[dict[str, Any]],
               template: Mapping[str, Any] | None, rate: Any
               ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """按模板给每件商品重算售价（用它自己的重量与采购价）。"""
    if template is None:
        return payloads, []
    priced: list[dict[str, Any]] = []
    reports: list[dict[str, Any]] = []
    for payload, entry in zip(payloads, entries, strict=True):
        try:
            new_payload, report = price_template_module.apply_template(
                payload, template, rate=rate)
        except price_template_module.PriceTemplateError as exc:
            raise errors.AppError(errors.TEMPLATE_INVALID,
                                  f"商品 {entry['detailId']} 无法按模板重新定价：{exc}",
                                  status=422, details={"field": "template"}) from None
        samples = report.get("samples") or []
        before = samples[0].get("before") if samples else None
        after = samples[0].get("after") if samples else None
        reports.append({
            "warehouseId": entry.get("warehouseId"),
            "detailId": entry["detailId"],
            "priceBefore": money(before),
            "priceAfter": money(after),
            "currency": "MYR",
            "weightKg": _weight_of(new_payload),
            "changed": report.get("changedCount"),
            "skuCount": report.get("skuCount"),
        })
        priced.append(new_payload)
    return priced, reports


def _weight_of(payload: Mapping[str, Any]) -> str | None:
    value = payload.get("weight")
    return str(value) if value not in (None, "") else None


def _find_template(db: Session, name: str | None) -> Mapping[str, Any] | None:
    wanted = str(name or "").strip()
    if not wanted:
        return None
    raw = setting_service.price_templates(db).get(wanted)
    if not raw:
        raise errors.AppError(errors.TEMPLATE_INVALID, f"定价模板「{wanted}」不存在",
                              status=422, details={"field": "template"})
    try:
        return price_template_module.normalize_template(raw)
    except price_template_module.PriceTemplateError as exc:
        raise errors.AppError(errors.TEMPLATE_INVALID, f"定价模板不可用：{exc}",
                              status=422, details={"field": "template"}) from None


def publish(db: Session, payload: Mapping[str, Any], *,
            operator_id: int | None = None,
            _log_operator_id: int | None = None) -> dict[str, Any]:
    """POST /publish。发布不可撤销，所有守卫都在这里。

    学员可发布本人已认领的成品，或本人匹配自动化任务已核验的处理结果。
    定时任务（schedule_service.run_due）不传 operator_id，不属于学员发布。
    """
    if payload.get("confirm") is not True:
        raise errors.AppError(errors.CONFIRM_REQUIRED,
                              "发布需要确认：请求体必须包含 \"confirm\": true",
                              status=400)
    # 「认领到采集箱」不是发布：它只把商品放进目标店铺的采集箱，不上架。
    # 发布总开关只管「上架」，不该拦住认领——否则用户没开发布也要能正常认领。
    _claim_only_request = payload.get("claimOnly") is True
    if not _claim_only_request and not setting_service.publish_enabled(db):
        raise errors.AppError(errors.PUBLISH_DISABLED,
                              "发布总开关未打开：请先在设置里开启发布",
                              status=403)
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise errors.validation("items 必须是非空数组", field="items")
    if len(items) > PUBLISH_MAX_ITEMS:
        raise errors.validation(f"一次最多发布 {PUBLISH_MAX_ITEMS} 件商品",
                                field="items", value=len(items))
    target_shop_id = clean_text(payload.get("shopId"))
    if not target_shop_id:
        raise errors.validation("请选择发布到哪个店铺", field="shopId")

    account, client = account_client_for(db, payload.get("accountId"))
    target_shop = require_shop(db, account, target_shop_id)
    from services import operator_service

    operator = operator_service.get(db, operator_id) if operator_id is not None else None
    member_publish = (operator is not None
                      and operator_service.role_of(operator) == operator_service.ROLE_MEMBER
                      )
    # 妙手账号是团队共享资源（负责人 2026-11-02 明确：一个账号可以多人绑定），
    # 因此不再按 owner_operator_id 拦「其他学员的账号」。
    # 台账返回里要显示「谁发的」，先取一次名字（一次查询，不逐行查）
    log_operator_id = operator_id if operator_id is not None else _log_operator_id
    operator_name = ""
    if log_operator_id is not None:
        operator_name = warehouse_service.operator_names(db).get(int(log_operator_id), "")
    entries = _resolve_items(db, items)
    _require_started_automation_publish(
        db, entries=entries, account=account, target_shop_id=target_shop_id,
        operator_id=operator_id)
    claim_only = payload.get("claimOnly") is True
    member_automation_publish = False
    if member_publish:
        if claim_only:
            raise errors.AppError(errors.FORBIDDEN,
                                  "学员只能发布已认领的成品商品",
                                  status=403)
        if any(not isinstance(item, Mapping) or item.get("warehouseId") in (None, "")
               for item in items):
            raise errors.AppError(errors.FORBIDDEN,
                                  "学员发布必须选择已认领成品或本人自动化处理结果",
                                  status=403)
        product_ids = [entry["product"].id for entry in entries
                       if isinstance(entry.get("product"), Product)]
        claims = (db.query(ProductClaim.product_id)
                  .filter(ProductClaim.owner_operator_id == operator.id,
                          ProductClaim.status == "claimed",
                          ProductClaim.product_id.in_(product_ids)).all()
                  if product_ids else [])
        claimed_ids = {row[0] for row in claims}
        automation_ids = {entry["product"].id for entry in entries
                          if isinstance(entry.get("product"), Product)
                          and entry["product"].automation_result
                          and entry["product"].operator_id == operator.id
                          and entry["product"].account_id == account.id}
        if len(claimed_ids | automation_ids) != len(entries):
            raise errors.AppError(errors.FORBIDDEN,
                                  "商品未由当前学员认领或处理，不能发布",
                                  status=403)
        if automation_ids and len(automation_ids) != len(entries):
            raise errors.validation("请分别发布自动化处理结果和已认领成品", field="items")
        member_automation_publish = bool(automation_ids)
        unverified_specs = []
        for entry in entries:
            product = entry.get("product")
            if not isinstance(product, Product):
                continue
            quality_status, reasons = warehouse_service._quality_review_summary(product)
            if quality_status != "verified":
                unverified_specs.append({"warehouseId": str(product.id),
                                         "reasons": reasons})
        if (not member_automation_publish and unverified_specs
                and payload.get("confirmUnverifiedSpecifications") is not True):
            raise errors.AppError(
                "QUALITY_REVIEW_REQUIRED",
                "所选商品存在待核验规格；请检查后明确确认再继续发布",
                status=409,
                details={"items": unverified_specs,
                         "field": "confirmUnverifiedSpecifications"},
            )
    batch_id = str(payload.get("batchId") or "").strip() or new_batch_id("pub")
    language = payload.get("language")
    language_warning: str | None = None
    if not claim_only:
        language_warning = check_language(
            language, str(target_shop.get("site") or ""),
            allow_mismatch=payload.get("allowMismatch") is True)

    # 发布互斥的键：每件商品在**目标店铺**上的仓库键。
    # 用目标店铺而不是源店铺，是因为「同一件发到同一个店铺」才是真正重复的
    # 那件事；发到两个不同店铺是两条独立的生产线，不该互相挡。
    publish_keys = [product_key(entry["detailId"], target_shop_id)
                    for entry in entries]

    template_name = payload.get("template")
    member_claimed_publish = member_publish and not member_automation_publish
    if member_claimed_publish and not str(template_name or "").strip():
        template_name = setting_service.selected_template_name(db, str(account.id))
    template = _find_template(db, template_name)
    if member_claimed_publish and template is None:
        raise errors.AppError(errors.TEMPLATE_INVALID,
                              "学员发布需要先选择可用的定价模板",
                              status=422, details={"field": "template"})
    rate = payload.get("exchangeRate")
    if rate not in (None, ""):
        try:
            rate_d = float(str(rate))
        except (TypeError, ValueError):
            raise errors.validation("汇率必须是数字", field="exchangeRate", value=rate) from None
        if not 0 < rate_d <= 1000:
            raise errors.validation("汇率必须大于 0 且不超过 1000",
                                    field="exchangeRate", value=rate)
        rate = rate_d
    titles = payload.get("titles") if isinstance(payload.get("titles"), dict) else {}

    same_shop = (not member_claimed_publish and
                 all(str(entry["shopId"]) == str(target_shop_id) for entry in entries))
    mode = "direct" if same_shop else "carrier"
    warning: str | None = language_warning
    filled_sources: list[int] = []

    published: list[str] = []
    claimed_ids: list[str] = []
    failures: list[dict[str, Any]] = []
    repriced: list[dict[str, Any]] = []
    logged: list[PublishLog] = []

    # 发布互斥。位置是刻意的：所有参数校验都做完了，还没碰上游。
    # 抢不到立刻 409（非阻塞），不用等一个几十秒的载体重建；
    # 抢到之后再用台账查一遍在途发布，挡住「进程重启 / 另起一个实例」的情况。
    #
    # 会话级锁：一直持到路由里 commit 之后。函数级的锁在这里同样会漏 ——
    # 台账行（processing）是在提交时才可见的，锁早放一秒，第二个人就查不到
    # 这行在途记录，然后对同一件商品再发一次。
    # 同一个妙手账号可能绑定多个软件用户。锁和台账阻止重复发布时按软件
    # 用户隔离，避免一个学员的操作锁住或覆盖另一个学员的记录。
    scoped_publish_keys = [f"{log_operator_id or 0}:{key}" for key in publish_keys]
    hold_for_session(
        db, scoped_publish_keys, blocking=False,
        busy_message="这件正在发布中，请等上一轮结束")
    try:
        _in_flight_blocker(db, account=account, keys=publish_keys,
                           operator_id=log_operator_id)
        if not claim_only:
            _already_published_blocker(
                db, account=account, entries=entries,
                target_shop_id=str(target_shop_id),
                operator_id=log_operator_id)
        if claim_only:
            # 认领的本质：把「服务器里存着的商品」分发一份到目标账号/店铺的采集箱。
            # 仓库是本地权威数据源（标题/图片/SKU/价格/重量都在 payload 里），
            # 因此**不依赖上游公共采集箱里是否还有这件商品**：
            #   · 先用 claim_to_shop 试一次同店认领（便宜、快，上游还在时最省事）；
            #   · 上游已经清掉这件商品时（「未找到采集箱商品信息」），自动降级为
            #     「用本地 payload 新建载体并写入」——这才是「存到服务器、所有账号都能分销」
            #     的语义。之前同店直接抛错，等于把仓库商品绑死在采集箱上。
            detail_ids = [int(entry["detailId"]) for entry in entries
                          if str(entry["detailId"]).isdigit()]
            need_rebuild = payload.get("forceRebuild") is True
            if detail_ids and not need_rebuild:
                try:
                    claim_response = client.claim_to_shop(detail_ids, [int(target_shop_id)])
                    parsed = carrier_service.claimed_detail_ids(claim_response or {})
                    if parsed:
                        claimed_ids.extend(str(value) for value in parsed)
                    else:
                        raise errors.unprocessable(
                            "认领请求已提交但未返回商品 ID，请先检查采集箱，勿重复操作")
                except MiaoshouError as exc:
                    message = str(exc).lower()
                    if not any(text in message for text in ("未找到采集箱商品", "missing")):
                        raise errors.unprocessable(
                            "认领结果未确认，请先检查采集箱，勿重复操作") from exc
                    # 上游采集箱里已没有这件商品（最常见）→ 走本地重建。
                    logger.info("同店认领失败，改用本地 payload 重建载体：%s", exc)
                    need_rebuild = True
            elif not detail_ids:
                need_rebuild = True

            if need_rebuild or (not claimed_ids and entries):
                # 用本地 payload 在目标店铺新建载体并写入商品内容。
                payloads = _load_carrier_payloads(db, entries, account, client)
                carrier_service.harvest_from_payloads(db, payloads)
                try:
                    listing = client.common_box_list(page_no=1, page_size=20)
                    box_rows_ = (listing.get("data") or {}).get("detailList") or []
                    if box_rows_:
                        carrier_service.harvest_from_common_box(db, box_rows_)
                        db.flush()
                except Exception as exc:  # noqa: BLE001 - 补不到就让下面报缺料
                    logger.warning("认领前补链接池失败：%s", exc)
                try:
                    carriers = carrier_service.collect_carriers(db, client, count=len(entries))
                    filled = carrier_service.fill_carriers(
                        client, carriers, payloads, shop_id=int(target_shop_id))
                except carrier_service.CarrierError as exc:
                    raise errors.unprocessable(
                        f"认领需要新建载体并写入商品，但失败了：{exc}") from None
                rebuilt = [str(v) for v in (filled.get("saved") or [])]
                claimed_ids = rebuilt
                filled_sources = filled.get("savedSources") or []
                for reason in (filled.get("failures") or []):
                    failures.append({"stage": "carrier", "reason": str(reason)[:200]})
                if not rebuilt:
                    try:
                        client.delete_collect_box([int(v) for v in carriers])
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("清理失败载体出错（%s）：%s", carriers, exc)
                    reason = ("用本地商品数据新建载体并写入时失败"
                              if not failures else "认领失败（临时载体已删除）：" + "；".join(
                                  str(item.get("reason") or "") for item in failures)[:180])
                    raise errors.unprocessable(reason)

            for entry, claimed_detail_id in _pair_entries(
                    entries, [], claimed_ids, saved_sources=filled_sources):
                product = entry.get("product")
                if product is not None and isinstance(product, Product):
                    state = dict(product.collected or {})
                    state[f"account:{account.id}"] = {
                        "at": now_ts(), "detailId": claimed_detail_id,
                        "shopId": str(target_shop_id),
                    }
                    product.collected = state
                    product.claimed = True
                    product.claimed_at = now_ts()
                    product.claimed_shop_id = str(target_shop_id)
                    # 认领也改了这一行，推版本号（理由同 _mark_published）
                    warehouse_service.bump_revision(product)
            db.flush()
        elif same_shop:
            detail_ids = [int(entry["detailId"]) for entry in entries]
            published = [str(value) for value in detail_ids]
            if template is not None:
                template_warning = (
                    f"这 {len(entries)} 件就在目标店铺，发布的是平台里已有的草稿，"
                    f"价格已经固定；模板「{template.get('name')}」没有生效。"
                    "要改价请先在处理页按该模板重新保存这些商品。")
                warning = (f"{warning}；{template_warning}" if warning else template_warning)
        else:
            payloads = _load_carrier_payloads(db, entries, account, client)
            payloads, repriced = _repricing(payloads, entries, template, rate)
            # 载体重建前先用待发布商品的货源补链接池（与旧实现 sync_source_links 一致）
            carrier_service.harvest_from_payloads(db, payloads)
            carriers = carrier_service.collect_carriers(db, client, count=len(entries))
            filled = carrier_service.fill_carriers(client, carriers, payloads,
                                                   shop_id=int(target_shop_id))
            failures.extend({"detailId": item.split(":", 1)[0], "reason": item}
                            for item in filled["failures"])
            if not filled["saved"]:
                raise errors.AppError(
                    errors.CARRIER_UNAVAILABLE,
                    "载体全部写入失败，本次没有发布任何商品",
                    status=422, details={"failures": filled["failures"][:5]})
            sources = filled.get("savedSources") or []
            filled_sources = [int(i) for i in sources if isinstance(i, int)
                              and 0 <= i < len(entries)]
            published = [str(value) for value in filled["saved"]]
            # 载体重建后 detailId 变了：保留原 id 供标题/日志回溯
            for index, detail_id in zip(sources, filled["saved"], strict=True):
                if isinstance(index, int) and 0 <= index < len(entries):
                    entry = entries[index]
                    entry["originDetailId"] = entry["detailId"]
                    entry["detailId"] = str(detail_id)

        # 写台账（processing）：detailId 用发布后的 id（载体重建是新 id）
        for entry, detail_id in _pair_entries(entries, published, claimed_ids,
                                              saved_sources=filled_sources):
            title = clean_text(titles.get(entry.get("originDetailId") or entry["detailId"])
                               or titles.get(entry["detailId"])
                               or entry.get("title") or "", 512)
            row = _upsert_log(db, account=account, product=entry.get("product"),
                              detail_id=detail_id, shop_id=str(target_shop_id),
                              shop_name=str(target_shop.get("name") or ""),
                              title=title, mode=mode, batch_id=batch_id,
                              status="processing",
                              schedule_id=payload.get("scheduleId"),
                              # 载体重建会换掉 detailId，originDetailId 是重建前
                              # 那个 id；用它拼出的仓库键才是这一单的稳定身份。
                              warehouse_key=product_key(
                                  entry.get("originDetailId") or entry["detailId"],
                                  target_shop_id),
                              operator_id=log_operator_id)
            row.detail = {**(row.detail or {}), "claimOnly": claim_only}
            logged.append(row)
        db.flush()
        if published and not claim_only:
            _submit_persisted_publish(
                db, client, detail_ids=published, shop_id=str(target_shop_id), logs=logged)
        # 平台只「已受理」时**不能**记 success：发布是异步的，平台侧稍后才判
        # 成功或失败。强制写 success 有两个后果——界面上把未确认的发布显示成
        # 「发布成功」，而且 /logs/refresh 只回源 processing 行，这些行再也
        # 拿不到回执，平台判 failed 也改不回来。
        # 保留 processing，等后台回源确认。
        for row in logged:
            row.status = "processing"
            row.reason = "已提交，等待平台回执"
            row.finished_at = None
        db.flush()
    except MiaoshouError as exc:
        raise map_upstream_error(exc) from None
    # 这里**没有** finally 释放锁，是故意的：锁活到路由里那次 commit/rollback
    # 之后才由会话事件释放（core/locks.py 的 session_lock）。
    # 用 finally 立刻释放会漏：台账里那行 processing 还没提交，第二个人查不到，
    # 照样能把同一件商品再发一遍 —— 这正是这把锁要挡的事。

    return {
        "publishRequested": True,
        "mode": mode,
        "batchId": batch_id,
        "targetShopId": str(target_shop_id),
        "targetShopName": str(target_shop.get("name") or ""),
        "published": published,
        "claimed": claimed_ids,
        "claimedWarehouseIds": [str(entry["product"].id) for entry, _ in
                                _pair_entries(entries, [], claimed_ids,
                                              saved_sources=filled_sources)
                                if isinstance(entry.get("product"), Product)],
        "failures": failures,
        "warning": warning,
        "repriced": repriced,
        "records": {
            "total": len(logged),
            "items": [serialize_log(row, account_name=account.name,
                                    operator_name=operator_name) for row in logged],
        },
    }


def _pair_entries(entries: list[dict[str, Any]], published: list[str],
                  claimed: list[str],
                  saved_sources: list[int] | None = None
                  ) -> list[tuple[dict[str, Any], str]]:
    """把「发布后的 id」与「仓库条目」配对。

    载体部分失败时 published 只含成功项，必须按 savedSources 的下标配对；
    仅当没有 savedSources（direct / claimOnly 分支）时才按顺序整体配对。
    """
    ids = claimed or published
    if not ids:
        return []
    if saved_sources and len(saved_sources) == len(ids):
        paired: list[tuple[dict[str, Any], str]] = []
        for index, detail_id in zip(saved_sources, ids, strict=True):
            if isinstance(index, int) and 0 <= index < len(entries):
                paired.append((entries[index], str(detail_id)))
        return paired
    if len(ids) == len(entries):
        return list(zip(entries, ids, strict=True))
    # 无配对信息时宁可少记日志，也不张冠李戴
    return []


def reconcile_product_publish(db: Session, log: PublishLog) -> None:
    """Only a final successful receipt can mark a teacher's shared product published."""
    if log.status != "success" or log.product_id is None or _is_claim_only_log(db, log):
        return
    if log.operator_id is not None:
        from models.operator import Operator
        from services import operator_service

        operator = db.get(Operator, log.operator_id)
        # A member's publication is represented by their own log and never
        # changes the shared product for other members.
        if operator is None or operator_service.role_of(operator) == operator_service.ROLE_MEMBER:
            return
    product = db.get(Product, log.product_id)
    account = db.get(Account, log.account_id) if log.account_id is not None else None
    if product is None or product.deleted_at is not None or account is None:
        return
    _mark_published(db, [{"product": product, "detailId": log.detail_id}], log.shop_id, account)


def _mark_published(db: Session, entries: list[dict[str, Any]], shop_id: str,
                    account: Account) -> None:
    stamp = now_ts()
    for entry in entries:
        product = entry.get("product")
        if not isinstance(product, Product):
            continue
        draft_verified = product.draft_verified_revision == product.revision
        quality_verified = warehouse_service._quality_review_summary(product)[0] == "verified"
        product.claimed = True
        product.claimed_at = product.claimed_at or stamp
        product.claimed_shop_id = str(shop_id)
        product.status = "published"
        state = dict(product.collected or {})
        state[account.name] = {"at": stamp, "detailId": entry.get("detailId")}
        product.collected = state
        product.updated_at = stamp
        # 发布也会改商品行（status → published）。漏了这一步，别人手上那份
        # 「发布前」的详情页仍然能拿旧版本号改标题，把刚发布的商品改回 ready。
        warehouse_service.bump_revision(product)
        # Only publication metadata changed; verified product contents stay valid.
        if draft_verified:
            product.draft_verified_revision = product.revision
        if quality_verified:
            product.quality_review = {
                **(product.quality_review or {}), "verifiedRevision": product.revision,
            }
    db.flush()


def account_client_for(db: Session, account_id: Any) -> tuple[Account, MiaoshouClient]:
    from services.account_service import require_account

    account = require_account(db, account_id)
    return account, account_client(account)


def available_shops(db: Session, account: Account) -> list[dict[str, Any]]:
    from services.account_service import cached_shops, fetch_shops

    shops = cached_shops(account)
    if shops:
        return shops
    shops, _degraded = fetch_shops(db, account, refresh=True)
    return shops
