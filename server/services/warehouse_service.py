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

from sqlalchemy import func, or_, select
from sqlalchemy import update as sql_update
from sqlalchemy.orm import Session

from core import errors
from core.constants import (
    DETAIL_IMAGE_HARD_LIMIT,
    MAIN_IMAGE_LIMIT,
    NEAR_LIMIT_RATIO,
    WAREHOUSE_MAX_ITEMS,
)
from core.utils import (
    as_id_str,
    money,
    now_ts,
    product_key,
)
from models.account import Account
from models.category import Category
from models.group import Group
from models.image_job import ImageJob
from models.product import Product
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

logger = logging.getLogger(__name__)


# --- 序列化 ---------------------------------------------------------------
def serialize(product: Product, *, account_name: str = "",
              include_payload: bool = False, operator_name: str = "") -> dict[str, Any]:
    images = product.images if isinstance(product.images, list) else []
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
        "claimed": bool(product.claimed),
        "claimedAt": product.claimed_at,
        "claimedShopId": product.claimed_shop_id,
        "collected": product.collected if isinstance(product.collected, dict) else {},
        "salesLast30": int(product.sales_last_30 or 0),
        "stock": int(product.stock or 0),
        "status": product.status or "ready",
        "skuProperties": product.sku_properties if isinstance(product.sku_properties, list) else [],
    }
    if include_payload:
        body["payload"] = product.payload if isinstance(product.payload, dict) else {}
    return body


def account_names(db: Session) -> dict[int, str]:
    return {row.id: row.name for row in db.query(Account).all()}


def operator_names(db: Session) -> dict[int, str]:
    """operators.id → 显示名，供列表页一次性取名。

    一次查全表而不是每行 join：操作员就几个人，几十行；而列表页有 24~100 行
    商品，逐行查会把一次查询变成上百次。
    """
    from models.operator import Operator

    return {row.id: (row.name or "") for row in db.query(Operator).all()}


def active_query(db: Session):
    return db.query(Product).filter(Product.deleted_at.is_(None))


def resolve_product(db: Session, ident: Any) -> Product:
    """按数字 id 或 `{detailId}:{shopId}` 取商品；软删除视为不存在。"""
    text = str(ident or "").strip()
    if not text:
        raise errors.validation("商品标识不能为空", field="id")
    query = active_query(db)
    if text.isdigit():
        product = query.filter(Product.id == int(text)).one_or_none()
    else:
        product = query.filter(Product.warehouse_key == text).one_or_none()
    if product is None:
        raise errors.not_found(f"仓库里没有商品 {text}", id=text)
    return product


