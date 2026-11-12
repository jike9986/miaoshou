"""成品仓库：分页查询（含 facets）、详情、改类目/分组、删除、批量操作。

分类计数规则（硬性要求）：**自定义分组优先于平台类目**——一个商品只算一个
标签。有分组就计入分组桶，没有分组才计入平台类目桶，避免同一件商品被数两次。
"""
from __future__ import annotations

import logging
import re
import threading
import zlib
from collections.abc import Iterable, Mapping
from copy import deepcopy
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import and_, false, func, or_, select
from sqlalchemy import update as sql_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core import errors
from core.constants import (
    DETAIL_IMAGE_HARD_LIMIT,
    MAIN_IMAGE_LIMIT,
    NEAR_LIMIT_RATIO,
    WAREHOUSE_MAX_ITEMS,
)
from core.utils import (
    money,
    now_ts,
    product_key,
)
from models.account import Account
from models.automation_run import AutomationRun, AutomationRunItem
from models.batch import Batch
from models.category import Category
from models.group import Group
from models.image_job import ImageJob
from models.product import Product
from models.product_claim import ProductClaim
from models.publish_log import PublishLog
from models.setting import CUSTOM_CATEGORIES_KEY
from services import (
    category_service,
    gate_service,
    pre_save_check,
    setting_service,
    title_save_service,
    translation_coverage,
)
from services.miaoshou import MiaoshouClient, MiaoshouError

# 剩余容量低于总上限的这个比例时，界面该开始提醒「快满了」。
# 阈值统一在 core/constants.py 里（NEAR_LIMIT_RATIO），不在这里再定义一份：
# 这里曾经本地写过 `NEAR_LIMIT_RATIO = 0.1`，跟常量表的 0.8 相反 ——
# 同一个名字两个值，stats() 的 `total >= MAX * RATIO` 就会在「只装了 10%」
# 的时候就报「快满了」。两处只留一处，判定式与常量同源。

SORTS = {
    "savedDesc": (Product.saved_at.desc(), Product.id.desc()),
    "savedAsc": (Product.saved_at.asc(), Product.id.asc()),
    "priceAsc": (Product.price_min.asc(), Product.id.asc()),
    "priceDesc": (Product.price_min.desc(), Product.id.desc()),
    "titleAsc": (Product.title.asc(), Product.id.asc()),
}
STATUSES = {"ready", "claimed", "published", "archived"}
_SENSITIVE_PAYLOAD_KEY_PARTS = (
    "secret", "password", "token", "authorization", "credential", "cookie",
)
_MEMBER_PRIVATE_PAYLOAD_KEYS = {
    "accountid", "accountname", "operatorid", "owneroperatorid",
    "boundbyoperatorid",
}

logger = logging.getLogger(__name__)


def _redact_payload(value: Any, *, member_view: bool = False) -> Any:
    if isinstance(value, dict):
        safe: dict[str, Any] = {}
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if any(part in normalized for part in _SENSITIVE_PAYLOAD_KEY_PARTS):
                continue
            if normalized in {"appkey", "accesskey", "privatekey"}:
                continue
            if member_view and normalized in _MEMBER_PRIVATE_PAYLOAD_KEYS:
                continue
            safe[str(key)] = _redact_payload(item, member_view=member_view)
        return safe
    if isinstance(value, list):
        return [_redact_payload(item, member_view=member_view) for item in value]
    return value


# --- 序列化 ---------------------------------------------------------------
def serialize(product: Product, *, account_name: str = "",
              include_payload: bool = False, operator_name: str = "",
              claim: ProductClaim | None = None,
              operator_id: int | None = None,
              member_view: bool = False,
              account_claim: dict[str, Any] | None = None,
              collected: dict[str, Any] | None = None,
              publish_log: PublishLog | None = None,
              published_shop_ids: list[str] | None = None,
              has_successful_publish: bool = False) -> dict[str, Any]:
    images = product.images if isinstance(product.images, list) else []
    quality_review_status, quality_review_reasons = _quality_review_summary(product)
    is_claimed = (bool(account_claim) if account_claim is not None else
                  (bool(claim) if operator_id is not None else bool(product.claimed)))
    claimed_at = (account_claim.get("at") if account_claim is not None else
                  (claim.claimed_at if claim else
                   (product.claimed_at if operator_id is None else None)))
    body: dict[str, Any] = {
        "id": str(product.id),
        "key": product.warehouse_key,
        "detailId": product.detail_id,
        "shopId": product.shop_id,
        "shopName": product.shop_name or "",
        "accountId": str(product.account_id) if product.account_id else None,
        "account": account_name,
        "title": product.title or "",
        "language": product.language or "",
        "site": product.site or "",
        "images": images,
        "image": images[0] if images else (product.cover or ""),
        "imageCount": len(images),
        "detailImageCount": len(product.detail_images or []),
        "skuCount": int(product.sku_count or 0),
        # 包裹重量（kg）：卡片右下角要显示，不用为这一项再拉详情
        "weight": _payload_weight(product),
        "purchasePriceMin": money(product.price_min),
        "purchasePriceMax": money(product.price_max),
        "purchaseCurrency": product.price_currency or "CNY",
        "cid": product.cid or "",
        "categoryId": str(product.category_id) if product.category_id else None,
        "categoryName": product.category_name or "",
        "groupId": str(product.group_id) if product.group_id else None,
        "group": product.group_name or "",
        "savedAt": product.saved_at,
        "updatedAt": product.updated_at,
        # 最后改这件商品的人，给前端显示「14:32 由张三修改」。
        # 空字符串（不是 null）表示「不知道/老数据」：老库补列后这一列全是 NULL，
        # 前端只要判空串就能统一处理，不用区分 null 与 ""。
        # 人不在了（离职被删）也显示空——悬空的 id 查不出名字，比显示一个
        # 猜出来的名字诚实。
        "updatedBy": operator_name or "",
        # 乐观锁版本号。前端改完要把它原样带回来（PATCH 的 expectedRevision），
        # 服务端比对不上就 409 —— 这就是「两个人同时改同一件商品」的拦截点。
        "revision": int(product.revision or 0),
        "claimed": is_claimed,
        "claimedAt": claimed_at,
        "claimedShopId": (account_claim.get("shopId") if account_claim is not None else
                          (product.claimed_shop_id if operator_id is None else None)),
        "claimStatus": ("claimed" if account_claim else None) if account_claim is not None else (
            claim.status if claim else None),
        "legacyClaimed": bool(product.claimed),
        "collected": (collected if collected is not None else
                      (product.collected if isinstance(product.collected, dict) else {})),
        "salesLast30": int(product.sales_last_30 or 0),
        "stock": int(product.stock or 0),
        "status": (
            "published" if member_view and (has_successful_publish or
                (publish_log is not None and publish_log.status == "success")) else
            ("archived" if member_view and product.status == "archived" else
             ("claimed" if member_view and is_claimed else
              ("ready" if member_view else (product.status or "ready"))))
        ),
        "publishStatus": publish_log.status if member_view and publish_log else None,
        "publishShopId": publish_log.shop_id if member_view and publish_log else None,
        "publishedShopIds": published_shop_ids or [] if member_view else [],
        "skuProperties": product.sku_properties if isinstance(product.sku_properties, list) else [],
        "qualityReviewStatus": quality_review_status,
        "qualityReviewReasons": quality_review_reasons,
        # 产品标准标记：product_standard 判定「数据缺失必须重新采集」时
        # 会往 payload 里写这两个键。之前只留在 payload 里，列表和卡片都看不到，
        # 于是这类商品和正常商品长得一模一样，直到认领时才被静默跳过。
        # 提到顶层后前端才能在卡片上打标记、在筛选里单独找出来。
        "needsRecollect": bool(_payload_flag(product, "needsRecollect")),
        "needsRecollectReason": str(_payload_flag(product, "needsRecollectReason") or ""),
    }
    if include_payload:
        payload = product.payload if isinstance(product.payload, dict) else {}
        body["payload"] = _redact_payload(payload, member_view=member_view)
        body["qualityReview"] = product.quality_review
    # 成品仓库是跨学员共享的商品目录。学员可以看到并认领商品，
    # 但不能从返回体或筛选面板反推出商品最初绑定的妙手账号。
    if member_view:
        body["accountId"] = None
        body["account"] = ""
        body["collected"] = {}
    return body


def _payload_flag(product: Product, key: str) -> Any:
    """从商品 payload 里取一个标记字段（payload 可能不是 dict，统一兜底）。"""
    payload = product.payload if isinstance(product.payload, dict) else {}
    return payload.get(key)