# --- 列表 -----------------------------------------------------------------
def list_products(db: Session, *, page: int = 1, page_size: int = 24,
                  category_id: Any = None, cid: Any = None, group_id: Any = None,
                  group: str | None = None, q: str | None = None,
                  account_id: Any = None, status: str | None = None,
                  claimed: bool | None = None, sort: str = "savedDesc"
                  ) -> dict[str, Any]:
    if page < 1:
        raise errors.validation("页码必须不小于 1", field="page", value=page)
    if not 1 <= page_size <= 100:
        raise errors.validation("每页数量必须在 1 到 100 之间", field="pageSize", value=page_size)
    if sort not in SORTS:
        raise errors.validation("排序方式无效", field="sort", value=sort)
    if status is not None and status not in STATUSES:
        raise errors.validation("状态筛选无效", field="status", value=status)
    if q is not None and len(str(q)) > 64:
        raise errors.validation("搜索词不能超过 64 个字符", field="q")

    query = active_query(db)
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
    if account_id not in (None, ""):
        text = str(account_id)
        if text.isdigit():
            query = query.filter(Product.account_id == int(text))
    if status:
        query = query.filter(Product.status == status)
    if claimed is not None:
        query = query.filter(Product.claimed.is_(bool(claimed)))
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
    items = [serialize(row, account_name=names.get(row.account_id or 0, ""),
                       operator_name=operators.get(row.operator_id or 0, ""))
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
        "facets": facets(db),
        "stats": stats(db, account_id=stats_account_id),
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


def facets(db: Session) -> dict[str, Any]:
    """筛选面板计数：分组优先，未分组的商品才计入平台类目。

    运营自定义标签（`warehouse.customCategories`）也是类目维度的一条：商品的
    `categoryName` 等于某个自定义标签时计入该标签桶，`cid` 留空——自定义标签
    本来就没有平台 cid，塞个假 cid 会让前端拿去调类目接口。
    """
    rows = (active_query(db)
            .with_entities(Product.group_id, Product.group_name, Product.cid,
                           Product.category_id, Product.category_name)
            .all())
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
    for (acc_id,) in active_query(db).with_entities(Product.account_id).all():
        account_counts[acc_id] = account_counts.get(acc_id, 0) + 1
    names = account_names(db)
    account_list = [
        {"accountId": str(acc_id) if acc_id else None,
         "name": names.get(acc_id or 0, "未标注"),
         "count": count}
        for acc_id, count in sorted(account_counts.items(), key=lambda item: -item[1])
    ]
    return {"categories": category_list, "groups": group_list, "accounts": account_list}


def stats(db: Session, *, account_id: int | None = None) -> dict[str, Any]:
    """仓库统计。传 account_id 时只数当前账号的商品——
    多账号共用一台机器时，「已认领 355 件」是全账号加总，用户分不清哪些是自己的。"""
    base = active_query(db)
    if account_id is not None:
        base = base.filter(Product.account_id == account_id)
    total = base.count()
    return {
        "totalProducts": total,
        "totalClaimed": base.filter(Product.claimed.is_(True)).count(),
        "totalPublished": base.filter(Product.status == "published").count(),
        # 容量信息一起给出，界面才能在快满时提示。
        # 这是 `trim_overflow` 的替代方案，理由见那个函数的 docstring。
        "maxProducts": WAREHOUSE_MAX_ITEMS,
        "remaining": max(0, WAREHOUSE_MAX_ITEMS - total),
        "nearLimit": total >= WAREHOUSE_MAX_ITEMS * NEAR_LIMIT_RATIO,
    }


# --- 详情 -----------------------------------------------------------------
def detail(db: Session, ident: Any, *, include_payload: bool = False) -> dict[str, Any]:
    product = resolve_product(db, ident)
    names = account_names(db)
    operators = operator_names(db)
    body = serialize(product, account_name=names.get(product.account_id or 0, ""),
                     include_payload=include_payload,
                     operator_name=operators.get(product.operator_id or 0, ""))
    history = (db.query(PublishLog).filter(PublishLog.product_id == product.id)
               .order_by(PublishLog.requested_at.desc()).limit(20).all())
    jobs = (db.query(ImageJob).filter(ImageJob.product_id == product.id)
            .order_by(ImageJob.created_at.desc()).limit(20).all())
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
                                        operator_id=operator_id)
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


def apply_image_results(db: Session, product: Product, payload: Any, *,
                        uploader: Any = None) -> dict[str, Any]:
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
        jobs = (db.query(ImageJob)
                .filter(ImageJob.product_id == product_id)
                .order_by(ImageJob.created_at.asc())
                .all())
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

def _workflow_setting(db: Session, key: str, fallback: Any) -> Any:
    """读工作流设置里的一个键；读不到就用 fallback，不因设置缺失而报错。"""
    from services import setting_service

    try:
        value = setting_service.workflow_defaults(db).get(key)
    except Exception:  # noqa: BLE001 - 设置读坏了不该挡住入仓
        return fallback
    return fallback if value is None else value


WEIGHT_RANGE = (0.05, 5.0)
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


def prepare_weight(info: dict[str, Any], increment: Any = 0.1) -> dict[str, Any]:
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

    low, high = WEIGHT_RANGE
    parsed: dict[str, float] = {}
    for name, sku in targets:
        raw = info.get("weight") if sku is None else sku.get("weight")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise errors.unprocessable(
                "商品重量无法识别，已跳过保存",
                details={"weightError": {"field": name, "value": raw}},
            ) from None
        if not (value == value) or value in (float("inf"), float("-inf")):
            raise errors.unprocessable("商品重量异常，已跳过保存",
                                       details={"weightError": {"field": name, "value": raw}})
        if not low <= value <= high:
            raise errors.unprocessable(
                f"商品重量异常（需要在 {low} 至 {high} kg 范围内），已跳过保存",
                details={"weightError": {"field": name, "value": raw,
                                         "reason": "out_of_range"}},
            )
        parsed[name] = value

    info["weight"] = round(parsed["weight"] + amount, 6)
    for name, sku in targets[1:]:
        if sku is not None:
            sku["weight"] = round(parsed[name] + amount, 6)
    return {"before": before, "after": info["weight"], "increment": amount}


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
                          operator_id: int | None = None) -> dict[str, Any]:
    """拉详情 → 按模板定价（可选）→ 入库（主图截断 9 张）。

    `allow_title_review` / `allow_image_review` 是人工复核的确认开关：老版默认
    抛错停下等人看，确认过才放行。`skip_image_review` 给批量路径用 —— 主图 OCR
    一件最多 9 张、冷跑要几十秒，批量入仓时按需开。

    `operator_id` 只写进「最后改的人」那一列，不参与任何判定（这不是权限系统）。
    定时任务、批量重试这些没有操作员上下文的调用方传 None，列就是空的——
    空着比编一个名字诚实。
    """
    from services import product_service

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

    # 先翻译再定价：价格字段必须原样保留，翻译不能覆盖金额
    translated, translation_report = product_service.translated_info(client, info, language)
    # 逐字段覆盖检查：只翻译了几个字段不代表翻译完成，漏翻的字段会在报告里列出。
    # 老版靠这个阻止「中文还在标题里就上架」（product_workflow.translation_coverage）。
    coverage = translation_coverage.translation_coverage(
        info, translated, target_language=str(language or "ms"))
    translation_report = {**translation_report, "coverage": coverage}
    info = translated

    template = None
    if price_template:
        raw = setting_service.price_templates(db).get(str(price_template))
        if not raw:
            raise errors.AppError(errors.TEMPLATE_INVALID,
                                  f"定价模板「{price_template}」不存在",
                                  status=422, details={"field": "template"})
        template = raw
    payload, _report = product_service.process_price(info, template=template,
                                                     exchange_rate=exchange_rate)

    # 加增量之前先看这件商品是不是已经处理过。
    # prepare_weight 不幂等：同一件重复入仓会一次次 +0.1，重量越漂越远。
    # 老版靠 run_records 比对上次保存结果来拦（「已处理商品与上次保存结果
    # 不一致，请在妙手核对，避免重复增加重量」），这里用现成的仓库行做同等检查。
    existing = (db.query(Product)
                .filter(Product.warehouse_key == product_key(detail_id, shop_id))
                .one_or_none())
    already_processed = bool(existing is not None and existing.saved_at)

    increment = _workflow_setting(db, "weightIncrement", 0.1)
    if already_processed and existing is not None:
        # 已入过仓：沿用上次存下的重量，不再加增量。
        # 上游原始重量确实变了（换了规格）才重新算一次。
        previous = existing.payload if isinstance(existing.payload, dict) else {}
        saved_source = str(previous.get("_sourceWeight") or "")
        if not saved_source:
            # 旧数据没有记账：把上次存下的重量减回增量，还原成上游原值再比。
            # 否则空串 vs 0.75 恒判「变了」，又加一次增量。
            try:
                back = float(previous.get("weight") or 0) - float(increment)
                saved_source = str(round(back, 6)) if back > 0 else ""
            except (TypeError, ValueError):
                saved_source = ""
        raw_changed = saved_source != str(_raw_weight_of(payload))
        if not raw_changed:
            _copy_saved_weights(payload, previous)
            weight_report = {"before": previous.get("weight"),
                             "after": payload.get("weight"),
                             "increment": 0, "reused": True}
            # 原始重量沿用上次记下的，不能被这次算出来的值覆盖：
            # 覆写之后下次比较的就是「加过增量的值」，等价于永远判定没变。
            payload["_sourceWeight"] = previous.get("_sourceWeight")
        else:
            weight_report = prepare_weight(payload, increment)
            payload["_sourceWeight"] = _raw_weight_of(payload, pre_increment=weight_report)
    else:
        weight_report = prepare_weight(payload, increment)
        payload["_sourceWeight"] = _raw_weight_of(payload, pre_increment=weight_report)
    dimensions = normalize_package_dimensions(
        _workflow_setting(db, "packageDimensions", None))
    payload.update(dimensions)
    chart_url = normalize_size_chart_url(_workflow_setting(db, "sizeChartUrl", ""))
    if chart_url:
        payload["sizeChart"] = chart_url
        payload["sizeChartType"] = "image"

    # 类目三档兜底（老版 product_workflow._resolve_category）。没 cid 时以前只报
    # 一句「category cid is missing」，运营不知道下一步该干什么，整批卡在这里。
    # 老版会依次试 AI 匹配、本地类目库名字反查、关键词兜底，三档全失败才提示
    # 去妙手手动选 —— 提示语明确说了「没选类目时颜色、尺码、重量都不会正常显示」，
    # 这句话本身就是操作指引，不能省。
    cid = str(payload.get("cid") or "").strip()
    category_source = "existing" if cid else ""
    category_message = ""
    if not cid:
        try:
            resolved = category_service.resolve_category(
                db, client=client, detail_id=detail_id, shop_id=shop_id,
                info=payload, account_id=account.id,
                site=str(info.get("site") or ""))
        except errors.AppError as exc:
            # AI 匹配不唯一是「需要人做决定」，不是「系统坏了」：转成提示继续走，
            # 让 pre_save 的规则 2 报出来，别把整件商品的处理链打断。
            resolved = {"cid": "", "source": "", "message": exc.message}
        cid = str(resolved.get("cid") or "").strip()
        category_source = str(resolved.get("source") or "")
        category_message = str(resolved.get("message") or "")
        if cid:
            payload["cid"] = cid
    category_row = (category_service.find_by_cid(db, cid, site=str(info.get("site") or ""),
                                                 account_id=account.id)
                    if cid else None)

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
    props = _clean_color_props(payload)

    # 必要字段诊断（老版 product_workflow.diagnose_item）。
    # 顺序与老版一致：先判翻译覆盖，再判字段齐备；两项都过才继续。
    # 拦在写库前，避免把「缺重量 / SKU 价格为 0 / 无包裹尺寸」的商品存进来，
    # 那种货上架时会被平台直接拒掉。
    diagnose_issues = product_service.diagnose(info)
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

    issues = pre_save_check.pre_save_issues(
        info, payload, coverage,
        main_limit=MAIN_IMAGE_LIMIT,
        detail_limit=DETAIL_IMAGE_HARD_LIMIT,
        # 尺寸与尺码图已经写进 payload 了，这里传真值让规则 3/4 真正生效
        package_dimensions=dimensions,
        size_chart_url=chart_url,
        process_images_enabled=False,
        image_only=False,
        pricing_enabled=template is not None,
        # 闸门结果并进 issue 清单：规则 13/14 与上面 gate 共用同一份判定，
        # 各写一套迟早出现「预览说有、入仓说没有」。
        title_review_words=(gate.get("title") or {}).get("reviewWords") or [],
        image_review_pending=gate.get("images"),
        requirements=requirements,
    )
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

    key = product_key(detail_id, shop_id)
    fields = product_service.build_product(
        info=payload, detail_id=detail_id, shop_id=shop_id, account_id=account.id,
        shop_name=_shop_name(db, account, shop_id), language=language,
        category_id=category_row.id if category_row else None,
        category_name=(category_row.name_cn or category_row.name) if category_row else
        product_service.category_label(payload),
        site=str(info.get("site") or account.site or ""),
    )
    existing = (db.query(Product).filter(Product.warehouse_key == key).one_or_none())

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
    image_applied = apply_image_results(db, product, updated_payload)
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


def group_counts(db: Session) -> dict[int, int]:
    rows = (active_query(db).filter(Product.group_id.is_not(None))
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