def _positive_measure(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 and number < float("inf") else None


def _payload_weight(product: Product) -> float | None:
    """payload 里的包裹重量（kg）；没有或非法返回 None。给商品卡显示用。"""
    payload = product.payload if isinstance(product.payload, dict) else {}
    raw = payload.get("weight")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value != value or value <= 0 or value == float("inf"):
        return None
    return round(value, 3)


def _quality_review_summary(product: Product) -> tuple[str, list[str]]:
    review = product.quality_review
    if isinstance(review, dict) and review:
        try:
            verified_revision = int(review.get("verifiedRevision"))
        except (TypeError, ValueError):
            verified_revision = None
        if verified_revision == int(product.revision or 0):
            return "verified", []
        return "needs_review", ["stale_review"]

    payload = product.payload if isinstance(product.payload, dict) else {}
    reasons: list[str] = []
    weight = _positive_measure(payload.get("weight"))
    if weight is None:
        reasons.append("missing_weight")

    dimensions = [
        _positive_measure(payload.get(key))
        for key in ("packageLength", "packageWidth", "packageHeight")
    ]
    if any(value is None for value in dimensions):
        reasons.append("missing_dimensions")
    elif dimensions == [30, 20, 10]:
        # 老默认值：可能是「没按品类填」，仍提示一次
        reasons.append("default_dimensions")

    if not reasons:
        reasons.append("unverified")

    # 合理区间或品类默认值不能证明实物规格已核验；只认与当前版本关联的来源记录。
    return "needs_review", reasons


# 按品类给包裹规格。鞋子是鞋盒，服饰是软包装，尺寸差别很大；
# 用错规格会直接算错跨境运费（按体积重计费时差得更多）。
# 重量区间对应「这类商品正常打包后的实际重量」：
#   · 暴走鞋/轮滑鞋自带轮子与支架，比普通鞋沉，上限放到 1.5kg（负责人明确）；
#   · 普通运动鞋/帆布鞋 0.2~1.2kg；
#   · 服饰类软包装 0.1~1.0kg。
CATEGORY_PACKAGE_SPECS: dict[str, dict[str, Any]] = {
    # 轮滑/暴走/旱冰：整双含轮架与鞋盒，1.6~2.0kg 属正常（负责人 2026-11-05 确认），
    # 不能按普通鞋类 1.2kg 卡住。
    "暴走": {"length": 35, "width": 24, "height": 16, "weightRange": (0.3, 2.0)},
    "轮滑": {"length": 35, "width": 24, "height": 16, "weightRange": (0.3, 2.0)},
    "旱冰": {"length": 35, "width": 24, "height": 16, "weightRange": (0.3, 2.0)},
    "鞋": {"length": 33, "width": 22, "height": 12, "weightRange": (0.2, 1.2)},
    "服饰": {"length": 30, "width": 24, "height": 6, "weightRange": (0.1, 1.0)},
    "包": {"length": 32, "width": 24, "height": 10, "weightRange": (0.1, 1.2)},
    "配件": {"length": 20, "width": 15, "height": 8, "weightRange": (0.05, 1.0)},
}
DEFAULT_PACKAGE_SPEC = {"length": 30, "width": 20, "height": 10, "weightRange": (0.05, 1.2)}

# 重量为空时按品类补的默认值（负责人同意：不用人工填，自动补一个合理重量）。
CATEGORY_DEFAULT_WEIGHT: dict[str, float] = {
    "暴走": 0.8,
    "轮滑": 0.8,
    "旱冰": 0.8,
    "鞋": 0.6,
    "服饰": 0.3,
    "包": 0.4,
    "配件": 0.2,
}
DEFAULT_WEIGHT_KG = 0.6


def category_default_weight(category_name: Any) -> float:
    """重量为空时按品类给的默认重量（kg）。认不出品类就用通用 0.6kg。"""
    name = str(category_name or "").strip()
    for keyword, weight in CATEGORY_DEFAULT_WEIGHT.items():
        if keyword in name:
            return weight
    for keyword in ("裤", "衣", "裙", "袜", "帽"):
        if keyword in name:
            return CATEGORY_DEFAULT_WEIGHT["服饰"]
    return DEFAULT_WEIGHT_KG


def category_min_weight(category_name) -> float:
    """Allowed lightest source weight (kg) for this category."""
    name = str(category_name or "").strip().casefold()
    for keyword, weight in CATEGORY_MIN_WEIGHT.items():
        if keyword.casefold() in name:
            return weight
    return WEIGHT_AUTO_MIN


def _weight_hard_max_for(category_name: Any = "", override: Any = None) -> float:
    """原重上限（kg）：优先用处理设置里配的值，没配才按品类。

    这个上限只比「上游原重」，重量增量不算在内 —— 增量是发给平台的报价参数，
    跟「采集重量是否填错」是两件事，混在一起会让正常重量被误拦
    （2026-11-09 负责人明确）。
    """
    if override not in (None, ""):
        try:
            value = float(override)
            if WEIGHT_LIMIT_RANGE[0] <= value <= WEIGHT_LIMIT_RANGE[1]:
                return value
        except (TypeError, ValueError):
            pass
    spec = category_package_spec(category_name)
    if spec:
        # 品类表里的值也统一抬到 1.5 下限：老表里普通鞋给的是 1.2，
        # 会把 1.27kg 的厚底老爹鞋这种正常货拦下来（负责人 2026-11-09 定）。
        return max(WEIGHT_HARD_MAX, float(spec["weightRange"][1]))
    return WEIGHT_HARD_MAX


def category_package_spec(category_name: Any) -> dict[str, Any] | None:
    """按品类名给出包裹规格（长宽高 + 合理重量区间）。

    类目名是中文自由文本（「成品鞋」「运动服饰」「瑜伽裤」…），按关键词归类；
    归不上就退到通用规格，避免把「认不出品类」当成异常一直挂着待核验。
    """
    name = str(category_name or "").strip()
    if not name:
        return dict(DEFAULT_PACKAGE_SPEC)
    for keyword, spec in CATEGORY_PACKAGE_SPECS.items():
        if keyword in name:
            return dict(spec)
    # 裤子/衣服类关键词兜底
    for keyword in ("裤", "衣", "裙", "袜", "帽"):
        if keyword in name:
            return dict(CATEGORY_PACKAGE_SPECS["服饰"])
    return dict(DEFAULT_PACKAGE_SPEC)


def account_names(db: Session) -> dict[int, str]:
    return {row.id: row.name for row in db.query(Account).all()}


def operator_names(db: Session) -> dict[int, str]:
    """operators.id → 显示名，供列表页一次性取名。

    一次查全表而不是每行 join：操作员就几个人，几十行；而列表页有 24~100 行
    商品，逐行查会把一次查询变成上百次。
    """
    from models.operator import Operator

    return {row.id: (row.name or "") for row in db.query(Operator).all()}


def _is_member(db: Session, operator_id: int | None) -> bool:
    if operator_id is None:
        return False
    from services import operator_service

    return operator_service.role_of(operator_service.get(db, operator_id)) == (
        operator_service.ROLE_MEMBER)


def _assert_member_account_access(db: Session, account_id: int | None,
                                  operator_id: int | None) -> None:
    if not _is_member(db, operator_id):
        return
    from services import account_service

    if not account_service.has_account_access(db, account_id, operator_id):
        raise errors.AppError(errors.FORBIDDEN,
                              "无权访问其他学员的仓库商品", status=403)


def _owned_account_ids(db: Session, operator_id: int):
    """Return accounts visible to one student, with legacy fallback scoped safely."""
    from services import account_service

    return [account_id for (account_id,) in db.query(Account.id).all()
            if account_service.has_account_access(db, account_id, operator_id)]


def _source_owned_product_ids(db: Session, operator_id: int):
    # A run's original owner is not sufficient authorization after its Miaoshou
    # account binding has been revoked. Keep only results from accounts this
    # member can access now.
    from services import account_service

    account_ids = [account_id for (account_id,) in db.query(Account.id).all()
                   if account_service.has_account_access(db, account_id, operator_id)]
    return (db.query(AutomationRunItem.product_id)
            .join(AutomationRun, AutomationRun.id == AutomationRunItem.run_id)
            .filter(AutomationRun.owner_operator_id == operator_id,
                    AutomationRun.account_id.in_(account_ids),
                    AutomationRunItem.product_id.is_not(None)))


def _assert_member_product_access(db: Session, product: Product,
                                  operator_id: int | None) -> None:
    if not _is_member(db, operator_id) or not product.automation_result:
        return
    from services import account_service

    if (product.account_id is None
            or not account_service.has_account_access(
                db, product.account_id, int(operator_id))):
        raise errors.AppError(errors.FORBIDDEN,
                              "无权访问其他学员的自动化处理结果", status=403)
    source_run = (db.query(AutomationRunItem.id)
                  .join(AutomationRun, AutomationRun.id == AutomationRunItem.run_id)
                  .filter(AutomationRunItem.product_id == product.id,
                          AutomationRun.owner_operator_id == operator_id)
                  .first())
    if source_run is None:
        raise errors.AppError(errors.FORBIDDEN,
                              "无权访问其他学员的自动化处理结果", status=403)


def _scope_member_products(db: Session, query, operator_id: int | None):
    if _is_member(db, operator_id):
        # automation_result=False 表示已进入共享成品仓库；此时不能按来源
        # account_id 过滤，否则老师入仓的成品会对其他学员完全消失。未入仓
        # 的自动化结果仅允许来源任务的学员看到。
        query = query.filter(or_(
            Product.automation_result.is_(False),
            Product.id.in_(_source_owned_product_ids(db, int(operator_id))),
        ))
    return query


def _claim_account_keys(db: Session, operator_id: int | None,
                        claim_account_id: Any = None) -> list[str] | None:
    member_view = _is_member(db, operator_id)
    if claim_account_id not in (None, ""):
        text = str(claim_account_id)
        if not text.isdigit():
            raise errors.validation("claimAccountId 须为数字", field="claimAccountId")
        account = db.get(Account, int(text))
        if account is None:
            raise errors.not_found("妙手账号不存在", accountId=text)
        _assert_member_account_access(db, account.id, operator_id)
        if member_view:
            return [f"operator:{operator_id}:account:{account.id}"]
        keys = [f"account:{account.id}"]
        if (account.name and
                db.query(Account.id).filter(Account.name == account.name).count() == 1):
            keys.append(account.name)
        return keys
    if member_view:
        owned_ids = _owned_account_ids(db, int(operator_id))
        # Accounts with no bindings is a legacy/unconnected context. Keep the
        # per-student ProductClaim state visible there; once an account is
        # selected, claim state is account-specific and comes only from a
        # confirmed collect-box import.
        if not owned_ids:
            return None
        return [f"operator:{operator_id}:account:{ident}" for ident in owned_ids]
    return None


def reconcile_member_claims(db: Session, *, operator_id: int,
                            account_id: Any) -> dict[str, int]:
    """Reconcile this member's saved claim markers with the live collect box.

    老师/管理员也会走到这里：前端在「仓库页加载」时会无条件核对一次，
    而角色刚被提升（member → teacher）的那一刻，浏览器里缓存的旧身份
    仍是 member，于是继续打这个接口。原先这里硬性要求 `_is_member`，
    老师一调就 403，界面弹出「无权访问该妙手账号，原有认领标记已保留」
    —— 用户看到的就是一个莫名其妙的报错，而且认领状态从此不再自动核对。

    正确语义：能不能访问这个妙手账号，取决于**账号绑定**，与角色无关。
    所以这里只校验归属；老师有绑定就照常核对。
    """
    from services import account_service, collect_box_service

    text = str(account_id or "")
    if not text.isdigit():
        raise errors.validation("accountId 须为数字", field="accountId")
    account = db.get(Account, int(text))
    if account is None:
        raise errors.not_found("妙手账号不存在", accountId=text)
    if not account_service.has_account_access(db, account.id, operator_id):
        raise errors.AppError(errors.FORBIDDEN, "无权访问该妙手账号", status=403)

    claim_key = f"operator:{operator_id}:account:{account.id}"
    products = _scope_member_products(
        db, active_query(db).filter(Product.automation_result.is_(False)), operator_id
    ).all()
    candidates: dict[tuple[str, str], list[Product]] = {}
    for product in products:
        collected = product.collected if isinstance(product.collected, dict) else {}
        marker = collected.get(claim_key)
        if not isinstance(marker, dict):
            continue
        detail_id = str(marker.get("detailId") or "")
        shop_id = str(marker.get("shopId") or "")
        if detail_id.isdigit() and shop_id.isdigit():
            candidates.setdefault((shop_id, detail_id), []).append(product)

    present: set[tuple[str, str]] = set()
    if candidates:
        from services.miaoshou import MiaoshouError

        client = account_service.account_client(account)
        try:
            for shop_id in {shop for shop, _ in candidates}:
                detail_ids = {detail for shop, detail in candidates if shop == shop_id}
                found = collect_box_service.verify_items_present(client, detail_ids, shop_id)
                present.update((shop_id, detail_id) for detail_id in found)
        except MiaoshouError as exc:
            raise account_service.map_upstream_error(exc) from None
        finally:
            client.close()

    cleared = 0
    for (shop_id, detail_id), rows in candidates.items():
        is_present = (shop_id, detail_id) in present
        for product in rows:
            collected = dict(product.collected or {})
            marker = collected.get(claim_key)
            if not isinstance(marker, dict):
                continue
            if is_present:
                claim_product(db, product.id, operator_id)
                continue
            collected.pop(claim_key, None)
            account_key = f"account:{account.id}"
            account_marker = collected.get(account_key)
            account_detail_id = str(account_marker.get("detailId") or "") if isinstance(
                account_marker, dict) else ""
            account_shop_id = str(account_marker.get("shopId") or "") if isinstance(
                account_marker, dict) else ""
            if account_detail_id == detail_id and account_shop_id == shop_id:
                collected.pop(account_key, None)
            product.collected = collected
            claim = db.query(ProductClaim).filter(
                ProductClaim.product_id == product.id,
                ProductClaim.owner_operator_id == operator_id,
            ).one_or_none()
            if claim is not None:
                claim.status = "released"
            bump_revision(product)
            cleared += 1

    return {"checked": sum(len(rows) for rows in candidates.values()),
            "present": sum(len(rows) for key, rows in candidates.items() if key in present),
            "cleared": cleared}


def _claim_expression(keys: list[str] | None):
    if keys is None:
        return None
    return or_(*(and_(Product.collected[key]["detailId"].as_string().is_not(None),
                      Product.collected[key]["detailId"].as_string() != "")
                 for key in keys)) if keys else false()


def _legacy_member_claim_expression(db: Session, operator_id: int):
    """Preserve legacy claims only for products not tied to an inaccessible account."""
    return db.query(ProductClaim.id).filter(
        ProductClaim.product_id == Product.id,
        ProductClaim.owner_operator_id == operator_id,
        Product.account_id.is_(None),
    ).exists()


def _claim_record(product: Product, keys: list[str] | None) -> dict[str, Any] | None:
    if keys is None:
        return None
    collected = product.collected if isinstance(product.collected, dict) else {}
    return next((value for key in keys if isinstance(value := collected.get(key), dict)
                 and value.get("detailId") not in (None, "")), {})


def _visible_collected(product: Product, keys: list[str] | None) -> dict[str, Any]:
    collected = product.collected if isinstance(product.collected, dict) else {}
    if keys is None:
        return collected
    return {key: collected[key] for key in keys if key in collected}


def _formal_publish_query(db: Session):
    # 认领日志可能由旧版写成 success，但它不是正式上架回执。
    marker = PublishLog.detail["claimOnly"].as_boolean()
    legacy_claim = db.query(Batch.id).filter(
        Batch.id == PublishLog.batch_id,
        Batch.settings["kind"].as_string() == "warehouse_claim",
    ).exists()
    return db.query(PublishLog).filter(~or_(
        marker.is_(True), and_(marker.is_(None), legacy_claim)))


def _latest_operator_publish_logs(db: Session, operator_id: int | None,
                                  product_ids: list[int],
                                  account_ids: list[int] | None = None
                                  ) -> dict[int, PublishLog]:
    if operator_id is None or not product_ids:
        return {}
    owned_accounts = (_owned_account_ids(db, operator_id)
                      if account_ids is None else account_ids)
    rows = (_formal_publish_query(db)
            .filter(PublishLog.operator_id == operator_id,
                    PublishLog.account_id.in_(owned_accounts),
                    PublishLog.product_id.in_(product_ids))
            .order_by(PublishLog.requested_at.desc(), PublishLog.id.desc()).all())
    latest: dict[int, PublishLog] = {}
    for row in rows:
        if row.product_id is not None:
            latest.setdefault(row.product_id, row)
    return latest


def _successful_operator_publish_shops(
    db: Session, operator_id: int | None, product_ids: list[int],
    account_ids: list[int] | None = None,
) -> dict[int, list[str]]:
    if operator_id is None or not product_ids:
        return {}
    owned_accounts = (_owned_account_ids(db, operator_id)
                      if account_ids is None else account_ids)
    rows = (_formal_publish_query(db).with_entities(PublishLog.product_id, PublishLog.shop_id)
            .filter(PublishLog.operator_id == operator_id,
                    PublishLog.account_id.in_(owned_accounts),
                    PublishLog.product_id.in_(product_ids),
                    PublishLog.status == "success").all())
    shops: dict[int, set[str]] = {}
    for product_id, shop_id in rows:
        if product_id is not None:
            published_shops = shops.setdefault(product_id, set())
            if shop_id:
                published_shops.add(str(shop_id))
    return {product_id: sorted(shop_ids) for product_id, shop_ids in shops.items()}


def active_query(db: Session):
    return db.query(Product).filter(Product.deleted_at.is_(None))


def resolve_product(db: Session, ident: Any, *, account_id: int | None = None) -> Product:
    """按数字 id 或 `{detailId}:{shopId}` 取商品；软删除视为不存在。

    Numeric warehouse ids are globally unique. A legacy ``detailId:shopId``
    key can now occur in multiple Miaoshou accounts, so callers with account
    context should pass ``account_id``. Older key-only callers receive a clear
    409 instead of an ORM ``MultipleResultsFound`` 500.
    """
    text = str(ident or "").strip()
    if not text:
        raise errors.validation("商品标识不能为空", field="id")
    query = active_query(db)
    if text.isdigit():
        product = query.filter(Product.id == int(text)).one_or_none()
    else:
        key_query = query.filter(Product.warehouse_key == text)
        if account_id is not None:
            product = key_query.filter(Product.account_id == account_id).one_or_none()
        else:
            matches = key_query.limit(2).all()
            if len(matches) > 1:
                raise errors.AppError(
                    errors.AMBIGUOUS_PRODUCT,
                    "商品键在多个妙手账号下重复，请使用仓库商品 ID",
                    status=409,
                    details={"warehouseKey": text, "hint": "useWarehouseId"},
                )
            product = matches[0] if matches else None
    if product is None:
        raise errors.not_found(f"仓库里没有商品 {text}", id=text)
    return product


# --- 列表 -----------------------------------------------------------------
def list_products(db: Session, *, page: int = 1, page_size: int = 24,
                  category_id: Any = None, cid: Any = None, group_id: Any = None,
                  group: str | None = None, q: str | None = None,
                  account_id: Any = None, status: str | None = None,
                  quality_status: str | None = None,
                  claimed: bool | None = None, sort: str = "savedDesc",
                  operator_id: int | None = None,
                  claim_account_id: Any = None,
                  ) -> dict[str, Any]:
    if page < 1:
        raise errors.validation("页码必须不小于 1", field="page", value=page)
    if not 1 <= page_size <= 1000:
        raise errors.validation("每页数量必须在 1 到 1000 之间", field="pageSize", value=page_size)
    if sort not in SORTS:
        raise errors.validation("排序方式无效", field="sort", value=sort)
    if status is not None and status not in STATUSES:
        raise errors.validation("状态筛选无效", field="status", value=status)
    if quality_status not in (None, "verified", "needs_review"):
        raise errors.validation("规格核验状态无效", field="qualityStatus", value=quality_status)
    if q is not None and len(str(q)) > 64:
        raise errors.validation("搜索词不能超过 64 个字符", field="q")

    query = active_query(db).filter(Product.automation_result.is_(False))
    reviewed_revision = Product.quality_review["verifiedRevision"].as_integer()
    if quality_status == "verified":
        query = query.filter(Product.quality_review.is_not(None),
                             reviewed_revision == Product.revision)
    elif quality_status == "needs_review":
        query = query.filter(or_(Product.quality_review.is_(None),
                                 reviewed_revision.is_(None),
                                 reviewed_revision != Product.revision))
    member_view = _is_member(db, operator_id)
    account_claim_keys = _claim_account_keys(db, operator_id, claim_account_id)
    account_claim_expr = _claim_expression(account_claim_keys)
    owned_accounts = (([int(claim_account_id)] if claim_account_id not in (None, "") else
                       _owned_account_ids(db, int(operator_id))) if member_view else [])
    # 学员也能看到成品仓库的全部商品（负责人口径 2026-11-12：
    # 「学员也能看到成品仓库的全部东西，仓库就是给多个人导入采集箱用的」）。
    #
    # 原来这里对学员做账号归属校验（_assert_member_account_access），
    # 学员只能看自己绑定账号的商品，于是老师用别的账号入的 70 多件成品
    # 对学员完全不可见 —— 但成品仓库本来就是共享货源池，
    # 学员的用途是「从这里挑货导入自己的采集箱」，看不到就挑不了。
    #
    # 账号参数仍然接受（用于「认领状态」这类按账号区分的展示），
    # 但不再作为可见性门槛。真正的权限边界是 _scope_member_products：
    # 学员看得到所有成品（automation_result=False），
    # 未入仓的自动化结果仍只对来源学员可见。
    if member_view and account_id not in (None, ""):
        text = str(account_id)
        if not text.isdigit():
            raise errors.validation("accountId 须为数字", field="accountId")
    query = _scope_member_products(db, query, operator_id)
    if category_id not in (None, ""):
        cid_value = str(category_id)
        if cid_value.isdigit():
            query = query.filter(or_(Product.category_id == int(cid_value),
                                     Product.cid == cid_value))
        else:
            query = query.filter(Product.cid == cid_value)
    if cid not in (None, ""):
        query = query.filter(Product.cid == str(cid))
    if group_id not in (None, ""):
        text = str(group_id)
        if text.isdigit():
            query = query.filter(Product.group_id == int(text))
        else:
            query = query.filter(Product.group_name == text)
    if group:
        query = query.filter(Product.group_name == str(group))
    # 成品仓库列表**不按妙手账号过滤**（负责人口径 2026-11-12：
    # 「从自动化结果把鞋加入成品仓库之后怎么只显示这个了其他的都不显示了」）。
    #
    # 顶部账号下拉只是「用哪个账号的妙手接口」的选择器，不是仓库的数据边界 ——
    # 仓库里同时躺着罗蔓 / 蒋新宇 / 赖彩红三个账号入库的商品，
    # 切个账号就把其他账号的 71 件藏起来，运营会以为商品丢了。
    # 分组计数（facets）本来就不按账号过滤，两边不一致也会让人困惑。
    # 真需要按账号看的时候，搜索框里输入账号名对应的商品即可。
    # （member_view 学员视角的隔离逻辑不受影响 —— 那是权限边界，保留。）
    if status:
        if member_view and status == "published":
            own_success = _formal_publish_query(db).filter(
                PublishLog.product_id == Product.id,
                PublishLog.operator_id == operator_id,
                PublishLog.account_id.in_(owned_accounts),
                PublishLog.status == "success",
            ).exists()
            query = query.filter(own_success)
        elif not member_view and account_claim_expr is not None and status == "claimed":
            query = query.filter(account_claim_expr)
        elif not member_view and account_claim_expr is not None and status == "ready":
            query = query.filter(~account_claim_expr, Product.status != "archived")
        elif member_view and status == "claimed":
            own_success = _formal_publish_query(db).filter(
                PublishLog.product_id == Product.id,
                PublishLog.operator_id == operator_id,
                PublishLog.account_id.in_(owned_accounts),
                PublishLog.status == "success",
            ).exists()
            query = query.filter(account_claim_expr, ~own_success)
        elif member_view and status == "ready":
            own_success = _formal_publish_query(db).filter(
                PublishLog.product_id == Product.id,
                PublishLog.operator_id == operator_id,
                PublishLog.account_id.in_(owned_accounts),
                PublishLog.status == "success",
            ).exists()
            query = query.filter(~own_success, ~account_claim_expr,
                                 Product.status != "archived")
        else:
            query = query.filter(Product.status == status)
    if claimed is not None:
        if account_claim_expr is not None:
            query = query.filter(account_claim_expr if claimed else ~account_claim_expr)
        elif operator_id is None:
            query = query.filter(Product.claimed.is_(bool(claimed)))
        else:
            owned_claim = (_legacy_member_claim_expression(db, operator_id)
                           if member_view else db.query(ProductClaim.id).filter(
                               ProductClaim.product_id == Product.id,
                               ProductClaim.owner_operator_id == operator_id,
                           ).exists())
            query = query.filter(owned_claim if claimed else ~owned_claim)
    needle = str(q or "").strip()
    if needle:
        pattern = f"%{needle}%"
        query = query.filter(or_(Product.title.ilike(pattern),
                                 Product.detail_id.like(pattern),
                                 Product.category_name.ilike(pattern)))

    total = query.count()
    rows = (query.order_by(*SORTS[sort])
            .offset((page - 1) * page_size).limit(page_size).all())
    names = account_names(db)
    operators = operator_names(db)
    claims = {}
    if operator_id is not None and rows:
        claim_query = db.query(ProductClaim).filter(
            ProductClaim.owner_operator_id == operator_id,
            ProductClaim.product_id.in_([item.id for item in rows]),
        )
        if member_view and account_claim_keys is None:
            legacy_product_ids = [item.id for item in rows if item.account_id is None]
            claim_query = claim_query.filter(
                ProductClaim.product_id.in_(legacy_product_ids or [-1]))
        claims = {row.product_id: row for row in claim_query.all()}
    publish_logs = (_latest_operator_publish_logs(
        db, operator_id, [item.id for item in rows], owned_accounts) if member_view else {})
    successful_publish_shops = (_successful_operator_publish_shops(
        db, operator_id, [item.id for item in rows], owned_accounts) if member_view else {})
    items = [serialize(row, account_name=names.get(row.account_id or 0, ""),
                       operator_name=operators.get(row.operator_id or 0, ""),
                       claim=claims.get(row.id), operator_id=operator_id,
                       member_view=member_view, publish_log=publish_logs.get(row.id),
                       account_claim=_claim_record(row, account_claim_keys),
                       collected=_visible_collected(row, account_claim_keys),
                       published_shop_ids=successful_publish_shops.get(row.id, []),
                       has_successful_publish=row.id in successful_publish_shops)
             for row in rows]
    # stats 按当前列表的 account_id 过滤：多账号共用时，
    # 「已认领」显示的是当前账号的数字，不是全账号加总。
    stats_account_id: int | None = None
    if account_id not in (None, ""):
        try:
            stats_account_id = int(str(account_id))
        except (TypeError, ValueError):
            stats_account_id = None
    return {
        "items": items,
        "page": page,
        "pageSize": page_size,
        "total": total,
        "hasMore": page * page_size < total,
        "facets": facets(db, operator_id=operator_id),
        "stats": stats(db, account_id=stats_account_id, operator_id=operator_id,
                       account_claim_expr=account_claim_expr,
                       claim_account_id=claim_account_id),
    }


def _label_key(label: str) -> int:
    """分组名 → 稳定的伪 id（不能用内置 hash：PYTHONHASHSEED 会让跨进程不一致）。"""
    return -abs(zlib.crc32(str(label or "").encode("utf-8"))) % 100000


def _merge_same_name(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同名只出一条；count 相加。

    只有「涉及自定义标签（cid 为空）」的同名才合并。两个**真实平台类目**用同一个
    中文名是上游常态（不同 cid、同一中文），把它们并成一条会让其中一半商品失去
    按 cid 筛选的能力——那是比面板难看严重得多的损失，所以照旧各出一条。
    """
    order: list[str] = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        name = str(entry.get("name") or "").strip()
        if name not in grouped:
            grouped[name] = []
            order.append(name)
        grouped[name].append(entry)

    merged: list[dict[str, Any]] = []
    for name in order:
        items = grouped[name]
        if len(items) == 1:
            merged.append(dict(items[0], name=name))
            continue
        tagged = [item for item in items if not item.get("cid")]
        if not tagged:
            # 全是带 cid 的真类目：保留每一条，筛选才准
            merged.extend(dict(item, name=name) for item in items)
            continue
        head = dict(items[0])
        head["name"] = name
        head["count"] = sum(int(item.get("count") or 0) for item in items)
        # cid 取真类目那条的：比自定义标签的空 cid 有信息量
        for item in items:
            if item.get("cid"):
                head["cid"] = item["cid"]
                head["categoryId"] = item.get("categoryId")
                break
        merged.append(head)
    return merged


# --- 自定义分类标签（老版 warehouse.custom_categories）---------------------
# 老版把标签存成 warehouse-categories.json 里的名字数组。新版不引入运行时
# 文件：设置表本来就是「键 + 任意 JSON」，备份/迁移跟着 app.db 一起走。
# 上限 200 与仓库批量操作的上限同量级，防止面板被几千个标签撑爆。
CUSTOM_CATEGORY_MAX = 200
CUSTOM_CATEGORY_NAME_MAX = 30


def custom_categories(db: Session) -> list[dict[str, Any]]:
    """运营自定义的分类标签，独立于妙手 cid。

    读不到、读坏了（不是数组 / 数组里混了非字符串）一律当空：设置是用户可手改
    的，读坏就报错会让整个仓库面板打不开。
    """
    raw = setting_service.get(db, CUSTOM_CATEGORIES_KEY, None)
    if not isinstance(raw, list):
        return []
    names: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        name = item.strip()
        if name and name not in names:
            names.append(name)
    return [{"name": name, "cid": "", "count": 0} for name in names]


def _save_custom_categories(db: Session, names: list[str]) -> list[dict[str, Any]]:
    setting_service.put(db, CUSTOM_CATEGORIES_KEY, list(names))
    return custom_categories(db)


def add_custom_category(db: Session, name: Any) -> list[dict[str, Any]]:
    """加一个标签；同名不重复加（幂等），返回完整列表。

    幂等是给界面连点两下用的：第二次点不该报「已存在」，也不该出现两条。
    """
    text = str(name or "").strip()[:CUSTOM_CATEGORY_NAME_MAX]
    if not text:
        raise errors.validation("分类名不能为空", field="name")
    names = [item["name"] for item in custom_categories(db)]
    if text in names:
        return custom_categories(db)
    if len(names) >= CUSTOM_CATEGORY_MAX:
        raise errors.validation(f"自定义分类最多 {CUSTOM_CATEGORY_MAX} 个",
                                field="name", value=len(names))
    names.append(text)
    return _save_custom_categories(db, names)


def remove_custom_category(db: Session, name: Any) -> list[dict[str, Any]]:
    """删一个标签；不存在不报错（幂等，前端重复点删除不会被 404 打断）。"""
    text = str(name or "").strip()
    names = [item["name"] for item in custom_categories(db)]
    # 前缀截断过的名字也可能被传进来：先按原样匹配，再按截断后的形式匹配
    target = text[:CUSTOM_CATEGORY_NAME_MAX]
    remaining = [item for item in names if item != text and item != target]
    if len(remaining) == len(names):
        return custom_categories(db)
    return _save_custom_categories(db, remaining)


def rename_custom_category(db: Session, old: Any, new: Any) -> list[dict[str, Any]]:
    """改名：改名而不是「删旧加新」，商品行上的 categoryName 才能跟着对上。

    改成一个已存在的名字时按「合并」处理（旧名消失），否则列表里会出现
    两条同名标签，facets 还得再合一次。
    """
    old_text = str(old or "").strip()
    old_key = old_text[:CUSTOM_CATEGORY_NAME_MAX]
    new_text = str(new or "").strip()[:CUSTOM_CATEGORY_NAME_MAX]
    if not new_text:
        raise errors.validation("分类名不能为空", field="name")
    names = [item["name"] for item in custom_categories(db)]
    if old_key not in names:
        raise errors.not_found(f"自定义分类「{old_text}」不存在", name=old_text)
    if old_key == new_text:
        return custom_categories(db)
    updated: list[str] = []
    for name in names:
        if name == old_key:
            if new_text not in updated:
                updated.append(new_text)
            continue
        if name not in updated:
            updated.append(name)
    return _save_custom_categories(db, updated)


def facets(db: Session, *, operator_id: int | None = None) -> dict[str, Any]:
    """筛选面板计数：分组优先，未分组的商品才计入平台类目。

    运营自定义标签（`warehouse.customCategories`）也是类目维度的一条：商品的
    `categoryName` 等于某个自定义标签时计入该标签桶，`cid` 留空——自定义标签
    本来就没有平台 cid，塞个假 cid 会让前端拿去调类目接口。
    """
    visible = _scope_member_products(
        db, active_query(db).filter(Product.automation_result.is_(False)), operator_id)
    rows = (visible
            .with_entities(Product.group_id, Product.group_name, Product.cid,
                           Product.category_id, Product.category_name)
            .all())
    # 「全部」chip 的数字：facets 本来就按当前身份的全量算，与筛选条件无关，
    # 直接复用 rows 的长度，不必再查一次库。
    overall = len(rows)
    custom = custom_categories(db)
    custom_names = {item["name"] for item in custom}
    groups = {row.id: row for row in db.query(Group).all()}
    category_counts: dict[str, dict[str, Any]] = {}
    group_counts: dict[int, int] = {}
    uncategorized = 0
    for group_id, group_name, cid, category_id, category_name in rows:
        label = (group_name or (groups.get(group_id).name if group_id in groups else "")).strip()
        if group_id or label:
            key = int(group_id) if group_id else _label_key(label)
            group_counts[key] = group_counts.get(key, 0) + 1
            continue
        if not cid and not category_name and not category_id:
            uncategorized += 1
            continue
        plain = str(category_name or "").strip()
        if plain and plain in custom_names:
            # 独立桶：同名平台类目（带 cid）与自定义标签（无 cid）不能混成一条，
            # 否则面板上那一行的 cid 是真是假说不清，点下去筛出来的东西也不对。
            bucket = category_counts.setdefault(f"custom:{plain}", {
                "name": plain,
                "cid": "",
                "categoryId": None,
                "count": 0,
            })
            bucket["count"] += 1
            continue
        bucket_key = str(cid or category_id or category_name)
        bucket = category_counts.setdefault(bucket_key, {
            "name": category_name or (f"类目 {cid}" if cid else "未分类"),
            "cid": cid or "",
            "categoryId": str(category_id) if category_id else None,
            "count": 0,
        })
        bucket["count"] += 1

    # 一个商品都没有的自定义标签也要出现在面板上：标签是运营先建好、
    # 之后再往上面挂商品的，建完就消失会让人以为没保存成功。
    for item in custom:
        category_counts.setdefault(f"custom:{item['name']}", {
            "name": item["name"],
            "cid": "",
            "categoryId": None,
            "count": 0,
        })

    category_list = _merge_same_name(list(category_counts.values()))
    category_list.sort(key=lambda item: (-item["count"], item["name"]))
    if uncategorized:
        category_list.append({"name": "未分类", "cid": "", "categoryId": None,
                              "count": uncategorized})

    group_list: list[dict[str, Any]] = []
    for row in sorted(groups.values(), key=lambda item: (item.sort, item.name)):
        group_list.append({
            "id": str(row.id),
            "name": row.name,
            "color": row.color or "slate",
            "count": group_counts.get(row.id, 0),
        })
    # 有分组名但分组行已被删除的商品，仍要在面板里能筛到
    known = {row.name for row in groups.values()}
    for (_group_id, group_name, _cid, _cat_id, _cat_name) in rows:
        name = str(group_name or "").strip()
        if name and name not in known:
            known.add(name)
            group_list.append({"id": None, "name": name, "color": "slate",
                               "count": group_counts.get(_label_key(name), 0)})

    account_counts: dict[int | None, int] = {}
    for (acc_id,) in visible.with_entities(Product.account_id).all():
        account_counts[acc_id] = account_counts.get(acc_id, 0) + 1
    names = account_names(db)
    account_list = [] if _is_member(db, operator_id) else [
        {"accountId": str(acc_id) if acc_id else None,
         "name": names.get(acc_id or 0, "未标注"),
         "count": count}
        for acc_id, count in sorted(account_counts.items(), key=lambda item: -item[1])
    ]
    return {"categories": category_list, "groups": group_list, "accounts": account_list,
            "total": overall}


def stats(db: Session, *, account_id: int | None = None,
          operator_id: int | None = None, account_claim_expr: Any = None,
          claim_account_id: Any = None) -> dict[str, Any]:
    """仓库统计。传 account_id 时只数当前账号的商品——
    多账号共用一台机器时，「已认领 355 件」是全账号加总，用户分不清哪些是自己的。"""
    base = active_query(db).filter(Product.automation_result.is_(False))
    member_view = _is_member(db, operator_id)
    if member_view:
        if account_id is not None:
            _assert_member_account_access(db, account_id, operator_id)
        base = _scope_member_products(db, base, operator_id)
        if account_claim_expr is None:
            account_claim_expr = _claim_expression(
                _claim_account_keys(db, operator_id, claim_account_id))
    # 对学员来说 account_id 只是当前发布账号的权限校验；共享成品的
    # 数量、容量和可认领目录不应因来源账号不同而被切掉。
    if account_id is not None and not member_view:
        base = base.filter(Product.account_id == account_id)
    total = base.count()
    return {
        "totalProducts": total,
        "totalClaimed": (
            base.filter(account_claim_expr).count() if account_claim_expr is not None else
            base.filter(Product.claimed.is_(True)).count() if operator_id is None else
            base.filter(_legacy_member_claim_expression(db, operator_id)
                        if member_view else db.query(ProductClaim.id).filter(
                            ProductClaim.product_id == Product.id,
                            ProductClaim.owner_operator_id == operator_id,
                        ).exists()).count()
        ),
        "totalPublished": (
            base.filter(_formal_publish_query(db).filter(
                PublishLog.product_id == Product.id,
                PublishLog.operator_id == operator_id,
                PublishLog.account_id.in_([int(claim_account_id)]
                    if claim_account_id not in (None, "") else
                    _owned_account_ids(db, operator_id)),
                PublishLog.status == "success",
            ).exists()).count() if member_view else
            base.filter(Product.status == "published").count()
        ),
        # 容量信息一起给出，界面才能在快满时提示。
        # 这是 `trim_overflow` 的替代方案，理由见那个函数的 docstring。
        "maxProducts": WAREHOUSE_MAX_ITEMS,
        "remaining": max(0, WAREHOUSE_MAX_ITEMS - total),
        "nearLimit": total >= WAREHOUSE_MAX_ITEMS * NEAR_LIMIT_RATIO,
    }


def _verified_automation_product_ids(db: Session):
    return (db.query(AutomationRunItem.product_id)
            .join(AutomationRun, AutomationRun.id == AutomationRunItem.run_id)
            .join(Product, Product.id == AutomationRunItem.product_id)
            .filter(
                AutomationRunItem.process_status == "succeeded",
                Product.draft_verified_revision == Product.revision,
                ~db.query(ImageJob.id).filter(
                    ImageJob.product_id == Product.id,
                    ImageJob.status != "ready",
                ).exists(),
                AutomationRun.account_id == Product.account_id,
                AutomationRunItem.detail_id == Product.detail_id,
                AutomationRunItem.source_shop_id == Product.shop_id,
            ))


def list_automation_results(db: Session, *, page: int = 1, page_size: int = 24
                            ) -> dict[str, Any]:
    if page < 1 or not 1 <= page_size <= 100:
        raise errors.validation("分页参数无效", field="page")
    query = (active_query(db).filter(Product.automation_result.is_(True))
             .filter(Product.id.in_(_verified_automation_product_ids(db))))
    total = query.count()
    rows = (query.order_by(Product.saved_at.desc(), Product.id.desc())
            .offset((page - 1) * page_size).limit(page_size).all())
    names = account_names(db)
    operators = operator_names(db)
    return {
        "items": [serialize(row, account_name=names.get(row.account_id or 0, ""),
                            operator_name=operators.get(row.operator_id or 0, ""))
                  for row in rows],
        "page": page,
        "pageSize": page_size,
        "total": total,
        "hasMore": page * page_size < total,
    }


def admit_automation_results(db: Session, ids: Iterable[Any], *,
                             group_id: Any = None) -> dict[str, Any]:
    """把自动化结果加入成品仓库，可同时指定入库分组（分类）。

    group_id 是成品仓库左侧的分组（负责人 2026-11-11 要求「入库可以选择
    当前仓库的分类入哪个分类」）。传 None 表示不分组。
    """
    wanted = list(dict.fromkeys(str(value).strip() for value in ids if str(value).strip()))
    if not wanted or len(wanted) > 100:
        raise errors.validation("请选择 1 到 100 件自动化处理结果", field="ids")
    products = [resolve_product(db, ident) for ident in wanted]
    verified_items = {
        row[0] for row in _verified_automation_product_ids(db)
        .filter(AutomationRunItem.product_id.in_([product.id for product in products]))
        .all()
    }
    invalid = [product.id for product in products
               if not product.automation_result or product.id not in verified_items]
    if invalid:
        raise errors.validation(
            "部分商品没有对应的成功自动化处理记录，不能加入成品仓库",
            field="ids",
                                value=[str(value) for value in invalid])
    available = stats(db)["remaining"]
    if len(products) > available:
        raise errors.AppError("WAREHOUSE_FULL", "成品仓库容量不足",
                              status=409, details={"remaining": available})
    group = None
    if group_id not in (None, ""):
        try:
            gid = int(str(group_id).strip())
        except (TypeError, ValueError):
            raise errors.validation("分组 id 必须是数字", field="groupId") from None
        group = db.get(Group, gid)
        if group is None:
            raise errors.validation("分组不存在", field="groupId")
    for product in products:
        product.automation_result = False
        if group is not None:
            product.group_id = group.id
            product.group_name = getattr(group, "name", "") or ""
    db.flush()
    return {"added": len(products), "ids": [str(product.id) for product in products],
            "groupId": group.id if group is not None else None}


# --- 详情 -----------------------------------------------------------------


def delete_automation_results(db: Session, ids: Iterable[Any]) -> dict[str, Any]:
    """从「自动化结果」列表删除记录。

    负责人口径 2026-11-12：「这里怎么没有删除」。
    语义是「这件不要了，从队列清掉」：
      · 删 AutomationRunItem + 关联 ImageJob（这些只是执行痕迹）；
      · 对应 Product（automation_result=True 的仓库行）一并软删除 ——
        它还没入成品仓库，留着也没意义；
      · **不动妙手草稿**（草稿还在采集箱，不影响再次处理）。
    """
    wanted = list(dict.fromkeys(str(v).strip() for v in ids if str(v).strip()))
    if not wanted or len(wanted) > 100:
        raise errors.validation("请选择 1 到 100 件自动化结果", field="ids")
    products = [resolve_product(db, ident) for ident in wanted]
    invalid = [str(p.id) for p in products if not p.automation_result]
    if invalid:
        raise errors.validation(
            "部分商品已加入成品仓库，请到商品列表里删除",
            field="ids", value=invalid)
    from models.image_job import ImageJob
    product_ids = [p.id for p in products]
    removed_jobs = (db.query(ImageJob)
                    .filter(ImageJob.product_id.in_(product_ids))
                    .delete(synchronize_session=False))
    removed_items = (db.query(AutomationRunItem)
                     .filter(AutomationRunItem.product_id.in_(product_ids))
                     .delete(synchronize_session=False))
    now = now_ts()
    for p in products:
        p.deleted_at = now
    db.flush()
    return {"deleted": len(products), "items": int(removed_items or 0),
            "jobs": int(removed_jobs or 0)}

def detail(db: Session, ident: Any, *, include_payload: bool = False,
           operator_id: int | None = None, claim_account_id: Any = None) -> dict[str, Any]:
    product = resolve_product(db, ident)
    _assert_member_product_access(db, product, operator_id)
    names = account_names(db)
    operators = operator_names(db)
    member_view = _is_member(db, operator_id)
    account_claim_keys = _claim_account_keys(db, operator_id, claim_account_id)
    owned_accounts = (([int(claim_account_id)] if claim_account_id not in (None, "") else
                       _owned_account_ids(db, int(operator_id))) if member_view else [])
    claim = None
    if operator_id is not None and (not member_view or account_claim_keys is None):
        claim_query = db.query(ProductClaim).filter(
            ProductClaim.product_id == product.id,
            ProductClaim.owner_operator_id == operator_id,
        )
        if member_view and account_claim_keys is None and product.account_id is not None:
            claim_query = None
        claim = claim_query.one_or_none() if claim_query is not None else None
    own_publish_log = (_latest_operator_publish_logs(
        db, operator_id, [product.id], owned_accounts).get(product.id) if member_view else None)
    successful_publish_shops = (_successful_operator_publish_shops(
        db, operator_id, [product.id], owned_accounts) if member_view else {})
    body = serialize(product, account_name=names.get(product.account_id or 0, ""),
                     include_payload=include_payload,
                     operator_name=operators.get(product.operator_id or 0, ""),
                     claim=claim, operator_id=operator_id,
                     member_view=member_view, publish_log=own_publish_log,
                      account_claim=_claim_record(product, account_claim_keys),
                      collected=_visible_collected(product, account_claim_keys),
                     published_shop_ids=successful_publish_shops.get(product.id, []),
                     has_successful_publish=product.id in successful_publish_shops)
    history_query = _formal_publish_query(db).filter(PublishLog.product_id == product.id)
    if member_view:
        history_query = history_query.filter(
            PublishLog.operator_id == operator_id,
            PublishLog.account_id.in_(owned_accounts),
        )
    history = history_query.order_by(
        PublishLog.requested_at.desc()).limit(20).all()
    jobs_query = db.query(ImageJob).filter(ImageJob.product_id == product.id)
    if member_view:
        jobs_query = jobs_query.filter(ImageJob.operator_id == operator_id)
    jobs = jobs_query.order_by(ImageJob.created_at.desc()).limit(20).all()
    return {
        "product": body,
        "publishHistory": [{
            "logId": str(row.id),
            "shopId": row.shop_id,
            "shopName": row.shop_name,
            "status": row.status,
            "reason": row.reason,
            "mode": row.mode,
            "requestedAt": row.requested_at,
            "finishedAt": row.finished_at,
            "platformItemId": row.platform_item_id,
        } for row in history],
        "imageJobs": [{
            "id": row.id,
            "source": row.source,
            "status": row.status,
            "reservedMilliyuan": int(row.reserved_milliyuan or 0),
            "filename": row.filename or None,
            "createdAt": row.created_at,
        } for row in jobs],
    }


def claim_product(db: Session, ident: Any, owner_operator_id: int) -> ProductClaim:
    product = resolve_product(db, ident)
    _assert_member_product_access(db, product, owner_operator_id)
    if product.automation_result:
        raise errors.validation("自动化结果加入成品仓库后才能认领", field="id")
    existing = db.query(ProductClaim).filter(
        ProductClaim.product_id == product.id,
        ProductClaim.owner_operator_id == owner_operator_id,
    ).one_or_none()
    if existing is not None:
        existing.status = "claimed"
        existing.claimed_at = now_ts()
        return existing

    row = ProductClaim(product_id=product.id, owner_operator_id=owner_operator_id)
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
        return row
    except IntegrityError:
        existing = db.query(ProductClaim).filter(
            ProductClaim.product_id == product.id,
            ProductClaim.owner_operator_id == owner_operator_id,
        ).one_or_none()
        if existing is None:
            raise
        return existing


# --- 写 -------------------------------------------------------------------
def apply_category(db: Session, product: Product, *, category_id: Any = None,
                   cid: Any = None, name: str | None = None,
                   set_category: bool = False, site: str = "") -> None:
    """改平台类目：cid 优先并回填 category_id；category_id=None 表示清空。"""
    if cid not in (None, ""):
        cid_text = str(cid).strip()
        row = category_service.find_by_cid(db, cid_text, site=site,
                                          account_id=product.account_id)
        if row is not None and not row.is_leaf:
            raise errors.AppError(errors.CATEGORY_NOT_LEAF,
                                  f"类目「{row.name_cn or row.name}」不是叶子类目，不能保存商品",
                                  status=422, details={"cid": row.cid, "path": row.path})
        product.cid = cid_text
        product.category_id = row.id if row is not None else None
        product.category_name = (str(name).strip() if name else
                                 (category_service.label_of(db, cid_text, site=site,
                                                            account_id=product.account_id)
                                  or f"类目 {cid_text}"))
        payload = product.payload if isinstance(product.payload, dict) else {}
        payload = dict(payload)
        payload["cid"] = cid_text
        product.payload = payload
        return
    if category_id not in (None, ""):
        text = str(category_id)
        if not text.isdigit():
            raise errors.validation("categoryId 必须是数字", field="categoryId", value=category_id)
        row = db.query(Category).filter(Category.id == int(text)).one_or_none()
        if row is None:
            raise errors.not_found(f"类目 {category_id} 不存在", categoryId=text)
        if not row.is_leaf:
            raise errors.AppError(errors.CATEGORY_NOT_LEAF,
                                  f"类目「{row.name_cn or row.name}」不是叶子类目，不能保存商品",
                                  status=422, details={"cid": row.cid, "path": row.path})
        product.category_id = row.id
        product.cid = row.cid
        product.category_name = str(name).strip() if name else (row.name_cn or row.name)
        payload = dict(product.payload or {})
        payload["cid"] = row.cid
        product.payload = payload
        return
    if set_category:
        product.category_id = None
        product.cid = ""
        product.category_name = ""


def apply_group(db: Session, product: Product, *, group_id: Any = None,
                set_group: bool = False) -> None:
    if set_group and group_id in (None, ""):
        product.group_id = None
        product.group_name = ""
        return
    if group_id in (None, ""):
        return
    text = str(group_id)
    if not text.isdigit():
        raise errors.validation("groupId 必须是数字", field="groupId", value=group_id)
    row = db.query(Group).filter(Group.id == int(text)).one_or_none()
    if row is None:
        raise errors.not_found(f"分组 {group_id} 不存在", groupId=text)
    product.group_id = row.id
    product.group_name = row.name


def bump_revision(product: Product) -> int:
    """写库前把版本号 +1（没有调用方版本号可比的路径用它）。

    所有会改商品行的路径都必须推进版本号：漏一处，那一处就成了「改了也不换
    版本」，别人手上的旧版本号还能继续通过校验 —— 防护直接破一个洞。

    只有拿不到 `expected_revision` 的写入方才用它（批量改、删除、采集箱落库、
    upsert、标题保存、改图）。能拿到版本号的路径用 `revision_guard`，
    它做的是**原子**条件更新，比这里的「内存 +1」强一个量级。
    """
    product.revision = int(product.revision or 0) + 1
    return product.revision


def _conflict(db: Session, product: Product, expected: int, actual: int,
              action: str) -> errors.AppError:
    """构造 409：把「谁改的、该怎么办」写进 hint。

    冲突信息里带上「上一次是谁改的」，是因为这条消息唯一的用处就是让运营知道
    该找谁对一下；只说「版本不对」等于让两个人互相猜。
    """
    last_editor = operator_names(db).get(product.operator_id or 0) or "另一个人"
    hint = f"这件商品在你{action}期间被 {last_editor} 改过，请刷新后重试"
    logger.info("版本冲突 product=%s expected=%s actual=%s by=%s",
                product.id, expected, actual, last_editor)
    return errors.revision_conflict(
        f"商品已被他人修改（当前版本 {actual}，你手上是 {expected}）",
        expected=expected, actual=actual, hint=hint)


def revision_guard(db: Session, product: Product, expected_revision: int | None,
                   *, action: str = "修改") -> None:
    """比对版本号，并在通过时**原子地**把它推到下一版。

    **`expected_revision=None` 表示「调用方没带版本号，这次不校验」**。
    这是刻意的向后兼容：老的调用方（脚本、探针、还没升级的前端）不带这个参数，
    不能因为它们不带就全部 400。代价必须说清楚 —— 不传 = 主动放弃这层保护，
    两个人同时改仍然会静默覆盖。前端每一次 PATCH 都应该带上它。
    这条路径仍然推进版本号（`bump_revision`），否则不传参数的写入方就能把
    别人手上的版本号「冻结」住，下一次他带版本号来反而是他赢。

    ── 为什么不能只在 Python 里 `if expected != product.revision: raise` ──
    那样有个致命的空档：

        A 读 revision=1 ──┐
        B 读 revision=1 ──┤  两个人都通过校验
        A 写 revision=2   │
        B 写 revision=2 ──┘  B 覆盖了 A，没人看到 409

    FastAPI 的同步路由跑在线程池里，两个人同时点保存就是这个时序，而且它
    只在「真的同时」时才出错 —— 用手点永远测不出来，上线以后偶尔丢一次改动。
    所以真正的判定交给了下面这条条件 UPDATE：SQLite 在 WAL 下写是串行的，
    `WHERE id=? AND revision=?` 只有一个人能改到行（rowcount=1），
    另一个必然拿到 rowcount=0。上面那次比较只是**快路径**（省掉一次
    UPDATE，也能在还没碰任何字段时就报错），判定权在数据库手里。
    """
    if expected_revision is None:
        bump_revision(product)
        return
    expected = int(expected_revision)
    actual = int(product.revision or 0)
    # 快路径：手上这一版和库里都不一样了，不必再发 UPDATE
    if expected != actual:
        raise _conflict(db, product, expected, actual, action)
    # 判定路径：条件更新。rowcount 为 0 说明在我们读完之后、写之前有人抢先改了。
    # 用 sql_update 而不是 update：本模块自己导出了一个 `def update(...)` 的业务
    # 函数，直接叫 update 的话这里的 SQLAlchemy 构造器会被它遮蔽成递归调用。
    result = db.execute(
        sql_update(Product)
        .where(Product.id == product.id, Product.revision == expected)
        .values(revision=expected + 1))
    if result.rowcount != 1:
        # 读回真实版本号再报，前端才知道该刷新到第几版
        latest = db.execute(
            select(Product.revision).where(Product.id == product.id)).scalar()
        raise _conflict(db, product, expected, int(latest or 0), action)
    # 让内存对象与库里一致：后续 flush 不会再把 revision 写回旧值，
    # 也不会因为「脏了」而多发一条没意义的 UPDATE。
    product.revision = expected + 1


def review_product_quality(db: Session, ident: Any, *, weight_kg: float,
                           package_length: float, package_width: float,
                           package_height: float, source: str, evidence: str,
                           operator_id: int, operator_name: str,
                           expected_revision: int) -> dict[str, Any]:
    """Persist verified measurements in publish fields and provenance separately."""
    from core.utils import now_ts

    product = resolve_product(db, ident)
    revision_guard(db, product, expected_revision, action="核验商品重量和尺寸")
    payload = dict(product.payload) if isinstance(product.payload, dict) else {}
    dimensions = {
        "packageLength": float(package_length),
        "packageWidth": float(package_width),
        "packageHeight": float(package_height),
    }
    payload["weight"] = float(weight_kg)
    payload.update(dimensions)
    product.payload = payload
    site = str(product.site or payload.get("site") or "")
    product.quality_review = {
        "verifiedRevision": int(product.revision or 0),
        "weightKg": float(weight_kg),
        "packageDimensions": dimensions,
        "source": source.strip(),
        "evidence": evidence.strip(),
        "site": site,
        "verifiedAt": now_ts(),
        "verifiedByOperatorId": operator_id,
        "verifiedByOperatorName": operator_name,
    }
    product.operator_id = operator_id
    db.flush()
    return {"id": str(product.id), "revision": int(product.revision),
            "qualityReview": product.quality_review}


def update(db: Session, ident: Any, *, category_id: Any = None, cid: Any = None,
           name: str | None = None, group_id: Any = None, title: str | None = None,
           set_category: bool = False, set_group: bool = False,
           operator_id: int | None = None,
           expected_revision: int | None = None) -> dict[str, Any]:
    """PATCH /warehouse/{id}：改类目 / 分组 / 标题。

    `operator_id` 只用于「最后改的人」这一列（列表页显示「14:32 由张三修改」），
    不参与任何判定——这不是权限系统。

    `expected_revision` 是乐观锁：调用方带上自己读到的那一版版本号，服务端在
    任何写入之前比对。**不传（None）就不校验**，见 revision_guard 的说明。
    """
    if not any([set_category, set_group, title is not None]):
        raise errors.validation("至少提供一个要修改的字段（categoryId / cid / groupId / title）")
    product = resolve_product(db, ident)
    # 校验必须在任何字段赋值之前：放在后面就成了「已经改了才报错」，
    # 虽然事务会回滚，但中间状态会被 flush 出去，语义也不对。
    revision_guard(db, product, expected_revision)
    if cid not in (None, "") or set_category:
        apply_category(db, product, category_id=category_id, cid=cid, name=name,
                       set_category=set_category, site=product.site)
    if set_group:
        apply_group(db, product, group_id=group_id, set_group=True)
    if title is not None:
        text = str(title).strip()
        if not 1 <= len(text) <= 255:
            raise errors.validation("标题长度必须在 1 到 255 之间", field="title",
                                    value=len(text))
        # 老版 TitleEditor.save 会先 preview，标题里还剩待清理词就直接拒绝
        # （「标题仍含待清理词，请先自动清理并核对结果」）。这里保持同一道闸门：
        # 否则绕过界面清理就能把违规词落库，之后入仓一路带上去。
        from services import word_service as _words

        words = _words.matcher(db)
        _cleaned, removed = _words.clean_title(text, words)
        if removed:
            raise errors.validation(
                "标题仍含违规词，请先清理：" + "、".join(removed),
                field="title", value=removed)
        # review 类只提示不拦：老版要求「确认已核查」，界面点清理时已经过目。
        # 这里本来算了一个 review 列表却没人用（ruff F841），保留语义但不必求值。
        product.title = text
        payload = dict(product.payload or {})
        payload["title"] = text
        product.payload = payload
    product.operator_id = operator_id
    product.updated_at = now_ts()
    # 版本号不在这里推：revision_guard 已经把它原子地推到下一版了
    # （带 expected_revision 走条件 UPDATE，不带走内存 +1）。
    # 这里再来一次就会变成一次保存 +2，前端的版本号立刻过期。
    db.flush()
    names = account_names(db)
    operators = operator_names(db)
    return {
        "product": serialize(product, account_name=names.get(product.account_id or 0, ""),
                             operator_name=operators.get(product.operator_id or 0, "")),
        "facets": facets(db),
    }


def batch_category(db: Session, *, ids: Iterable[Any], category_id: Any = None,
                   cid: Any = None, name: str | None = None, group_id: Any = None,
                   set_category: bool = False, set_group: bool = False,
                   operator_id: int | None = None) -> dict[str, Any]:
    """批量改类目/分组，单事务，全成功或全失败。"""
    wanted = [str(item).strip() for item in ids if str(item or "").strip()]
    deduped = list(dict.fromkeys(wanted))
    if not deduped:
        raise errors.validation("请选择要修改的商品", field="ids")
    if len(deduped) > 200:
        raise errors.validation("一次最多修改 200 件商品", field="ids", value=len(deduped))
    if not (set_category or set_group):
        raise errors.validation("至少提供一个目标字段（categoryId / cid / groupId）")

    products: list[Product] = []
    missing: list[str] = []
    for ident in deduped:
        try:
            products.append(resolve_product(db, ident))
        except errors.AppError as exc:
            if exc.code == errors.NOT_FOUND:
                missing.append(ident)
                continue
            raise
    if missing:
        raise errors.not_found(f"有 {len(missing)} 件商品不存在，本次未做任何修改",
                               missing=missing)

    updated: list[dict[str, Any]] = []
    try:
        for product in products:
            if set_category:
                apply_category(db, product, category_id=category_id, cid=cid, name=name,
                               set_category=set_category, site=product.site)
            if set_group:
                apply_group(db, product, group_id=group_id, set_group=True)
            product.operator_id = operator_id
            product.updated_at = now_ts()
            # 批量改也要换版本：否则别人手上的旧版本号在批量操作后仍然有效，
            # 下一次单件保存会把这一批改动一起盖掉。
            bump_revision(product)
        db.flush()
    except Exception:
        db.rollback()
        raise
    for product in products:
        updated.append({
            "id": str(product.id),
            "cid": product.cid or "",
            "categoryId": str(product.category_id) if product.category_id else None,
            "categoryName": product.category_name or "",
            "groupId": str(product.group_id) if product.group_id else None,
            "group": product.group_name or "",
        })
    return {
        "updated": len(updated),
        "requested": len(deduped),
        "failed": [],
        "products": updated,
        "facets": facets(db),
    }


def delete(db: Session, ident: Any) -> dict[str, Any]:
    """软删除；图片结果文件一并清理，发布记录保留并把 product_id 置 NULL。"""
    product = resolve_product(db, ident)
    running = (db.query(ImageJob)
               .filter(ImageJob.product_id == product.id,
                       ImageJob.status.in_(("queued", "submitted"))).count())
    if running:
        raise errors.busy(f"该商品还有 {running} 个图片任务在跑，请等待结束后再删除",
                          runningJobs=running)
    in_flight = (db.query(PublishLog)
                 .filter(PublishLog.product_id == product.id,
                         PublishLog.status == "processing").count())
    if in_flight:
        raise errors.busy(f"该商品还有 {in_flight} 条发布记录在处理中，请稍后再删除",
                          runningJobs=in_flight)

    files_removed = _remove_result_files(db, product)
    now = now_ts()
    product.deleted_at = now
    product.updated_at = now
    product.status = "archived"
    bump_revision(product)
    db.query(PublishLog).filter(PublishLog.product_id == product.id).update(
        {PublishLog.product_id: None}, synchronize_session=False)
    db.flush()
    return {
        "deleted": True,
        "id": str(product.id),
        "key": product.warehouse_key,
        "filesRemoved": files_removed,
        "stats": stats(db),
    }


def select_translatable_images(
    payload: dict[str, Any], *, main_limit: int = MAIN_IMAGE_LIMIT,
    detail_limit: int = DETAIL_IMAGE_HARD_LIMIT, sku_limit: int = MAIN_IMAGE_LIMIT,
) -> dict[str, list[str]]:
    """对主图 / 详情图 / SKU 图做「本地 OCR 筛选 + 内容去重」，返回各类要翻译的源图。

    老版口径（用户在 2026-10-30 明确复述）：
      · 三类图各自最多 9 张；
      · 只把「含中文」的图拿出去翻译（keep = 无中文不翻，review = 没识别到文字也不翻）；
      · 内容相同（OCR 文字一致）的重复图只翻一张，其余重复位用同一张译文覆盖；
      · remove（厂家宣传）整张不翻。

    返回 {main:[...], detail:[...], sku:[...]}，值都是「代表图」URL，
    保持原顺序、同类内按 OCR 文字去重。任何一类 OCR 不可用时退化为
    该类全部图（宁可多翻，也不因 OCR 抖动漏翻含中文的图）。
    """
    from services import image_review_service, pre_save_check

    def _pick(urls: list[str], limit: int) -> list[str]:
        """先剔废图再截上限 —— 顺序不能反。

        负责人口径（2026-11-11）：12 张详情图不是直接取前 9 张。
        前期图里常夹厂家宣传图/尺码对照图，先截 9 张会把第 10、11 张的
        好图挡在门外，留下的 9 张里还混着废图。正确顺序：
          1. 全量 OCR 分类（不预截）
          2. 先剔 remove（厂家宣传/尺码图）
          3. 剩下去重 + 截上限（补进来的是后面的好图）
          4. 从候选里挑含中文的提交翻译
        """
        urls = list(dict.fromkeys(
            str(u) for u in urls if isinstance(u, str) and u.startswith("http")))
        if not urls:
            return []
        try:
            rows = image_review_service.analyze(urls, limit=len(urls))
        except Exception as exc:  # noqa: BLE001 - OCR 不可用退化为全部
            logger.warning("图片 OCR 筛选不可用，按全部提交翻译：%s", exc)
            return urls[:limit]
        by_url = {str(row.get("url") or ""): row for row in rows}
        failed_rows = [row for row in rows if row.get("error")]
        if rows and len(failed_rows) == len(rows):
            logger.warning(
                "图片 OCR 全部不可用（%s），本次按全部图提交翻译：%s",
                str(failed_rows[0].get("error"))[:80],
                failed_rows[0].get("reason") or "")
            return urls[:limit]
        # 先剔废图，再截上限
        survivors = [u for u in urls
                     if str((by_url.get(u) or {}).get("action") or "") != "remove"]
        candidates = survivors[:limit]
        picked: list[str] = []
        seen_text: set[str] = set()
        for url in candidates:
            row = by_url.get(url) or {}
            if row.get("error"):
                picked.append(url)
                continue
            action = str(row.get("action") or "")
            # review = OCR 一行字都没识别到。实测（2026-11-11，12 张详情图）            # 这类图全是纯产品照，图上确实没有文字，不需要翻译。            # 之前这里「照翻」是怕 OCR 漏识别深色底白字/艺术字，但代价是            # 每件多翻 2-3 张纯产品图（每张 0.024 元），而实测 OCR 对中文            # 的召回很稳（同一批 7 行中文全部识别到，置信度 0.98-1.0）。            # 负责人口径：只翻有中文的图。            if action == "review":                continue
            if action != "translate":
                continue
            text = re.sub(r"\s+", "", str(row.get("text") or ""))
            if text:
                if text in seen_text:
                    continue
                seen_text.add(text)
            picked.append(url)
        return picked

    main_urls = list(payload.get("imgUrls") or [])
    detail_urls = pre_save_check.extract_detail_image_urls(payload.get("notes"))
    sku_urls = [item["url"] for item in sku_image_sources(payload)]

    return {
        "main": _pick(main_urls, main_limit),
        "detail": _pick(detail_urls, detail_limit),
        "sku": _pick(sku_urls, sku_limit),
    }


def attach_image_translation(db: Session, product: Mapping[str, Any], *,
                             limit: int, source_images: list[str] | None = None,
                             source: str = "main", operator_id: int | None = None
                             ) -> dict[str, Any]:
    """把商品主图、SKU 图或详情图的翻译任务提交给图片服务。

    只做提交，不等结果：图片翻译是异步的（`GET /images/jobs/{id}` 查进度），
    入仓流程不能被它阻塞。真正失败也不回滚商品——文字已经处理好了，
    图片没翻上是「部分完成」，不是「这件商品废了」。

    返回结构里如实带 submitted/failed，界面显示的是这个数字，
    而不是商品自带几张图（那是「有几张图」，不等于「翻译了几张」）。

    `operator_id` 一路传到 image_service.translate：这里提交的图同样按张扣费，
    入仓带图翻译和单独去图片页翻译是同一笔账。
    """
    from services import image_service

    if source not in ("main", "detail", "sku"):
        raise errors.validation("source 只能是 main / detail / sku", field="source", value=source)
    images = list(dict.fromkeys(
        str(url) for url in (source_images or [])
        if isinstance(url, str) and url.startswith("http")))
    # 传入的 source_images 已经由 select_translatable_images 做过
    # 「OCR 只留含中文 + 内容去重」，这里直接按上限截取提交，不再重复 OCR。
    count = max(0, min(int(limit or 0), MAIN_IMAGE_LIMIT))
    selected = images[:count]
    source_name = {"main": "主图", "detail": "详情图", "sku": "SKU 图"}[source]
    if not selected:
        return {"requested": 0, "submitted": 0, "failed": 0, "jobs": [],
                "reason": f"没有可翻译的{source_name}"}

    items = [{"productId": str(product.get("id") or ""),
              "detailId": str(product.get("detailId") or ""),
              "source": source, "url": url} for url in selected]
    try:
        result = image_service.translate(db, {"items": items, "size": "1:1"},
                                        operator_id=operator_id,
                                        owner_operator_id=operator_id)
    except errors.AppError as exc:
        # 没配密钥（IMAGE_KEY_MISSING）或超预算：如实回报，不阻断入库
        logger.warning("图片翻译未提交：%s", exc.message if hasattr(exc, "message") else exc)
        return {"requested": len(selected), "submitted": 0, "failed": len(selected),
                "jobs": [], "reason": getattr(exc, "message", str(exc))}

    jobs = result.get("jobs") or result.get("items") or []
    submitted = sum(1 for job in jobs if str(job.get("status") or "") != "failed")
    return {
        "requested": len(selected),
        "submitted": submitted,
        "failed": len(selected) - submitted,
        "jobs": jobs,
        "batchId": result.get("batchId"),
    }


# --- 图片翻译结果写回 payload（P0） ---------------------------------------
# 老版 product_workflow 在图片翻译拿到结果后，把 `imgUrls` / `notes` /
# `skuPropertyList` 三处换成译文地址，再整份 payload 保存（第 1546-1554 行构造，
# 第 1928-1938 行落库）。新版此前只把结果留在 ImageJob.result_url（本地路径），
# 从不写回 payload，于是「花钱翻译过的图」永远跟着原始图发到上游——翻译等于白做。
#
# 上游只认 http(s) 公网地址：本地 `/api/v1/images/jobs/{id}/file` 它拿不到，
# 所以写回前必须先把本地结果文件传到图床（services/image_upload.upload_image，
# 与老版 _upload_once 是同一条路）。ImageJob 上没有公网 URL 字段，只能现传。

_IMAGE_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
# 同一张译文图会被反复写回（每次入仓都跑一遍），传过就不再传：
# 进程内缓存省掉重复的 SFTP/HTTP 往返；跨进程靠图床侧「同名同大小不重放」兜底。
_PUBLIC_URL_CACHE: dict[str, str] = {}
_PUBLIC_URL_LOCK = threading.Lock()
# 缓存上限：服务是长驻进程，条目只增不减会慢慢吃掉内存。
# 到顶就整体清空——重传一次的代价远小于无界增长。
_PUBLIC_URL_CACHE_LIMIT = 512
# 单张图上传的超时（秒）。入仓是同步流程，不能被一张图卡住好几分钟。
UPLOAD_TIMEOUT = 20


def _is_image_url(value: Any) -> bool:
    return isinstance(value, str) and bool(_IMAGE_URL_RE.match(value.strip()))


def sku_image_sources(item: Any) -> list[dict[str, Any]]:
    """枚举 SKU 里所有图片位置（老版 `_sku_image_sources`）。

    给出 `propertyIndex` / `valueIndex` / `imageIndex` 是「原位替换」的坐标：
    只有按位置写回，才能保证 SKU 顺序、平台 SKU 编码和译文文本一字不动。
    `supplementarySkuImageUrls` 在供应商数据里既可能是单个字符串也可能是列表，
    两种形态都要枚举到，否则「补充图」那一半永远翻不到。
    """
    # 也接受直接传 skuPropertyList 列表：调用方手上常常只有属性列表
    properties = (item.get("skuPropertyList") or []) if isinstance(item, Mapping) else (item or [])
    sources: list[dict[str, Any]] = []
    if not isinstance(properties, list):
        return sources
    for property_index, prop in enumerate(properties):
        if not isinstance(prop, Mapping):
            continue
        values = prop.get("attrValueList") or []
        if not isinstance(values, list):
            continue
        for value_index, value in enumerate(values):
            if not isinstance(value, Mapping):
                continue
            base_path = f"skuPropertyList[{property_index}].attrValueList[{value_index}]"
            image_url = value.get("imgUrl")
            if _is_image_url(image_url):
                sources.append({
                    "path": f"{base_path}.imgUrl",
                    "kind": "imgUrl",
                    "url": str(image_url).strip(),
                    "propertyIndex": property_index,
                    "valueIndex": value_index,
                })
            supplementary = value.get("supplementarySkuImageUrls")
            if isinstance(supplementary, str):
                supplementary = [supplementary]
            if not isinstance(supplementary, list):
                continue
            for image_index, url in enumerate(supplementary):
                if _is_image_url(url):
                    sources.append({
                        "path": f"{base_path}.supplementarySkuImageUrls[{image_index}]",
                        "kind": "supplementarySkuImageUrls",
                        "url": str(url).strip(),
                        "propertyIndex": property_index,
                        "valueIndex": value_index,
                        "imageIndex": image_index,
                    })
    return sources


def rewrite_sku_image_urls(properties: Any, replacements: Mapping[str, str]) -> Any:
    """按 URL 映射替换 SKU 图片地址（老版 `_rewrite_sku_image_urls`）。

    只动 `imgUrl` 与 `supplementarySkuImageUrls`，`attrValueId` / `attrValue`
    这些翻译结果与平台主键必须原样保留——SKU ID 变了，平台侧就是另一件商品。
    deepcopy 后再改：入参是商品 payload 的一份引用，就地改会污染调用方
    手里「翻译前」的快照（预保存比对、回读校验都靠它）。
    """
    result = deepcopy(properties)
    if not isinstance(result, list):
        return result
    for prop in result:
        if not isinstance(prop, dict):
            continue
        values = prop.get("attrValueList") or []
        if not isinstance(values, list):
            continue
        for value in values:
            if not isinstance(value, dict):
                continue
            image_url = value.get("imgUrl")
            if isinstance(image_url, str):
                value["imgUrl"] = replacements.get(image_url, image_url)
            supplementary = value.get("supplementarySkuImageUrls")
            if isinstance(supplementary, str):
                value["supplementarySkuImageUrls"] = replacements.get(supplementary, supplementary)
            elif isinstance(supplementary, list):
                value["supplementarySkuImageUrls"] = [
                    replacements.get(url, url) if isinstance(url, str) else url
                    for url in supplementary
                ]
    return result


def _replace_url_list(urls: Any, replacements: Mapping[str, str]) -> tuple[list[Any], int]:
    """保持顺序的一对一替换；返回 (新列表, 命中数)。"""
    if not isinstance(urls, list):
        return [], 0
    replaced: list[Any] = []
    hits = 0
    for url in urls:
        key = url.strip() if isinstance(url, str) else url
        if isinstance(key, str) and key in replacements:
            replaced.append(replacements[key])
            hits += 1
        else:
            replaced.append(url)
    return replaced, hits


def _public_result_url(job: ImageJob, uploader: Any = None) -> str:
    """把一个 ImageJob 的本地结果文件换成上游能访问的 HTTPS 地址。

    返回空串表示「这张图现在换不了」：文件不在本地、上传失败、图床没配。
    调用方据此把它记进 missing，让界面提示「还有 N 张没翻完」，
    而不是把本地路径塞给上游（那样上游取不到图，整件商品图片失效）。
    """
    # result_url 若已经是公网地址就直接用：历史数据/别的写入路径可能存了完整
    # URL，这时没必要再传一次（也传不了——本地没有对应文件）。
    remote = str(job.result_url or "").strip()
    if remote.startswith("https://"):
        return remote
    if not job.filename:
        return ""
    key = f"{job.id}:{job.filename}"
    with _PUBLIC_URL_LOCK:
        cached = _PUBLIC_URL_CACHE.get(key)
    if cached:
        return cached

    from core.config import get_settings

    target = get_settings().image_results_dir / job.filename
    try:
        if not target.is_file():
            return ""
    except OSError:
        return ""

    try:
        if uploader is not None:
            url = uploader(target)
        else:
            from services import image_upload

            url = image_upload.upload_image(target, timeout=UPLOAD_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 - 上传失败只降级，不能掀翻入仓
        # 只记类型不记内容：image_upload 的异常短语可能带服务器地址
        logger.warning("译文图上传失败 job=%s type=%s", job.id, type(exc).__name__)
        return ""
    if not isinstance(url, str) or not url.startswith("https://"):
        return ""
    with _PUBLIC_URL_LOCK:
        if len(_PUBLIC_URL_CACHE) >= _PUBLIC_URL_CACHE_LIMIT:
            _PUBLIC_URL_CACHE.clear()
        _PUBLIC_URL_CACHE[key] = url
    return url


def _payload_image_urls(payload: Mapping[str, Any]) -> list[str]:
    """列出 payload 里所有图片地址（主图 / 详情图 / SKU 图 / 尺码图）。"""
    urls: list[str] = []
    main = payload.get("imgUrls")
    if isinstance(main, list):
        urls.extend(str(u) for u in main if isinstance(u, str))
    urls.extend(pre_save_check.extract_detail_image_urls(payload.get("notes")))
    for item in sku_image_sources(payload):
        url = str(item.get("url") or "")
        if url:
            urls.append(url)
    chart = payload.get("sizeChart")
    if isinstance(chart, str) and chart:
        urls.append(chart)
    return list(dict.fromkeys(urls))


def apply_image_results(db: Session, product: Product, payload: Any, *,
                        uploader: Any = None,
                        job_ids: list[str] | None = None) -> dict[str, Any]:
    """把已完成的图片翻译结果写回商品 payload（老版第 1928-1938 行的移植）。

    返回 `{replaced, main, detail, sku, missing}`：
      · `replaced` 是三处命中数之和；`main` / `detail` / `sku` 分别计数；
      · `missing` 是「有翻译任务但这次拿不到译文地址」的原始 URL，
        界面据此提示「还有 N 张没翻完」，而不是静默发原图。

    任何一步失败都只降级成本次不回写（replaced=0 + error 说明）：
    图片没翻完是常态，不能因为图片把整件商品的入仓废掉。
    """
    report: dict[str, Any] = {"replaced": 0, "main": 0, "detail": 0, "sku": 0,
                              "missing": [], "error": ""}
    if not isinstance(payload, dict):
        report["error"] = "payload 不是对象，跳过图片结果回写"
        return report
    product_id = getattr(product, "id", None)
    if product_id in (None, ""):
        # 还没落库的行没有 id，ImageJob.product_id 无从对应
        report["reason"] = "商品还没有 id，跳过图片结果回写"
        return report

    try:
        query = db.query(ImageJob).filter(ImageJob.product_id == product_id)
        if job_ids is not None:
            query = query.filter(ImageJob.id.in_(job_ids))
        jobs = query.order_by(ImageJob.created_at.asc()).all()
    except Exception as exc:  # noqa: BLE001 - 查库失败也不能掀翻入仓
        logger.warning("读取图片任务失败 product=%s type=%s", product_id, type(exc).__name__)
        report["error"] = "读取图片任务失败，本次未写回译文图"
        return report

    replacements: dict[str, str] = {}
    missing: list[str] = []
    try:
        for job in jobs:
            source = str(job.source_url or "").strip()
            if not _is_image_url(source):
                continue
            # 只认「真的有结果」的任务：filename 且文件在，或至少有 result_url。
            # 提交失败 / 还在排队的任务不该被当成译文。
            if not job.filename and not job.result_url:
                if source not in missing:
                    missing.append(source)
                continue
            target = _public_result_url(job, uploader)
            if target:
                replacements[source] = target
            elif source not in missing:
                missing.append(source)
    except Exception as exc:  # noqa: BLE001 - 查任务/上传失败都只降级
        logger.warning("图片结果收集失败 product=%s type=%s", product_id, type(exc).__name__)
        report.update({"missing": missing,
                       "error": f"图片结果收集失败（{type(exc).__name__}），本次按原图保存"})
        return report
    report["missing"] = missing
    if not replacements:
        return report

    # 入库会把图从 auto-temp 搬到 products，URL 变了但文件没变。
    # ImageJob.source_url 记的是提交翻译时的旧地址，直接精确匹配会全部失配
    # （2026-11-09 实测：搬运后 imageApplied.replaced=0，译文图白翻了）。
    # 这里按「同文件名视为同一张图」补齐映射。
    try:
        from services import image_upload

        for url in _payload_image_urls(payload):
            if url in replacements:
                continue
            for source, target in list(replacements.items()):
                if image_upload.zone_equivalent(url, source):
                    replacements[url] = target
                    break
    except Exception as exc:  # noqa: BLE001 - 补齐失败按原逻辑走
        logger.warning("图片地址分区等价映射失败 product=%s：%s", product_id, exc)

    try:
        main_before = payload.get("imgUrls")
        main_after, main_hits = _replace_url_list(main_before, replacements)

        # 详情图在 payload 里是 HTML 片段，URL 必须先抽出来再拼回去，
        # 与 pre_save_check 的「抽取 / 还原」保持同一对函数，保证可逆：
        # 自己写正则拼 HTML 很容易把 &amp; 之类的实体改坏。
        detail_before = payload.get("notes")
        detail_urls = pre_save_check.extract_detail_image_urls(detail_before)
        detail_after, detail_hits = _replace_url_list(detail_urls, replacements)

        props_before = payload.get("skuPropertyList")
        sku_sources = sku_image_sources(props_before)
        # 老版只把「SKU 里出现过的 URL」交给 SKU 改写，这里保持一致：
        # 主图/详情图的映射混进来只会让 SKU 里同名 URL 被无关结果覆盖。
        sku_urls = {source["url"] for source in sku_sources}
        sku_replacements = {url: replacements[url] for url in sku_urls if url in replacements}
        sku_hits = sum(1 for source in sku_sources if source["url"] in replacements)
        props_after = (rewrite_sku_image_urls(props_before, sku_replacements)
                       if sku_hits else props_before)
    except Exception as exc:  # noqa: BLE001 - 回写失败不得影响入仓
        logger.warning("图片结果写回失败 product=%s type=%s", product_id, type(exc).__name__)
        report.update({"replaced": 0, "main": 0, "detail": 0, "sku": 0,
                       "error": f"图片结果写回失败（{type(exc).__name__}），本次按原图保存"})
        return report

    # 三处都算完再一次性落到 payload：中途抛错时 payload 保持原样，
    # 不会出现「主图换了、详情图没换」这种半截状态。
    # 空映射 / 全部未命中时一个字都不改：payload 哈希不变，
    # 下游的「非图片字段未被改动」比对才有意义。
    if main_hits:
        payload["imgUrls"] = main_after
    if detail_hits:
        payload["notes"] = pre_save_check.detail_images_only(detail_after)
    if sku_hits:
        payload["skuPropertyList"] = props_after

    report.update({"replaced": main_hits + detail_hits + sku_hits,
                   "main": main_hits, "detail": detail_hits, "sku": sku_hits})
    return report


def _remove_result_files(db: Session, product: Product) -> int:
    from core.config import get_settings

    removed = 0
    results_dir = get_settings().image_results_dir
    jobs = db.query(ImageJob).filter(ImageJob.product_id == product.id).all()
    for job in jobs:
        name = job.filename or ""
        if not name:
            continue
        target = (results_dir / name).resolve()
        try:
            if target.is_file() and results_dir.resolve() in target.parents:
                target.unlink()
                removed += 1
        except OSError:
            continue
        job.filename = ""
        job.result_url = ""
    return removed


def batch_delete(db: Session, ids: Iterable[Any]) -> dict[str, Any]:
    deduped = list(dict.fromkeys([str(item).strip() for item in ids if str(item or "").strip()]))
    if not deduped:
        raise errors.validation("请选择要删除的商品", field="ids")
    if len(deduped) > 200:
        raise errors.validation("一次最多删除 200 件商品", field="ids", value=len(deduped))
    deleted: list[str] = []
    failures: list[dict[str, Any]] = []
    files = 0
    for ident in deduped:
        try:
            result = delete(db, ident)
        except errors.AppError as exc:
            failures.append({"id": ident, "code": exc.code, "reason": exc.message})
            continue
        deleted.append(result["id"])
        files += int(result["filesRemoved"])
    return {
        "deleted": deleted,
        "requested": len(deduped),
        "deletedCount": len(deleted),
        "filesRemoved": files,
        "failures": failures,
        "stats": stats(db),
    }


# --- 从采集箱入仓 ---------------------------------------------------------
# 老版 product_workflow 的三步收尾（_prepare_weight / _normalize_package_dimensions /
# _normalize_size_chart_url）。之前新版这三步全缺：
#   - 重量不加上增量 → 跨境运费按上游原值算，偏低；
#   - 包裹尺寸完全不写 → pre_save_check 规则 3 被 package_dimensions=None 跳过；
#   - 尺码图完全不写 → 规则 4 被 size_chart_url="" 跳过。
# 结果是「校验都过、上架全缺」，平台侧运费与尺码表都错。

def _workflow_setting(db: Session, key: str, fallback: Any,
                      operator_id: int | None = None) -> Any:
    """读工作流设置里的一个键；读不到就用 fallback，不因设置缺失而报错。"""
    from services import setting_service

    try:
        scope_id = operator_id
        if operator_id is not None:
            from services import operator_service

            actor = operator_service.get(db, operator_id)
            if (actor is not None
                    and operator_service.role_of(actor) != operator_service.ROLE_MEMBER):
                scope_id = None
        value = setting_service.workflow_defaults(db, operator_id=scope_id).get(key)
    except Exception:  # noqa: BLE001 - 设置读坏了不该挡住入仓
        return fallback
    return fallback if value is None else value


WEIGHT_RANGE = (0.05, 5.0)
# 鞋类业务口径（2026-10-30 负责人明确）：
#   · 低于 WEIGHT_AUTO_MIN（正常鞋子不可能这么轻，多是妙手采集没带重量）：自动修正到该值，
#     不再直接报错拦下——运营不用回妙手逐件补重量。
#   · 高于 WEIGHT_HARD_MAX（鞋子不可能这么重，多半是数据错）：跳过不处理，备注「重量异常」。
#   · 两者之间正常处理。两个阈值都可按业务调整。
WEIGHT_AUTO_MIN = 0.4

# 各品类允许的最轻原重。负责人口径（2026-11-11）：拖鞋/洞洞鞋这类 EVA
# 发泡材质天生就轻，上游采集 0.33kg 是真实值，不能被「太轻自动补基准」
# 拉成 0.9。拖鞋最轻可以到 0.25，之后再加 0.1 增量。
# 全局 0.4 对运动鞋是对的，对拖鞋过高。
CATEGORY_MIN_WEIGHT = {
    "拖鞋": 0.25,
    "洞洞鞋": 0.25,
    "凉鞋": 0.25,
    "凉拖": 0.25,
    "sandal": 0.25,
    "selipar": 0.25,
    "服饰": 0.2,
}


# 原重上限（不含重量增量）。负责人 2026-11-09 定：
#   · 这个阈值只用来判断**上游原重**是否属于采集错误，与「重量增加」那 0.1 无关；
#   · 可在处理设置里改（workflow.weightLimit），默认 1.5kg；
#   · 所有品类统一 1.5，厚底老爹鞋这类 1.27kg 属于正常装箱重量，
#     按老的 1.2 会把正常货拦下来。
WEIGHT_HARD_MAX = 1.5
WEIGHT_LIMIT_DEFAULT = 1.5
WEIGHT_LIMIT_RANGE = (0.1, 10.0)
DIMENSION_MAX = 300
DEFAULT_PACKAGE_DIMENSIONS = {
    "packageLength": 30, "packageWidth": 20, "packageHeight": 10}


def _raw_weight_of(info: Mapping[str, Any], *, pre_increment: Any = None) -> str:
    """取「加增量之前」的重量，用来判断上游原始重量有没有变。"""
    if isinstance(pre_increment, Mapping) and pre_increment.get("before") is not None:
        return str(pre_increment.get("before"))
    return str(info.get("weight") or "")


def _copy_saved_weights(payload: dict[str, Any], previous: Mapping[str, Any]) -> None:
    """把上次存下的重量原样搬回来，避免重复加增量。"""
    if previous.get("weight") not in (None, ""):
        payload["weight"] = previous["weight"]
    saved_skus = previous.get("skuMap") if isinstance(previous.get("skuMap"), dict) else {}
    for key, sku in (payload.get("skuMap") or {}).items():
        if not isinstance(sku, dict):
            continue
        old = saved_skus.get(key) if isinstance(saved_skus.get(key), dict) else None
        if old is not None and old.get("weight") not in (None, ""):
            sku["weight"] = old["weight"]


def prepare_weight(info: dict[str, Any], increment: Any = 0.1,
                   *, category_name: Any = "", weight_limit: Any = None) -> dict[str, Any]:
    """商品与每个未删除 SKU 的重量统一加增量（老版 _prepare_weight）。

    注意不是幂等的：跑两次会加两次，所以只在入仓这一步做一次，
    且必须在写库前完成——写库后再改，payload 与展示字段就对不上了。
    """
    try:
        amount = float(increment)
    except (TypeError, ValueError):
        amount = 0.1
    if amount < 0 or amount > 1:
        amount = 0.1

    before = info.get("weight")
    targets: list[tuple[str, dict[str, Any] | None]] = [("weight", None)]
    for key, sku in (info.get("skuMap") or {}).items():
        if isinstance(sku, dict) and str(sku.get("isDelete", "0")) != "1":
            targets.append((f"sku:{key}:weight", sku))

    parsed: dict[str, float] = {}
    skipped_increment: list[str] = []
    auto_fixed_from: Any = None
    # 上限只比原重，不加增量（负责人 2026-11-09 口径）
    hard_max = _weight_hard_max_for(category_name, weight_limit)
    # 太轻/为空时补到的基准：鞋类用 0.4，暴走/轮滑用 0.5；取品类默认重量与 0.4 的较大者
    # 太轻时补到的基准 = 该品类允许的「最轻原重」。
    #
    # 不能再用 max(全局0.4, 品类默认0.6) —— 那会把 EVA 拖鞋真实的 0.33kg
    # 强行拉到 0.6（负责人 2026-11-11 指出：拖鞋就是这么轻）。
    # 现在只按品类下限：拖鞋/洞洞鞋 0.25，运动鞋 0.4。
    # 只有「重量为空」或低于该下限时才补到下限值。
    base_min = category_min_weight(category_name)
    for name, sku in targets:
        raw = info.get("weight") if sku is None else sku.get("weight")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            # 取不到重量：按「为空」处理，稍后按品类补默认值
            value = 0.0
        if value != value or value in (float("inf"), float("-inf")):
            value = 0.0
        # 超过该品类上限（普通鞋 1.2 / 暴走鞋 1.5）时分两种情况：
        #
        #   a) 原重 <= 上限 + 增量（如 1.6 <= 1.5+0.1）：说明这个重量
        #      很可能是**之前某次保存加过增量**写回草稿的（草稿重量会被
        #      我们的保存覆盖）。此时沿用草稿值、不再加增量，不算异常。
        #      硬拦会让这件商品永远处理不了（2026-11-10 实测：3522040694
        #      卡死在「采集原重 1.6 超过 1.5」）。
        #   b) 原重 > 上限 + 增量：真超重（多半是单位填错），拦下不处理。
        if value > hard_max:
            if value <= hard_max + amount:
                parsed[name] = value
                skipped_increment.append(name)
                continue
            raise errors.unprocessable(
                f"重量异常：采集原重 {raw} kg 超过设置的原重上限 {hard_max} kg"
                f"（不含 {amount} kg 增量），已跳过不处理。"
                f"请核对妙手采集的重量是否为克/千克单位填错",
                details={"weightError": {"field": name, "value": raw,
                                         "reason": "too_heavy"}},
            )
        # 太轻或为空（多为采集没带重量）：自动补到该品类的合理基准，不拦。
        if value < base_min:
            if sku is None:
                auto_fixed_from = raw
            value = base_min
        parsed[name] = value

    info["weight"] = round(parsed["weight"] + amount, 6)
    for name, sku in targets[1:]:
        if sku is not None:
            sku["weight"] = round(parsed[name] + amount, 6)
    report = {"before": before, "after": info["weight"], "increment": amount}
    if auto_fixed_from is not None:
        report["autoFixedFrom"] = auto_fixed_from
        report["autoFixedTo"] = base_min
    return report


def normalize_package_dimensions(value: Any = None) -> dict[str, float]:
    """包裹长宽高：必须是 0–300 的有限数，键必须是妙手那三个字段名。"""
    dimensions = dict(DEFAULT_PACKAGE_DIMENSIONS) if value is None else value
    fields = set(DEFAULT_PACKAGE_DIMENSIONS)
    if not isinstance(dimensions, Mapping) or set(dimensions) != fields:
        raise errors.unprocessable("请填写包裹长、宽、高", details={"field": "packageDimensions"})
    normalized: dict[str, float] = {}
    for field in fields:
        number = dimensions[field]
        try:
            number = float(number)
        except (TypeError, ValueError):
            raise errors.unprocessable("包裹尺寸须为 0 到 300 cm 之间的有效数字",
                                       details={"field": field}) from None
        if number != number or number in (float("inf"), float("-inf")) \
                or not 0 < number <= DIMENSION_MAX:
            raise errors.unprocessable("包裹尺寸须为 0 到 300 cm 之间的有效数字",
                                       details={"field": field, "value": dimensions[field]})
        normalized[field] = int(number) if number.is_integer() else number
    return normalized


def normalize_size_chart_url(value: Any) -> str:
    """尺码图必须是公开 HTTPS 地址；空串表示不设置。"""
    url = str(value or "").strip()
    if not url:
        return ""
    if len(url) > 2048:
        raise errors.unprocessable("请选择有效的尺码图地址")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise errors.unprocessable("尺码图须使用公开 HTTPS 图片地址")
    return url


def save_from_collect_box(db: Session, account: Account, client: MiaoshouClient, *,
                          detail_id: str, shop_id: str, language: str = "",
                          group_id: Any = None, price_template: str | None = None,
                          exchange_rate: Any = None,
                          enforce_pre_save: bool = True,
                          allow_title_review: bool = False,
                          allow_image_review: bool = False,
                          skip_image_review: bool = False,
                          operator_id: int | None = None,
                          automation_result: bool = False,
                           image_limits: Mapping[str, Any] | None = None
                           ) -> dict[str, Any]:
    """拉详情 → 按模板定价（可选）→ 入库（主图截断 9 张）。

    `allow_title_review` / `allow_image_review` 是人工复核的确认开关：老版默认
    抛错停下等人看，确认过才放行。`skip_image_review` 给批量路径用 —— 主图 OCR
    一件最多 9 张、冷跑要几十秒，批量入仓时按需开。

    自动化结果归本次操作员所有；学员不能覆盖共享成品或其他人的处理结果。
    """
    from services import product_service

    # 处理设置里的数量 = 保留几张（SKU 图数量 = 颜色数）
    _limits: Mapping[str, Any] = (image_limits
                                 if isinstance(image_limits, Mapping) else {})

    key = product_key(detail_id, shop_id)
    if automation_result:
        # One upstream draft is shared by everyone bound to this Miaoshou key.
        # Serialize processing before any upstream read/write, then refuse to
        # turn a shared product or somebody else's private result into our own.
        from core.locks import hold_for_session

        hold_for_session(
            db, [f"automation-process:{account.id}:{key}"], blocking=False,
            busy_message="该采集箱商品正在处理中，请稍后再试")
    existing = (db.query(Product)
                .filter(Product.account_id == account.id,
                        Product.warehouse_key == key)
                .one_or_none())
    if (automation_result and _is_member(db, operator_id)
            and existing is not None
            and (not existing.automation_result or existing.operator_id != operator_id)):
        raise errors.AppError(
            errors.FORBIDDEN,
            "该采集箱商品已关联共享成品或其他用户的处理结果，请选择另一件商品",
            status=403)
    if (automation_result and existing is not None
            and (existing.automation_images or {}).get("queued") is True):
        raise errors.busy("该商品的图片仍在处理，请等待完成后再操作")

    try:
        response = client.shop_detail(detail_id, shop_id)
    except MiaoshouError as exc:
        from services.account_service import map_upstream_error

        raise map_upstream_error(exc) from None
    block = product_service.detail_block(response)
    info = block["info"]
    if not info:
        raise errors.not_found(f"上游查无明细 {detail_id}", detailId=str(detail_id))
    if not block["ossMd5"]:
        raise errors.unprocessable("商品详情缺少 ossMd5，请先在妙手保存一次草稿")

    # 翻译放在**最后**一步（负责人 2026-11-09 定），这里只做占位：
    # 颜色、标题清洗、图片筛选的规则全是按中文写的（中文颜色词表、违规词表、
    # 工厂图判定）。先翻译再清洗等于拿马来语撞中文规则 —— 实测
    # HD866 kuning air（马来语「浅黄」）被中文清洗逻辑当型号码剥成 Kuning，
    # 与妙手规格表对不上，保存报「SKU【Kuning-41】不存在」，整件处理失败。
    # 正确顺序：把中文阶段该处理的都处理完，最后统一翻译一次。
    translation_report: dict[str, Any] = {}
    coverage: dict[str, Any] = {}

    template = None
    if price_template:
        raw = setting_service.price_templates(db).get(str(price_template))
        if not raw:
            raise errors.AppError(errors.TEMPLATE_INVALID,
                                  f"定价模板「{price_template}」不存在",
                                  status=422, details={"field": "template"})
        template = raw
    # 注意：定价必须在「重量加完增量之后」再算 —— 运费按最终包裹重量计费，
    # 先用原始重量算价、再把重量加 0.1 保存，会导致「价格按 1.85kg 算、实际存 1.95kg」，
    # 报价与实际重量不符（老版 product_workflow 是 prepare_weight 之后才 apply 定价）。
    payload = dict(info)
    # === 类目解析（必须在重量 / 尺寸 / 定价之前）===
    # 负责人 2026-11-12 明确要求：先有类目才能处理后面的东西。
    # 重量上限、包裹尺寸推荐值、尺码图 URL、颜色清洗的 attrId 全部按
    # 类目取值；类目为空时这些步骤要么走默认值、要么直接跳过，
    # 结果就是「类目为空」失败，或者用错误的默认值算出一份错价格。
    #
    # 原来这段在图片清洗之后（第 2582 行左右），前面已经用过
    # product_service.category_label(payload) 算重量基准了 —— 那时
    # payload 还没有 cid，等于拿「未分类」的默认值算完整件商品。
    # 类目解析：先本地关键词（毫秒级），命不中才调 AI（20-30 秒）。
    #
    # 负责人口径（2026-11-12）：「AI 类目匹配有点慢啊，万一没识别出来怎么办」。
    # 原来一上来就调 AI，每件商品白等 20-30 秒；而雨鞋/拖鞋/跑鞋这类
    # 标题特征极明显，本地词表就能定。现在：
    #   1. 草稿已有 cid → 直接用；
    #   2. 关键词兜底唯一命中 → 直接用（毫秒级）；
    #   3. 都没命中 → 才调 AI 匹配（带超时，失败也不阻断）；
    #   4. 全失败 → 报「请手动选类目」，不让整批默默挂掉。
    #
    # 注意：类目虽然排在流程最前面，但它是**文字处理**的一环 ——
    # 图片处理（OCR 挑图 / 翻译）完全不看 cid（select_translatable_images
    # 里零处引用 cid），所以 AI 匹配慢只会拖文字，不影响图片。
    cid = str(payload.get("cid") or "").strip()
    category_source = "existing" if cid else ""
    category_message = ""
    if not cid:
        # 第 1 档：本地关键词，毫秒级
        kw_cid = ""
        try:
            kw_cid = category_service.fallback_by_keyword(
                db, str(payload.get("title") or ""),
                account_id=account.id, site=str(info.get("site") or ""))
        except Exception:  # noqa: BLE001 - 关键词不命中就走 AI
            kw_cid = ""
        if kw_cid:
            cid = str(kw_cid)
            category_source = "keyword"
            logger.info("类目关键词命中 detailId=%s cid=%s（跳过 AI 匹配）",
                        detail_id, cid)
    if not cid:
        # 第 2 档：AI 匹配。慢，所以放最后。
        try:
            resolved = category_service.resolve_category(
                db, client=client, detail_id=detail_id, shop_id=shop_id,
                info=payload, account_id=account.id,
                site=str(info.get("site") or ""))
        except errors.AppError as exc:
            # AI 匹配不唯一是「需要人做决定」，不是「系统坏了」：转成提示继续走，
            # 让 pre_save 的规则 2 报出来，别把整件商品的处理链打断。
            resolved = {"cid": "", "source": "", "message": exc.message}
        except Exception as exc:  # noqa: BLE001 - AI 超时不该拖垮整件商品
            logger.warning("AI 类目匹配失败 detailId=%s：%s", detail_id, exc)
            resolved = {"cid": "", "source": "", "message": str(exc)[:200]}
        cid = str(resolved.get("cid") or "").strip()
        category_source = str(resolved.get("source") or "")
        category_message = str(resolved.get("message") or "")
    if cid:
        payload["cid"] = cid
    category_row = (category_service.find_by_cid(db, cid, site=str(info.get("site") or ""),
                                                 account_id=account.id)
                    if cid else None)

    # 加增量之前先看这件商品是不是已经处理过。
    # prepare_weight 不幂等：同一件重复入仓会一次次 +0.1，重量越漂越远。
    # 老版靠 run_records 比对上次保存结果来拦（「已处理商品与上次保存结果
    # 不一致，请在妙手核对，避免重复增加重量」），这里用现成的仓库行做同等检查。
    already_processed = bool(existing is not None and existing.saved_at)

    increment = _workflow_setting(db, "weightIncrement", 0.1, operator_id)
    # 原重上限：处理设置里的独立项，不含增量（负责人 2026-11-09 定）
    weight_limit = _workflow_setting(db, "weightLimit", WEIGHT_LIMIT_DEFAULT, operator_id)
    if already_processed and existing is not None:
        # 已入过仓：沿用上次存下的重量，不再加增量。
        # 上游原始重量确实变了（换了规格）才重新算一次。
        previous = existing.payload if isinstance(existing.payload, dict) else {}
        saved_source = str(previous.get("_sourceWeight") or "")
        if not saved_source:
            # _sourceWeight 丢失（实验重置 / 老数据 / 手工改库）：
            # **绝对不能**用「上次重量 - 增量」倒推。倒推把已经加过 N 次增量
            # 的重量当成「原始重量」，每重跑一次就再 +0.1，重量滚雪球上涨，
            # 最终超过原重上限被拦（2026-11-09 实测：SKU 重量被推到 1.6kg 还在涨）。
            # 正确做法：不倒推、不加增量，沿用上次存下的重量，并当场补记
            # _sourceWeight（用上次的最终重量），下次就能正常比对。
            _copy_saved_weights(payload, previous)
            payload["_sourceWeight"] = str(previous.get("weight") or "")
            weight_report = {"before": previous.get("weight"),
                             "after": payload.get("weight"),
                             "increment": 0, "reused": True, "sourceMissing": True}
            return weight_report
        else:
            weight_report = prepare_weight(
                payload, increment,
                category_name=product_service.category_label(payload),
                weight_limit=weight_limit)
            payload["_sourceWeight"] = _raw_weight_of(payload, pre_increment=weight_report)
    else:
        weight_report = prepare_weight(
            payload, increment,
            category_name=product_service.category_label(payload),
            weight_limit=weight_limit)
        payload["_sourceWeight"] = _raw_weight_of(payload, pre_increment=weight_report)

    # 现在 payload 的包裹重量已是最终值（含增量），据此定价
    _source_weight = payload.get("_sourceWeight")
    payload, _report = product_service.process_price(payload, template=template,
                                                     exchange_rate=exchange_rate)
    # process_price 返回的是 payload 的副本；把记账字段补回去，供下次重复入仓比对
    if _source_weight is not None:
        payload["_sourceWeight"] = _source_weight
    # 重量自动修正记录，写进 payload 供前端/详情页提示「原重量过轻，已自动修正」
    if isinstance(weight_report, dict) and weight_report.get("autoFixedFrom") is not None:
        payload["weightAutoFixed"] = {
            "from": weight_report.get("autoFixedFrom"),
            "to": weight_report.get("autoFixedTo"),
        }

    # 包裹尺寸：优先用运营在设置里显式配的；没配（或就是旧默认 30×20×10）时
    # 按品类给合理值 —— 鞋盒与软包装的跨境运费差很多，用统一默认会算错价。
    _raw_dims = _workflow_setting(db, "packageDimensions", None, operator_id)
    _configured_dims = normalize_package_dimensions(_raw_dims)
    # 用户**显式配过**就用他的值，哪怕等于 30×20×10。
    #
    # 旧逻辑把「30/20/10」当成「没配过、用旧默认」，自动换成品类推荐值
    # （鞋类 33×22×12），于是运营在界面上明明填了 30/20/10、商品里却是
    # 33/22/12（2026-11-10 实测）。只有**根本没配**（None / 空）才走品类兜底。
    _explicit = (isinstance(_raw_dims, Mapping) and bool(_raw_dims)
                 and all(_raw_dims.get(k) not in (None, "") for k in
                         ("packageLength", "packageWidth", "packageHeight")))
    _is_legacy_default = not _explicit
    if _is_legacy_default:
        # 此时 category_row 还没解析出来，先用 payload 上的类目标签；
        # payload["cid"]/类目名在上游详情里就有，足够判断鞋盒还是软包装。
        _cat_name = product_service.category_label(payload)
        _spec = category_package_spec(_cat_name) or {}
        if _spec:
            dimensions = {
                "packageLength": float(_spec["length"]),
                "packageWidth": float(_spec["width"]),
                "packageHeight": float(_spec["height"]),
            }
        else:
            dimensions = _configured_dims
    else:
        dimensions = _configured_dims
    payload.update(dimensions)
    chart_url = normalize_size_chart_url(
        _workflow_setting(db, "sizeChartUrl", "", operator_id))
    if chart_url:
        payload["sizeChart"] = chart_url
        payload["sizeChartType"] = "image"

    # 图片固化：把上游（1688/妙手）的图片地址全部换成我们自己的图床地址。
    #
    # 只在**手动入库**（automation_result=False）时做，这是负责人的口径
    # （2026-11-07 明确）：自动化处理只把商品做出来、放进成品仓库，图片仍指向上游；
    # 人工挑中并入库时才落到自有图床。理由是自动化是一次性过 200 件的大批量，
    # 每件几十张图全下载转存会把带宽和图床配额吃光，而真正决定要卖的可能只有几件。
    #
    # 认领/发布路径（publish_service._claim_payloads）另有兜底：真的要把图发给
    # 平台之前会再转一次，所以这里跳过不会导致发布出去的图是外链。
    # 图片固化：把上游（1688/妙手）的图片地址换成我们自己的图床地址。
    #
    # 自动化处理阶段也要做（2026-11-09 更正）：
    #   · 负责人原话说的是「自动化只做出来、手动入库再转存」，
    #     但实测图片翻译必须基于**能下载到的图**：1688 图地址带鉴权/防爬，
    #     翻译服务取不到，图里的中文就永远清不掉。
    #   · 而且上游图会过期变挂图，仓库里放着外链等于把商品图交给别人保管。
    # 所以这里不再按 automation_result 跳过，两条路径都固化。
    image_store_report: dict[str, Any] = {}
    try:
        from services import image_store

        image_store_report = image_store.persist_payload_images(payload)
    except Exception as exc:  # noqa: BLE001 - 固化失败不该阻断入库
        logger.warning("商品图片固化失败 detailId=%s：%s", detail_id, exc)
        image_store_report = {"error": str(exc)[:200]}

    # ===== 图片处理：主图 / 详情图 / SKU 图 =====
    # 负责人口径（2026-11-09）：工厂宣传图和尺码对照图会混进主图与详情图，
    # 详情区除了图片不要任何文字。这里在写回妙手之前统一清一遍。
    # OCR 拿不到结果时不清（宁可不删，也不误删用户要的图）。
    image_report: dict[str, Any] = {}
    try:
        analyzed = analyze_payload_images(payload, retry=skip_image_review)
        detail_rows = rows_index(analyzed.get("detail") or [])
        image_report["detail"] = clean_detail_images(
            payload, limit=DETAIL_IMAGE_HARD_LIMIT, rows_by_url=detail_rows)
        # 按处理设置「真正截断」主图 / 详情图 / 颜色：
        # 界面上的数字是保留几张（SKU 图数量 = 颜色数），不是翻译几张。
        # 顺序必须在 clean 之后：先删废图（厂家宣传/尺码表），再按上限取前 N 张，
        # 这样设 9 时补进来的是好图而不是废图。
        try:
            image_report["limits"] = apply_image_limits(
                payload,
                main_limit=int(_limits.get("main") or 0),
                detail_limit=int(_limits.get("detail") or 0),
                sku_limit=int(_limits.get("sku") or 0))
        except Exception as exc:  # noqa: BLE001 - 截断失败不阻断入库
            logger.warning("按设置截断图片失败 detailId=%s：%s", detail_id, exc)
        # 主图的工厂图/尺码图剔除由 pre_save_gate 的 removeIndexes 负责，
        # 闸门在主流程后面才算，所以这里只把 OCR 结果记下来，避免重复识别。
        image_report["ocr"] = {key: len(value) for key, value in analyzed.items()}
    except Exception as exc:  # noqa: BLE001 - 图片清洗失败不阻断入库
        logger.warning("图片清洗失败 detailId=%s：%s", detail_id, exc)
        image_report = {"error": f"{type(exc).__name__}: {exc}"[:160]}


    # 类目发布要求（老版 extract_category_requirements + validate_category_requirements）。
    # 平台按类目校验必填项：有的类目强制包裹长宽高，有的强制某些属性。这些要求
    # 只有上游知道，本地无从推断，所以必须现查。查不到就传 None 退化成不额外拦 ——
    # 网络抖一下不该让整批商品入不了仓。
    requirements = None
    if cid:
        try:
            meta = client.category_metadata(
                str(info.get("site") or account.site or ""), cid, [int(shop_id)])
            requirements = category_service.extract_requirements(meta)
        except Exception:  # noqa: BLE001 - 查不到要求不等于商品不合格
            requirements = None
    # 违规词：remove 类直接从标题删掉，review 类只记录（与老版一致）。
    # 必须在 build_product 之前改 payload，否则改的是已经复制过的字段。
    from services import word_service

    words = word_service.matcher(db)
    cleaned_title, removed_words = word_service.clean_title(
        str(payload.get("title") or ""), words)
    if cleaned_title and cleaned_title != payload.get("title"):
        payload["title"] = cleaned_title
    violation_hits = product_service.scan_violation_words(
        db, " ".join(str(payload.get(k) or "") for k in ("title", "description")))

    # 颜色规范化必须在 build_product 之前：build 会快照 sku_properties，
    # 之后清洗只会让 payload 与展示字段不一致（审查 P0-7）。
    #
    # 清洗前先补类目真实 attrId（颜色 100000 / 尺码 100007）。妙手只有拿到
    # attrId 才会按我们提交的规格**重建**草稿的规格表；attrId 为空时它按草稿里
    # 原有的规格值逐字匹配，颜色被清洗过（HD866米色 → 米色）就必然报
    # 「SKU【米色-41】不存在」，整件商品存不进去（2026-11-09 实测）。
    spec_attr_ids = _fill_spec_attr_ids(
        db, client, payload, shop_id=shop_id,
        site=str(info.get("site") or account.site or ""))
    if spec_attr_ids:
        props = _clean_color_props(payload)
        # 尺码值也要清：供应商在码数后面写销售建议，翻译后超 50 字符被妙手拒收。
        _size_cleaned = _clean_size_values(payload)
        if _size_cleaned:
            logger.info("尺码值清洗 %d 条 detailId=%s", _size_cleaned, detail_id)
    else:
        # 拿不到类目 attrId 就不要清洗颜色。
        #
        # 为什么：妙手只有拿到 attrId 才会按提交的规格重建草稿规格表；
        # attrId 为空时它按草稿原有的规格值逐字匹配，我们不洗值原样提交反而
        # 是对的，洗过的值必然对不上（报「SKU【米色-41】不存在」）。
        # 宁可这次不清洗（下次补上 attrId 再清），也不能产出存不进去的商品。
        props = [p for p in (payload.get("skuPropertyList") or [])
                 if isinstance(p, dict)]
        logger.warning("类目规格 attrId 不可用，本次跳过颜色清洗 detailId=%s", detail_id)

    # 必要字段诊断（老版 product_workflow.diagnose_item）。
    # 顺序与老版一致：先判翻译覆盖，再判字段齐备；两项都过才继续。
    # 拦在写库前，避免把「缺重量 / SKU 价格为 0 / 无包裹尺寸」的商品存进来，
    # 那种货上架时会被平台直接拒掉。
    #
    # 诊断对象必须是「即将入库的 payload」，不是上游原始 info：
    # 包裹长宽高是此时才从 workflow.packageDimensions 写进 payload 的
    # （见上面的 payload.update(dimensions)），上游 info 里根本没有这三个字段，
    # 拿 info 去查会恒定报 missing_packageLength/Width/Height，等于每件都处理失败。
    # 老版 diagnose 用的就是拼装后的商品结构，这里对齐。
    diagnose_issues = product_service.diagnose(payload)
    blocking = [item for item in diagnose_issues
                if item.get("severity") != "warning"]
    if blocking and enforce_pre_save:
        raise errors.unprocessable(
            "商品必要字段诊断未通过：" + "；".join(
                str(item.get("message") or item.get("code")) for item in blocking),
            diagnostics=diagnose_issues,
        )

    # 保存前校验（老版 _pre_save_issues 的 10 条规则）。
    # 必须在 build_product 之前：不合格的商品不该进库，而不是进库后再标记。
    #
    # 两道硬闸门（老版 prepare_shoe 的 review_main_images 与标题复核）在这里先算：
    # 老版这两处都是「抛错即停」，新版一度退化成只往 report 里写标记，结果是带
    # 中文/工厂图的主图和含需复核词的标题能一路进库 —— 上架才被平台拒。
    # 闸门要拿到「即将入库的那份」payload，所以必须在 clean_title 之后算。
    gate = gate_service.pre_save_gate(
        db, payload,
        allow_title_review=allow_title_review,
        allow_image_review=allow_image_review,
        skip_images=skip_image_review,
    )
    # 工厂图剔除（老版 review_main_images 里 action=="remove" 的那步）。
    # 只报告不删等于把问题推给运营逐件手删；闸门已确认「剔完还有图」，
    # 这里按 index 从后往前删，避免前面的下标失效。
    dropped_images = _drop_main_images(payload, gate.get("removeIndexes") or [])
    # 主图去重 + 截到平台上限 9 张。
    # 采集回来的主图经常 15~20 张，以前靠 pre_save_check 报
    # 「main image count exceeds its configured limit」拦下来让人手删；
    # 负责人 2026-11-09 定的口径是「最多保留前九张」，所以这里直接截，
    # 不再让正常商品卡在这一步。
    main_urls = [str(u) for u in (payload.get("imgUrls") or [])
                 if isinstance(u, str) and u.strip()]
    unique_main = list(dict.fromkeys(main_urls))[:MAIN_IMAGE_LIMIT]
    if unique_main != payload.get("imgUrls"):
        payload["imgUrls"] = unique_main

    issues = pre_save_check.pre_save_issues(
        info, payload, coverage,
        main_limit=MAIN_IMAGE_LIMIT,
        detail_limit=DETAIL_IMAGE_HARD_LIMIT,
        # 尺寸与尺码图已经写进 payload 了，这里传真值让规则 3/4 真正生效
        package_dimensions=dimensions,
        size_chart_url=chart_url,
        # 图片处理在本流程里是**开着**的：主图截断、详情图清洗（删文字/厂家图/
        # 尺码图/去重）都在写回之前做了。传 False 会触发规则 8
        # 「关掉图片处理却改了图片字段」，把每一次正常清洗都判成异常
        # （2026-11-09 实测：detail_images 一清就报 image fields changed）。
        process_images_enabled=True,
        image_only=False,
        pricing_enabled=template is not None,
        # 闸门结果并进 issue 清单：规则 13/14 与上面 gate 共用同一份判定，
        # 各写一套迟早出现「预览说有、入仓说没有」。
        title_review_words=(gate.get("title") or {}).get("reviewWords") or [],
        image_review_pending=gate.get("images"),
        requirements=requirements,
    )
    # 产品标准自动修复（负责人 2026-11-05 定）：新采集的商品必须自动套用
    # 现在这套入库标准，不能靠事后手工脚本补。
    #   · 只在真要落库时修（save=false 的预览不动物理数据）
    #   · 修完重跑一遍 issues，让「修没修好」体现在同一份清单里
    standard_report: dict[str, Any] = {}
    if enforce_pre_save and isinstance(payload, dict):
        try:
            from services import product_standard

            _cat_name = product_service.category_label(payload)
            _verdict = product_standard.validate(
                payload, category_name=_cat_name,
                language=str(language or payload.get("language") or ""))
            if not _verdict["ok"]:
                _repaired, _notes = product_standard.repair(
                    payload, category_name=_cat_name,
                    language=str(language or payload.get("language") or ""))
                payload.clear()
                payload.update(_repaired)
                standard_report = {
                    "before": _verdict["issues"][:20],
                    "fixes": _notes[:20],
                }
                # 修完再校验一次：真正修不好的（如字段缺失）才继续报错
                _again = product_standard.validate(
                    payload, category_name=_cat_name,
                    language=str(language or payload.get("language") or ""))
                if _again["ok"]:
                    # 已修好，清掉旧的 issue 清单，避免误拦
                    issues = [
                        i for i in issues
                        if i not in set(_verdict["issues"])
                    ]
                standard_report["after"] = _again["issues"][:20]
        except Exception as exc:  # noqa: BLE001 - 修复失败不阻断入库主流程
            logger.warning("产品标准自动修复失败 detailId=%s：%s", detail_id, exc)
            standard_report = {"error": str(exc)[:200]}

    # 闸门阻断排在规则清单前面报：它是「停下等人看」，比字段缺失更该先说。
    blocks = gate.get("blocks") or []
    if blocks and enforce_pre_save:
        raise errors.unprocessable(
            "保存前校验未通过：" + "；".join(str(b.get("message") or "") for b in blocks),
            issues=[str(b.get("message") or "") for b in blocks],
            blocks=blocks,
            gate=gate,
        )
    # 预览（save=false）只回滚不写库，不该被拦：用户得先看到问题才能去改。
    # 只有真正要落库时才阻断。
    if issues and enforce_pre_save:
        raise errors.unprocessable(
            "保存前校验未通过：" + "；".join(issues),
            issues=issues,
            missingFields=coverage.get("missingFields") or [],
            categoryMessage=category_message,
        )

    # 妙手不接受 skuMap 里的 null：itemNum / identifierCode 这类字段传 null 时，
    # 平台解析规格会失败，报「SKU【米色-41】不存在」——看起来像颜色对不上，
    # 实际是空值把规格解析弄崩了（2026-11-09 实测：同样数据把 null 字段去掉就能存）。
    _strip_null_sku_fields(payload)
    _restore_sku_key_order(payload, info)

    # ===== 最后一步：翻译 =====
    # 走到这里，标题、图片、类目、颜色、重量、尺寸、价格都已在中文上处理完。
    # 翻译只做语言转换，不再参与任何清洗判定 —— 这是负责人 2026-11-09 定的流程：
    #   标题 → 图片（主图/详情图/SKU 图）→ 类目 → 颜色 → 重量 → 尺寸 → 价格 → 翻译
    # 中文阶段清洗有两层意义：中文颜色词表/违规词表/工厂图判定都是为中文写的，
    # 拿马来语去撞会把颜色洗坏（HD866 kuning air → Kuning，SKU 对不上）。
    if _needs_translation(payload, language):
        # 覆盖率检查的「源」必须是**上游原文**（info），不是已经处理过的 payload：
        # payload 走到这里已经换过图片、算过价格、清洗过颜色，结构上与译文对不上，
        # 拿它做源会把「我们把字段改成别的形态」误判成「漏翻」，
        # 报「translation coverage is incomplete」把整件商品挡在入库外（实测）。
        translated, translation_report = product_service.translated_info(
            client, payload, language)
        coverage = translation_coverage.translation_coverage(
            info, translated, target_language=str(language or "ms"))
        translation_report = {**translation_report, "coverage": coverage}
        # 只把译文字段写回，价格/重量/尺寸/图片/规格 id 一个字都不动：
        # translated_info 会把整块 info 换掉，直接赋值等于把前面几步全部作废。
        payload = merge_translated_fields(payload, translated)

    draft_verified = False
    if enforce_pre_save:
        # A local warehouse write is not proof that the processed draft reached Miaoshou.
        # Publish must only follow a verified upstream save of this exact payload.
        #
        # 提交用**副本**剔除只服务本地的字段（colorMap/sizeMap/translation 等）：
        # 这些字段要留在库里给详情页和译文核对用，直接改 payload 会让入库后的
        # 商品丢掉它们（2026-11-09 实测：probe_img_wire 的 translation 字段被删，
        # 11 项断言只剩 4 项通过）。妙手那边仍然拿不到这些多余字段。
        upstream_payload = _strip_local_only_fields(deepcopy(payload))
        _save_processed_draft(
            client, detail_id=str(detail_id), shop_id=str(shop_id),
            oss_md5=str(block["ossMd5"]), payload=upstream_payload, original=info,
            coverage=coverage, pricing_enabled=template is not None,
        )
        draft_verified = True

    # 入库 = 进成品仓：把图片从临时区搬到成品区。
    #
    # 图床分两个区（负责人 2026-11-09 定）：auto-temp 放自动化临时图、14 天清理；
    # products 放成品仓图、长期保留。这里必须**真的搬文件**，只改数据库标记的话
    # 临时区整批清理时会把成品商品的图一起删掉，商品全变挂图。
    # 搬不动就保留临时地址（图还在），不阻断入库。
    try:
        from services import image_upload

        promote_report = image_upload.promote_payload_images(payload)
        if promote_report.get("moved"):
            logger.info("入库搬运图片到成品区 detailId=%s moved=%s failed=%s",
                        detail_id, promote_report.get("moved"), promote_report.get("failed"))
    except Exception as exc:  # noqa: BLE001 - 搬运失败不阻断入库
        logger.warning("图片搬运到成品区失败 detailId=%s：%s", detail_id, exc)
        promote_report = {"error": str(exc)[:160]}

    key = product_key(detail_id, shop_id)
    fields = product_service.build_product(
        info=payload, detail_id=detail_id, shop_id=shop_id, account_id=account.id,
        shop_name=_shop_name(db, account, shop_id), language=language,
        category_id=category_row.id if category_row else None,
        category_name=(category_row.name_cn or category_row.name) if category_row else
        product_service.category_label(payload),
        site=str(info.get("site") or account.site or ""),
    )
    existing = (db.query(Product)
                .filter(Product.account_id == account.id,
                        Product.warehouse_key == key)
                .one_or_none())

    fields.setdefault("payload", {})
    if isinstance(fields["payload"], dict):
        fields["payload"]["translation"] = translation_report
        if props:
            fields["payload"]["skuPropertyList"] = props
        if violation_hits:
            fields["payload"]["violations"] = violation_hits
        if removed_words:
            fields["payload"]["removedWords"] = removed_words
    product = product_service.upsert_product(db, account.id, fields, warehouse_key=key)
    if automation_result:
        product.automation_images = None
    if automation_result and (existing is None or existing.automation_result):
        product.automation_result = True
    if existing is not None:
        # 重复入仓不重置认领状态与分组
        product.claimed = existing.claimed
        product.claimed_at = existing.claimed_at
        product.claimed_shop_id = existing.claimed_shop_id
        product.status = existing.status or "ready"
        if existing.group_id:
            product.group_id = existing.group_id
            product.group_name = existing.group_name
    if group_id not in (None, ""):
        apply_group(db, product, group_id=group_id, set_group=True)
    # 最后改的人：重复入仓时也要刷新（这次是另一个人点的保存）
    if operator_id is not None:
        product.operator_id = operator_id
    # 版本号不在这里推：product_service.upsert_product 已经推过一次
    # （入仓是整份覆盖 payload 的最典型路径），这里再推一次会让一次保存跳两版，
    # 前端的 revision 立刻半新半旧，排查时看不出规律。
    db.flush()

    # 译文图写回（老版第 1928-1938 行）。位置是刻意的，三处依赖都在它之前：
    #   · 重量 / 包裹尺寸 / 尺码图先算完并写进 payload；
    #   · pre_save_check 已经跑过。它按「关掉图片处理就别动图片字段」判定
    #     （process_images_enabled=False），把译文图写回放在它之前会让每一件
    #     翻过图的商品都被 rule 8 挡住、整批入不了仓；
    #   · product 行已落库，ImageJob.product_id 才对得上。
    # 图片没翻完是常态，这一步失败只降级、不抛错，所以放在 return 前的最后一格。
    #
    # 在深拷贝上改、再整份赋值：JSON 列不会感知就地 mutation，直接改
    # product.payload 里的字典不会产生 UPDATE。
    updated_payload = deepcopy(product.payload) if isinstance(product.payload, dict) else {}
    image_applied = (apply_image_results(db, product, updated_payload)
                     if not automation_result else {"replaced": 0})
    if image_applied.get("replaced"):
        product.payload = updated_payload
        # 展示字段跟着走，否则列表页与详情页看到的还是原图
        images = product_service.main_images(updated_payload)
        product.images = images
        product.detail_images = product_service.detail_images(updated_payload)
        product.sku_properties = product_service.sku_properties(updated_payload)
        if images:
            product.cover = images[0]
        db.flush()

    # Bind the upstream readback result to this exact product revision. A local
    # image replacement happens after the upstream save, so it invalidates proof.
    product.draft_verified_revision = (
        int(product.revision) if draft_verified and not image_applied.get("replaced")
        else None
    )
    db.flush()

    names = account_names(db)
    operators = operator_names(db)
    body = serialize(product, account_name=names.get(product.account_id or 0, ""),
                     operator_name=operators.get(product.operator_id or 0, ""))
    body["imageApplied"] = image_applied
    # 类目来源与兜底提示要让运营看见：AI 匹配上的和手动选好的，风险不一样。
    body["categorySource"] = category_source
    if category_message:
        body["categoryMessage"] = category_message
    if dropped_images:
        body["droppedImages"] = dropped_images
    # 两阶段保存的核对摘要（老版 payloadReview）。预览（save=false）时把它交给
    # 运营核对，确认阶段带上 payloadHash 回来，服务端重算一次比对 —— 中间商品
    # 在上游被改过、或图片翻译晚到改了图，hash 就对不上，保存会被拒。
    # 这是「核对过的和真存进去的是同一份」的唯一保证。
    body["payloadReview"] = payload_review(
        product, payload if isinstance(payload, dict) else {},
        coverage=coverage, issues=issues, gate=gate,
        category_source=category_source, category_message=category_message)
    body["draftSaved"] = draft_verified
    body["draftVerified"] = draft_verified
    if standard_report:
        # 让运营看见「这件商品被自动修了什么」，而不是悄悄改掉
        body["standardRepair"] = standard_report
    if isinstance(image_store_report, dict) and image_store_report.get("stored"):
        body["imageStore"] = image_store_report

    # 链接池自动更新（负责人 2026-10-30 明确「老版就是自动的」）：
    # 每次入仓都顺手把商品的 1688 货源链接收进池子，跨店认领/发布要用它重建载体。
    # 失败只记日志——补池是副产品，不能因为补不上就让入仓失败。
    try:
        from services import carrier_service as _carrier
        _added = _carrier.harvest_from_payloads(db, [payload] if isinstance(payload, dict) else [])
        if _added:
            logger.info("入仓自动补充货源链接 detailId=%s 新增=%s", detail_id, _added)
    except Exception as exc:  # noqa: BLE001 - 补池失败不影响入仓
        logger.warning("入仓自动补充货源链接失败 detailId=%s：%s", detail_id, exc)

    return body


def apply_transformed_payload(db: Session, product: Product,
                              payload: dict[str, Any],
                              report: Mapping[str, Any]) -> None:
    """把批量变换的结果写回仓库行（payload + 展示字段同步刷新）。

    只更新受影响的展示字段：标题、价格区间、主图。认领状态、分组、
    类目这些用户手工整理过的东西一律不动——批量改价不该顺带改归属。
    """
    from services import product_service

    product.payload = payload
    if report.get("title"):
        product.title = str(payload.get("title") or product.title or "")
    # 注意 price_min/price_max 是**采购价**区间（originPrice 优先），
    # 批量改价只动 price / priceIncludeVat，采购价没变，不能跟着刷。
    if report.get("images"):
        images = product_service.main_images(payload)
        product.images = images
        product.detail_images = product_service.detail_images(payload)
        if images:
            product.cover = images[0]
    from core.utils import now_ts

    product.updated_at = now_ts()
    # 批量变换（改价/删图/改标题）也是一次写库，同样要推进版本号。
    bump_revision(product)
    db.flush()


def apply_image_limits(payload, *, main_limit=0, detail_limit=0, sku_limit=0):
    """按处理设置真正截断主图 / 详情图 / SKU 图与颜色。

    负责人口径（2026-11-11）：界面上的「主图数量 / 详情图数量 / SKU 图数量」
    是**保留几张**，不是「翻译几张」；SKU 图数量 = 颜色数，设 4 就只保留
    4 个颜色（连带删其余 SKU）。0 表示不限制。
    """
    from services import pre_save_check

    report = {}

    # 主图：先去重再截断。
    #
    # 上游主图里同一张图重复出现很常见（1688 把一张图同时挂在多个位置），
    # 不去重就按「前 N 张」取，重复占位会让真正的第 N 张被挤掉，
    # 且入库时按唯一性去重后张数又变少（2026-11-11 实测：设 9 张、
    # 上游 13 张，截出 9 张里 2 张重复，最终入库只有 7 张）。
    main_raw = [u for u in (payload.get("imgUrls") or []) if isinstance(u, str)]
    main = list(dict.fromkeys(main_raw))
    if len(main) != len(main_raw):
        report["main_dedup"] = {"before": len(main_raw), "after": len(main)}
    if main_limit and main_limit > 0 and len(main) > main_limit:
        report["main"] = {"before": len(main), "after": main_limit}
        main = main[:main_limit]
    payload["imgUrls"] = main

    # 详情图
    if detail_limit and detail_limit > 0:
        detail_raw = pre_save_check.extract_detail_image_urls(payload.get("notes"))
        detail = list(dict.fromkeys(detail_raw))
        if len(detail) != len(detail_raw):
            report["detail_dedup"] = {"before": len(detail_raw), "after": len(detail)}
        if len(detail) > detail_limit:
            report["detail"] = {"before": len(detail), "after": detail_limit}
            detail = detail[:detail_limit]
        payload["notes"] = pre_save_check.detail_images_only(detail)

    # 颜色 / SKU 图（同一维度：一个颜色一张 SKU 图）
    if sku_limit and sku_limit > 0:
        for prop in (payload.get("skuPropertyList") or []):
            if not isinstance(prop, dict):
                continue
            if str(prop.get("attrId") or "") != "100000":
                continue
            values = [v for v in (prop.get("attrValueList") or [])
                      if isinstance(v, dict)]
            if len(values) <= sku_limit:
                break
            kept = values[:sku_limit]
            kept_ids = {str(v.get("attrValueId") or "") for v in kept}
            all_ids = {str(v.get("attrValueId") or "") for v in values}
            report["colors"] = {"before": len(values), "after": len(kept)}
            prop["attrValueList"] = kept
            sku_map = payload.get("skuMap")
            if isinstance(sku_map, dict):
                dropped = 0
                for key in list(sku_map.keys()):
                    parts = [x for x in str(key).split(";") if x]
                    removed = [p for p in parts if p in all_ids and p not in kept_ids]
                    if removed:
                        sku_map.pop(key, None)
                        dropped += 1
                report["sku"] = {"dropped": dropped}
            break
    return report

def clean_detail_images(payload: dict[str, Any], *,
                       limit: int = DETAIL_IMAGE_HARD_LIMIT,
                       rows_by_url: Mapping[str, Mapping[str, Any]] | None = None
                       ) -> dict[str, Any]:
    """详情区只留图片：删文字、删厂家宣传图、删尺码图、去重、截上限。

    负责人口径（2026-11-09）：
      · 详情里除了图片什么都不要——中英文空白、段落文字全部清掉；
      · 厂家宣传图（厂房/招商/联系方式）和尺码对照图，出现在详情图里是废图；
      · 同一张图重复出现只留一张；
      · 最多 limit 张（平台硬上限）。

    OCR 结果由调用方传进来（rows_by_url），拿不到就不做内容判定——
    网络抖动时按原样保留，宁可不删也不误删用户要的图。
    """
    from services import pre_save_check

    urls = pre_save_check.extract_detail_image_urls(payload.get("notes"))
    kept: list[str] = []
    seen: set[str] = set()
    dropped: list[dict[str, str]] = []
    for url in urls:
        cleaned = str(url or "").strip()
        if not cleaned:
            continue
        if cleaned in seen:
            dropped.append({"url": cleaned, "reason": "重复图片"})
            continue
        row = (rows_by_url or {}).get(cleaned) or {}
        action = str(row.get("action") or "")
        if action == "remove":
            dropped.append({"url": cleaned,
                            "reason": str(row.get("reason") or "厂家宣传/尺码图")})
            continue
        seen.add(cleaned)
        if len(kept) >= limit:
            dropped.append({"url": cleaned, "reason": f"超过详情图上限 {limit} 张"})
            continue
        kept.append(cleaned)
    payload["notes"] = pre_save_check.detail_images_only(kept)
    return {"kept": len(kept), "dropped": dropped,
            "before": len(urls), "after": len(kept)}


def analyze_payload_images(payload: dict[str, Any], *, retry: bool = False
                           ) -> dict[str, Any]:
    """对主图 / 详情图 / SKU 图统一做一次 OCR 分类。

    返回 {main: [...], detail: [...], sku: [...]}，每项是 analyze 的原始行。
    三类分开返回是为了让调用方按图位决策：主图删掉后要补位，详情图删掉直接不要，
    SKU 图删掉要重排颜色。
    """
    from services import image_review_service, pre_save_check

    def _rows(urls: list[str]) -> list[dict[str, Any]]:
        urls = [str(u) for u in urls if isinstance(u, str) and u.startswith("http")]
        if not urls:
            return []
        try:
            return image_review_service.analyze(urls, limit=len(urls), retry=retry)
        except Exception as exc:  # noqa: BLE001 - OCR 不可用不阻断入库
            logger.warning("图片 OCR 不可用，跳过内容判定：%s", exc)
            return []

    return {
        "main": _rows(list(payload.get("imgUrls") or [])),
        "detail": _rows(pre_save_check.extract_detail_image_urls(payload.get("notes"))),
        "sku": _rows([item["url"] for item in sku_image_sources(payload)]),
    }


def rows_index(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """OCR 行按 URL 建索引，供清洗函数查。"""
    return {str(row.get("url") or ""): dict(row) for row in rows
            if isinstance(row, Mapping) and row.get("url")}


def _drop_main_images(payload: dict[str, Any], indexes: Iterable[Any]) -> int:
    """按 index 从主图里剔掉工厂图，返回实际删掉的数量。

    从后往前删：闸门给的是「原始下标」，从前删会让后面的下标整体前移，
    第二个要删的就指向了别的图。删完必须保证至少留一张 —— 老版在这里
    抛「清理后主图会全部被删掉」，闸门已拦，这里是最后一道兜底。
    """
    urls = payload.get("imgUrls")
    if not isinstance(urls, list) or not urls:
        return 0
    order = sorted({int(i) for i in indexes if isinstance(i, int) or str(i).isdigit()},
                   reverse=True)
    dropped = 0
    for index in order:
        if 0 <= index < len(urls) and len(urls) > 1:
            del urls[index]
            dropped += 1
    if dropped:
        payload["imgUrls"] = urls
    return dropped


def payload_review(product: Product, payload: Mapping[str, Any], *,
                   coverage: Mapping[str, Any] | None = None,
                   issues: Iterable[Any] | None = None,
                   gate: Mapping[str, Any] | None = None,
                   category_source: str = "",
                   category_message: str = "") -> dict[str, Any]:
    """给运营核对的摘要 + 指纹（老版 `payloadReview`）。

    只放「人能一眼判断对不对」的字段，不塞完整 payload —— 整份 payload 几十 KB，
    塞进列表响应会把首屏拖慢，而且运营也不会逐字段读 JSON。

    `payloadHash` 是确认保存时的唯一凭据：预览时算一次存起来，确认时重算比对。
    中间任何一步改了 payload（图片翻译晚到、上游被改、运营改了模板）都会让
    hash 变，确认就会失败并提示重新核对 —— 这是「核对过的就是存进去的」的保证。
    """
    from services import product_service

    main_urls = product_service.main_images(payload)
    review = {
        "detailId": str(getattr(product, "detail_id", "") or payload.get("itemId") or ""),
        "shopId": str(getattr(product, "shop_id", "") or ""),
        "title": str(payload.get("title") or ""),
        "titleLength": len(str(payload.get("title") or "")),
        "cid": str(payload.get("cid") or ""),
        "weight": payload.get("weight"),
        "packageDimensions": {key: payload.get(key)
                              for key in ("packageLength", "packageWidth", "packageHeight")},
        "mainImageCount": len(main_urls),
        "detailImageCount": len(product_service.detail_images(payload)),
        "skuImageCount": len(sku_image_sources(payload)),
        "colors": [str(item.get("attrValue") or "")
                   for prop in (payload.get("skuPropertyList") or [])
                   if isinstance(prop, dict)
                   for item in (prop.get("attrValueList") or [])
                   if isinstance(item, dict)],
        "sizeChart": str(payload.get("sizeChart") or ""),
        "translationCoverage": dict(coverage or {}),
        "categorySource": category_source,
        "payloadHash": title_save_service.revision_of(payload),
    }
    if category_message:
        review["categoryMessage"] = category_message
    if issues:
        review["issues"] = [str(item) for item in issues]
    if gate:
        review["gate"] = {"blocks": gate.get("blocks") or [],
                          "warnings": gate.get("warnings") or []}
    return review


# 翻译阶段只允许改这些「文案」字段。
# 价格 / 重量 / 尺寸 / 图片 / 规格 id 都是前面几步辛苦算出来的，译文接口
# 回一份整块 info 就把它们覆盖掉，等于白算（实测 translated_info 返回的是
# 上游的完整结构，不只是译文字段）。
_TRANSLATABLE_TEXT_FIELDS = (
    "title", "oriTitle", "description",
    "keyProductFeatures", "productAttributes",
)
# notes（详情区 HTML）**不接译文**：详情区按口径只留图片，清洗已在前一步做完
# （删文字、删厂家图、删尺码图、去重）。接译文等于把上游那份带文字、
# 带厂家图的原始 HTML 又贴回来，前面的清洗全白做。


def merge_translated_fields(payload: dict[str, Any],
                            translated: Mapping[str, Any]) -> dict[str, Any]:
    """把译文字段并回已处理好的 payload，其余字段一律保留。

    为什么不能直接赋值：payload 走到翻译这一步时已经带上了
    「加完增量的重量、按模板算好的售价、截断后的主图、清洗后的颜色 id」，
    译文的返回体是上游原始结构，直接换掉会把前面几步全部作废。
    """
    merged = dict(payload)
    for field in _TRANSLATABLE_TEXT_FIELDS:
        value = translated.get(field)
        if value not in (None, "", [], {}):
            merged[field] = value
    # Spec values join translation; normalize maps them back to draft originals.
    translated_props = translated.get("skuPropertyList")
    if isinstance(translated_props, list):
        merged["skuPropertyList"] = _merge_spec_translation(
            payload.get("skuPropertyList") or [], translated_props)
    return merged


# 维度语义归类：颜色/尺码，兼容中英马三语 + 上游 attrId。
_COLOR_DIM_NAMES = ("颜色", "色彩", "color", "colour", "warna")
# 「规格」在供应商草稿里几乎总是尺码维度（M/L/XL、34-36 这种），
# 但也可能是任意属性。不把它归到尺码，妙手补完 attrId 后两个维度都叫
# Warna，颜色清洗会直接报「商品有 2 个颜色属性，无法确定用哪一个」
# （2026-11-12 实测：3527583908 颜色 5 个 + 规格 4 个 → 整件失败）。
_SIZE_DIM_NAMES = ("尺码", "尺寸", "规格", "size", "saiz", "ukuran", "spesifikasi")
_COLOR_DIM_IDS = {"100000"}
_SIZE_DIM_IDS = {"100007"}


def _spec_kind(name: str, attr_id: str) -> str:
    folded = name.strip().casefold().replace(" ", "")
    if attr_id in _COLOR_DIM_IDS or folded in _COLOR_DIM_NAMES:
        return "color"
    if attr_id in _SIZE_DIM_IDS or folded in _SIZE_DIM_NAMES:
        return "size"
    return ""


def _merge_spec_translation(current_props, translated_props):
    """Merge translated spec values; keep attrValueId / attrId intact."""
    if not isinstance(current_props, list) or not isinstance(translated_props, list):
        return current_props
    merged = deepcopy(current_props)
    for cur in merged:
        if not isinstance(cur, dict):
            continue
        cur_dim = str(cur.get("attrId") or "").strip()
        cur_name = str(cur.get("attrName") or "").strip().casefold()
        # 维度语义判定：翻译接口返回的维度名可能已经变成 Warna/Saiz（英文），
        # 而当前是 颜色/尺码（中文），attrId 也被上游剥掉，两个常规条件全对不上。
        # 按语义归类兜底，否则译文规格永远合并不进来（2026-11-10 实测）。
        cur_kind = _spec_kind(cur_name, cur_dim)
        twin = None
        for tp in translated_props:
            if not isinstance(tp, dict):
                continue
            t_dim = str(tp.get("attrId") or "").strip()
            t_name = str(tp.get("attrName") or "").strip().casefold()
            if cur_dim and t_dim == cur_dim:
                twin = tp
                break
            if not cur_dim and t_name == cur_name:
                twin = tp
                break
            if cur_kind and _spec_kind(t_name, t_dim) == cur_kind:
                twin = tp
                break
        if twin is None:
            continue
        cur_values = [v for v in (cur.get("attrValueList") or []) if isinstance(v, dict)]
        twin_values = [v for v in (twin.get("attrValueList") or []) if isinstance(v, dict)]
        if not cur_values or not twin_values:
            continue
        if len(cur_values) == len(twin_values):
            # 正常情况：两边一一对应，按顺序覆盖
            for cv, tv in zip(cur_values, twin_values):
                label = str(tv.get("attrValue") or "").strip()
                if label:
                    cv["attrValue"] = label
        else:
            # 数量不等：翻译接口有时只返回部分值（实测返回 1 个、本地 5 个），
            # 直接跳过会导致「只有第一个 SKU 翻译了、其余仍是中文」
            # （2026-11-12 负责人指出）。
            # 不能按位置硬配（会张冠李戴），改成按 attrValueId 匹配，
            # 匹配不到的保持中文 —— 宁可少翻，也不能把颜色配错。
            by_id = {str(tv.get("attrValueId") or ""): tv for tv in twin_values}
            for cv in cur_values:
                vid = str(cv.get("attrValueId") or "")
                tv = by_id.get(vid) if vid else None
                if tv is None:
                    continue
                label = str(tv.get("attrValue") or "").strip()
                if label:
                    cv["attrValue"] = label
    return merged


def _needs_translation(payload: Mapping[str, Any], language: str) -> bool:
    """要不要翻：目标语言为空/中文时不翻，省一次上游调用。"""
    # 延迟导入：product_service 在模块顶层导入会与本文件形成循环
    # （product_service 也 import warehouse_service）。
    from services import product_service as _product_service

    target = str(language or _product_service.DEFAULT_LANGUAGE).strip().lower()
    if target in ("", "zh", "zh-cn", "cn", "chinese"):
        return False
    return True


def _fill_spec_attr_ids(db: Session, client: Any, payload: dict[str, Any], *,
                        shop_id: Any, site: str) -> dict[str, str]:
    """给颜色/尺码维度补类目真实 attrId 与属性名。

    妙手保存草稿的两种行为：
      · attrId 齐全 → 按提交的规格重建整张规格表（我们清洗过的值才能生效）
      · attrId 为空 → 按草稿里原有的规格值逐字匹配，清洗过的值一律报
        「SKU【xxx-41】不存在」
    所以清洗颜色之前必须先补 attrId，否则清得再对也存不进去。
    """
    import time as _time

    from services import carrier_service

    cid = str(payload.get("cid") or "").strip()
    if not cid:
        return {}
    attrs: dict[str, str] = {}
    # 限流必须重试：拿不到 attrId，颜色清洗就会在没有 attrId 的状态下改值改 id，
    # 妙手按草稿原有规格逐字匹配，必然报「SKU【米色-41】不存在」。
    # 实测这条接口批量处理时常回「请求太频繁」，退避重试即可。
    for attempt in range(3):
        try:
            attrs = carrier_service.category_sale_attrs(
                client, cid, site or "MY", shop_id, strict=True)
        except Exception as exc:  # noqa: BLE001 - 拿不到就保持原样，不清洗照旧
            logger.warning("读取类目规格 attrId 失败 cid=%s（第 %d/3 次）：%s",
                           cid, attempt + 1, exc)
            attrs = {}
        if attrs:
            break
        if attempt < 2:
            _time.sleep(1.5 * (attempt + 1))
    if not attrs:
        return {}
    notes: list[str] = []
    for prop in (payload.get("skuPropertyList") or []):
        if not isinstance(prop, dict):
            continue
        name = str(prop.get("attrName") or prop.get("propName") or "").casefold()
        values = [str(av.get("attrValue") or "") for av in (prop.get("attrValueList") or [])
                  if isinstance(av, dict)]
        # 按属性名判断维度；名字认不出时按取值形态判断（纯数字/区间是尺码）。
        # 「规格」必须算尺码：供应商常写「颜色 + 规格」两维，规格里是
        # M/L/XL 或 34-36。不认它的话两个维度都会落到下面的 elif 分支，
        # 双双被赋成颜色 attrId 100000，随后颜色清洗报
        # 「商品有 2 个颜色属性（Warna、Warna），无法确定用哪一个」
        # （2026-11-12 实测：3527583908 整件失败）。
        is_size = ("size" in name or "saiz" in name or "尺码" in name
                   or "尺寸" in name or "规格" in name or "spesifikasi" in name)
        is_color = ("color" in name or "colour" in name or "warna" in name
                    or "颜色" in name or "色彩" in name)
        if not is_size and not is_color:
            size_like = sum(1 for v in values if v and v.replace("-", "").isdigit())
            is_size = bool(values) and size_like * 2 >= len(values)
        if is_size and attrs.get("size"):
            if str(prop.get("attrId") or "") != str(attrs["size"]):
                prop["attrId"] = attrs["size"]
                notes.append("尺码 attrId→%s" % attrs["size"])
            if attrs.get("sizeName") and prop.get("attrName") != attrs["sizeName"]:
                prop["attrName"] = attrs["sizeName"]
        elif not is_size and attrs.get("color"):
            if str(prop.get("attrId") or "") != str(attrs["color"]):
                prop["attrId"] = attrs["color"]
                notes.append("颜色 attrId→%s" % attrs["color"])
            if attrs.get("colorName") and prop.get("attrName") != attrs["colorName"]:
                prop["attrName"] = attrs["colorName"]
    # 返回 attrId 字典而不是 notes：调用方要靠「有没有拿到 attrId」决定
    # 能不能清洗颜色（见 save_from_collect_box）。返回空列表会被当成
    # 「没拿到」，颜色清洗被跳过，规格值原样提交反而正确 —— 但那样
    # 就永远清不了颜色。
    return attrs


# 只给本地展示用的字段，绝不能发给妙手。
# colorMap / sizeMap / colorPropName / sizePropName 是 build_product 写进 payload
# 的展示快照（详情页用），妙手收到会拿去解析规格，报
# 「SKU【白黑色（A19）-36】不存在」——看起来像颜色对不上，实际是多余字段
# 干扰了它的规格解析（2026-11-09 实测：同一份数据去掉这四个字段就能存进去）。
_LOCAL_ONLY_PAYLOAD_FIELDS = (
    "colorMap", "sizeMap", "colorPropName", "sizePropName",
    "_sourceWeight", "weightAutoFixed", "weightBeforeCap",
    "translation", "violations", "removedWords", "needsRecollect",
    "needsRecollectReason",
)


def _strip_local_only_fields(payload: Mapping[str, Any]) -> dict[str, Any]:
    """返回剔除了「只服务本地展示」字段的副本；原对象不动。

    为什么返回副本而不是就地删：这些字段（translation / colorMap / sizeMap …）
    要留在数据库里给详情页与译文核对用，只是**不能发给妙手**。
    就地删会让入库后的商品永久丢掉它们。
    """
    return {key: value for key, value in payload.items()
            if key not in _LOCAL_ONLY_PAYLOAD_FIELDS}


def _restore_sku_key_order(payload: dict[str, Any],
                           original: Mapping[str, Any]) -> int:
    """把 skuMap 的键顺序恢复成妙手草稿里的顺序。

    妙手保存草稿时按**它自己那份草稿的键**逐字匹配：草稿写「分号尺码id分号
    颜色id分号」，我们提交「分号颜色id分号尺码id分号」就是两个不同的 SKU，
    平台报「SKU 不存在」。同一批采集数据里两种顺序都有，所以不能统一排序，
    只能跟着草稿走（2026-11-09 实测：产物与草稿的规格、id、值完全一致，
    唯一差别是键顺序，把顺序改回草稿顺序就保存成功）。

    段集合相同、只是顺序不同的键做映射；其余键保持原样。
    """
    before = payload.get("skuMap")
    if not isinstance(before, dict) or not before:
        return 0
    draft = original.get("skuMap") if isinstance(original, Mapping) else None
    if not isinstance(draft, dict) or not draft:
        return 0
    by_signature: dict[frozenset[str], str] = {}
    for key in draft:
        parts = frozenset(part for part in str(key).strip(";").split(";") if part)
        if parts:
            by_signature.setdefault(parts, str(key))
    restored: dict[str, Any] = {}
    changed = 0
    for key, value in before.items():
        parts = frozenset(part for part in str(key).strip(";").split(";") if part)
        draft_key = by_signature.get(parts)
        if draft_key and draft_key != key:
            restored[draft_key] = value
            changed += 1
        else:
            restored[key] = value
    if changed:
        payload["skuMap"] = restored
    return changed


def _strip_null_sku_fields(payload: dict[str, Any]) -> int:
    """删掉 skuMap 里值为 None 的字段。

    妙手对 null 的处理是「字段存在但不合法」，不是「未设置」：
    itemNum=null / identifierCode=null 会让规格解析失败，
    整单报「SKU【颜色-尺码】不存在」，排查起来会误以为是颜色值不对。
    值为空串的保留（平台认它是「没填」）。
    """
    removed = 0
    sku_map = payload.get("skuMap")
    if not isinstance(sku_map, dict):
        return 0
    for key, sku in list(sku_map.items()):
        if not isinstance(sku, dict):
            continue
        cleaned = {field: value for field, value in sku.items() if value is not None}
        removed += len(sku) - len(cleaned)
        sku_map[key] = cleaned
    return removed


def _clean_size_values(payload: dict[str, Any]) -> int:
    """把尺码维度里「M（34-36）运动鞋建议拍大一码」这类值截成 M（34-36）。

    供应商在尺码值里塞销售建议，翻译成马来语后必然超过妙手 50 字符硬限：
      「Kasut sukan M(34-36) disarankan untuk memilih saiz satu nombor
        lebih besar.」长度超过50个字符
    （2026-11-12 实测：3527583908 整件失败）。

    只保留「码数本体」：括号里的尺码区间、字母码、纯数字。
    返回改动的条数。
    """
    import re as _re

    changed = 0
    for prop in (payload.get("skuPropertyList") or []):
        if not isinstance(prop, dict):
            continue
        if _spec_kind(str(prop.get("attrName") or ""),
                      str(prop.get("attrId") or "")) != "size":
            continue
        for av in (prop.get("attrValueList") or []):
            if not isinstance(av, dict):
                continue
            raw = str(av.get("attrValue") or "").strip()
            if not raw:
                continue
            # 「M（34-36）运动鞋建议拍大一码」→「M（34-36）」
            # 「L(37-39) 建议拍大一码」      →「L(37-39)」
            # 「2XL（43-45）…」              →「2XL（43-45）」
            m = _re.match(r"^([0-9A-Za-z]+)\s*[（(]\s*([0-9]+\s*[-–~]\s*[0-9]+)\s*[）)]",
                          raw)
            if m:
                cleaned = "%s(%s)" % (m.group(1), m.group(2).replace(" ", ""))
            else:
                # 「40」「XL」「40-41」这种本来就干净的保留；
                # 其余取第一个空白前 / 第一个中文前的片段
                cleaned = _re.split(r"[\s\u4e00-\u9fff]", raw, maxsplit=1)[0].strip()
            if cleaned and cleaned != raw and len(cleaned) <= 20:
                av["attrValue"] = cleaned
                changed += 1
    return changed

def _clean_color_props(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """就地规范化颜色属性值，返回清洗后的 skuPropertyList。

    走 color_service.normalize_colors（照搬老版 shoe_workflow.normalize_colors），
    而不是逐个值调 clean_color：老版这一步除了收敛写法，还做四件清洗器不做的事：
      1. 颜色属性必须恰好 1 个，0 个或多个都报根因（颜色和尺码被压成同一属性时，
         逐个改值看着成功，实际 SKU 键根本对不上）；
      2. attrValueId 必须齐全且不重复 —— 重复 id 会让两个颜色共用一个 SKU 键；
      3. 同名颜色去重（「白色」「白色 」「白」是同一个色）；
      4. 删掉的颜色同步剪掉 skuMap 里对应的 SKU 键，否则上架时颜色选不到货。
    这些在老版都是抛错即停，不是尽力而为。清理后会删空所有在售 SKU 的，
    也在入仓前拦掉 —— 那种商品上架就是零库存。
    """
    props = payload.get("skuPropertyList") or []
    if not isinstance(props, list):
        return []
    from services import color_service

    try:
        # 传整个 payload：normalize_colors 要同时改 skuPropertyList 和 skuMap，
        # 只传属性列表就剪不掉 SKU 键。
        result = color_service.normalize_colors(payload)
    except color_service.ColorError as exc:
        raise errors.unprocessable(f"颜色处理未通过：{exc}") from None
    cleaned = result.get("properties")
    if isinstance(cleaned, list):
        payload["skuPropertyList"] = cleaned
        return cleaned
    payload["skuPropertyList"] = props
    return props


def _shop_name(db: Session, account: Account, shop_id: Any) -> str:
    from services.account_service import cached_shops

    for shop in cached_shops(account):
        if str(shop.get("shopId")) == str(shop_id):
            return str(shop.get("name") or "")
    from models.shop import Shop

    row = (db.query(Shop)
           .filter(Shop.account_id == account.id, Shop.shop_id == str(shop_id))
           .one_or_none())
    return row.name if row else ""


def _save_processed_draft(client: MiaoshouClient, *, detail_id: str, shop_id: str,
                         oss_md5: str, payload: Mapping[str, Any],
                         original: Mapping[str, Any],
                         coverage: Mapping[str, Any],
                         pricing_enabled: bool) -> None:
    """Write the processed product to Miaoshou and fail closed on readback mismatch."""
    from services import product_service, readback_check

    # 规格值必须与妙手草稿原文逐字一致。
    #
    # 我们把颜色清洗成「白绿色」，草稿里存的是「566【白绿色】」；妙手
    # save_shop 按草稿原文匹配规格值，提交清洗值直接报
    # 「SKU【白绿色-41】不存在」（2026-11-10 实测：3522040694 反复失败）。
    # carrier_service.normalize_spec_for_save 早就实现了「对回草稿原文 +
    # 原 attrValueId + 重建 skuMap」，但那条路径只给学员认领发布用，
    # 自动化上品走的是这里，所以一直没生效。这里补上同一套处理。
    from services import carrier_service

    upstream_payload: Mapping[str, Any] = payload
    try:
        upstream_payload, _spec_notes = carrier_service.normalize_spec_for_save(
            dict(payload), dict(original))
    except Exception as exc:  # noqa: BLE001 - 规范化失败退回原样，交给妙手判
        logger.warning("规格规范化失败 detailId=%s：%s", detail_id, exc)
        upstream_payload = payload
    # 重量还原：妙手草稿永远保存「采集原重」，不加增量。
    #
    # 增量是我们本地的运费口径（运费按最终包裹重量计费），只体现在成品仓
    # 的记录里。如果把它写回妙手草稿，下次处理时草稿重量已是「原重+增量」，
    # 再加一次就变 1.6 -> 1.7 -> 1.8 无限上涨，最后被原重上限拦死
    # （2026-11-10 实测：3522040694 重量 1.6 -> 1.7 反复失败）。
    try:
        _src = str((payload.get("_sourceWeight") or "") or "").strip()
        if _src:
            try:
                _src_val = float(_src)
                if _src_val > 0:
                    upstream_payload = dict(upstream_payload)
                    upstream_payload["weight"] = _src_val
                    for _k, _sku in (upstream_payload.get("skuMap") or {}).items():
                        if isinstance(_sku, dict) and str(_sku.get("isDelete", "0")) != "1":
                            _sku["weight"] = _src_val
            except ValueError:
                pass
        client.save_shop(detail_id, shop_id, oss_md5, upstream_payload)
        saved = client.shop_detail(detail_id, shop_id)
    except MiaoshouError as exc:
        from services.account_service import map_upstream_error

        raise map_upstream_error(exc) from None
    actual = product_service.detail_block(saved)["info"]
    if not actual:
        raise errors.AppError(
            errors.UPSTREAM_UNKNOWN_RESULT,
            "妙手已接收保存请求，但无法读回商品详情；已阻止后续发布，请刷新核对",
            status=502,
        )
    # 校验基准必须是**实际提交给妙手的那份**（upstream_payload），
    # 不是规范化之前的 payload：规格值对回草稿原文后，payload 里的
    # 清洗值（白绿色）与读回值（566【白绿色】）必然"不一致"，
    # 拿它当基准等于每件带型号前缀的商品都过不了核验
    # （2026-11-10 实测：3522040694 提交 upstream 后仍报 weight/prices 不一致）。
    mismatches = readback_check.readback_issues(
        upstream_payload, actual, original, coverage, pricing_enabled=pricing_enabled,
    )
    if mismatches:
        # 明细必须带出去：只报一句「不一致」没法定位（2026-11-11 实测：
        # 同一句话连报多天，没人知道是 weight 还是 prices 还是规格）。
        try:
            rows = readback_check.details(upstream_payload, actual, original,
                                          coverage, pricing_enabled=pricing_enabled)
        except Exception:
            rows = []
        logger.error("读回不一致 detailId=%s：%s", detail_id, mismatches)
        raise errors.AppError(
            errors.UPSTREAM_UNKNOWN_RESULT,
            "妙手草稿读回与处理结果不一致，已阻止后续发布",
            status=502,
            details={"mismatches": mismatches, "rows": rows},
        )


def count(db: Session) -> int:
    return active_query(db).count()


def over_limit(db: Session) -> int:
    """超过上限多少件；没超返回 0。给界面提示用，**不改数据**。"""
    return max(0, count(db) - WAREHOUSE_MAX_ITEMS)


def trim_overflow(db: Session, *, confirm: bool = False) -> int:
    """仓库上限保护：超出 WAREHOUSE_MAX_ITEMS 时归档最旧的商品。

    ⚠️ **不要在任何自动路径（保存 / 入仓 / 批次 / 定时任务）里调用它。**

    曾经它写了却没接线，看起来是漏了一个调用；实际接线前必须想清楚它在做什么：
    `status="archived"` + `deleted_at` 就是**软删除**，`active_query` 之后再也查不到。
    一旦挂到入仓路径上，第 2001 件商品入仓时，用户最旧的那件会**静默消失**——
    界面上没有任何提示，只有「这件去哪了」的困惑，而过几天他也不会记得
    2000 这个数字。这比「仓库没有上限」严重得多：没有上限只是慢，
    静默删用户的东西是数据事故。

    所以上限的落地方式是「让人知道，让人决定」：
      · `stats()` 返回 maxProducts / remaining / nearLimit，快满时界面提示；
      · 用户自己决定清理哪些（批量删除是现成功能，且删除是他按的）。
    这个函数只留 `confirm=True` 的显式入口，给「我确实要清到 2000 以内」用。
    """
    if not confirm:
        # 默认拒绝执行。写成显式的 False 而不是省略参数，是为了让调用方
        # 在 code review 里一眼看到「这里没确认」而不是「这里没传参」。
        raise errors.validation(
            "trim_overflow 会归档（软删除）最旧的商品，必须在确认后显式传 confirm=True",
            field="confirm")
    total = count(db)
    if total <= WAREHOUSE_MAX_ITEMS:
        return 0
    overflow = total - WAREHOUSE_MAX_ITEMS
    rows = (active_query(db).order_by(Product.saved_at.asc()).limit(overflow).all())
    stamp = now_ts()
    for row in rows:
        row.deleted_at = stamp
        row.status = "archived"
    db.flush()
    return len(rows)


def group_counts(db: Session, *, operator_id: int | None = None) -> dict[int, int]:
    visible = _scope_member_products(db, active_query(db), operator_id)
    rows = (visible.filter(Product.group_id.is_not(None))
            .with_entities(Product.group_id, func.count(Product.id))
            .group_by(Product.group_id).all())
    return {int(group_id): int(total) for group_id, total in rows if group_id is not None}


# --- 货源 offer id 汇总（老版 warehouse.all_source_item_ids）----------------
# 成品 payload 只留 oriTitle 之类的展示字段，原始 1688 链接早就没了；重建载体
# 时要拿 offer id 去 1688 重新抓货，否则就得整批重新采集一遍。id 就藏在 payload
# 里几个历史遗留字段上，这里全部抠出来。
SOURCE_ITEM_ID_FIELDS = ("sourceItemId", "oriItemId")


def all_source_item_ids(db: Session, account_id: Any = None) -> list[str]:
    """所有已存商品的 payload 里出现过的 1688 offer id，去重保序。

    只认纯数字（老版 `.isdigit()` 同一口径）：上游偶尔把商品链接或标题写进
    这些字段，非数字拿去做请求参数只会白白打一趟 1688。

    `account_id` 传了就只看那个账号的商品——载体重建是按账号来的。
    """
    query = active_query(db)
    if account_id not in (None, ""):
        text = str(account_id)
        if text.isdigit():
            query = query.filter(Product.account_id == int(text))
        else:
            # 账号标识写错时宁可空手而归，也不要返回全部账号的 id 混在一起
            return []
    # 按入仓顺序扫：去重保序的结果才稳定，重建时的抓取顺序也就稳定
    rows = (query.order_by(Product.saved_at.asc(), Product.id.asc())
            .with_entities(Product.payload).all())
    ids: list[str] = []
    for (payload,) in rows:
        if not isinstance(payload, Mapping):
            continue
        sources = payload.get("sourceList")
        if isinstance(sources, list):
            for source in sources:
                if not isinstance(source, Mapping):
                    continue
                value = str(source.get("sourceItemId") or "").strip()
                if value.isdigit():
                    ids.append(value)
        for key in SOURCE_ITEM_ID_FIELDS:
            value = str(payload.get(key) or "").strip()
            if value.isdigit():
                ids.append(value)
    return list(dict.fromkeys(ids))

