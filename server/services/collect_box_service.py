"""采集箱：分页查询、单件详情、认领。

「扫描即查询」：不再一次性拉全部 100 条，改为分页 + 服务端过滤。

上游结果带一层 TTL 本地缓存（``services.cache``）：妙手不可达时不再让用户
干等整段超时，有旧值就降级返回（``degraded=True`` + ``staleSeconds``）。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from core import errors
from core.constants import (
    COLLECT_BOX_MAX_ITEMS,
    COLLECT_BOX_PAGE_MAX,
    COLLECT_BOX_UPSTREAM_PAGE,
)
from core.locks import session_lock
from core.utils import (
    first_text,
    image_url,
    now_ts,
    product_key,
)
from models.product import Product
from services import carrier_service, product_service, warehouse_service
from services.account_service import (
    Account,
    account_client,
    map_upstream_error,
    require_shop,
)
from services.cache import FetchResult, TTLCache, get_cache, ttl_from_env
from services.miaoshou import MiaoshouError

VALID_STATUS = {"notPublished": "notPublished", "published": "published", "all": "all"}

# --- 缓存策略 ---------------------------------------------------------------
# 上游结果默认缓存 60 秒：短到「认领/删除」立刻能看到，长到连点几次采集箱
# 不会次次回源。要改就设 AOHUANG_COLLECT_BOX_CACHE_TTL（秒）。
COLLECT_BOX_CACHE_TTL_ENV = "AOHUANG_COLLECT_BOX_CACHE_TTL"
COLLECT_BOX_CACHE_TTL_DEFAULT = 60.0
# 上游翻页最多 10 次（原有约束），缓存表按「账号 × 参数组合」存，256 条足够
COLLECT_BOX_CACHE_MAX_ENTRIES = 256

_CACHE: TTLCache | None = None


def _cache() -> TTLCache:
    """按账号分前缀的单例缓存：写操作后能只失效该账号。"""
    global _CACHE
    if _CACHE is None:
        _CACHE = get_cache("collect_box", max_entries=COLLECT_BOX_CACHE_MAX_ENTRIES,
                           default_ttl=COLLECT_BOX_CACHE_TTL_DEFAULT)
    return _CACHE


def cache_ttl() -> float:
    return ttl_from_env(COLLECT_BOX_CACHE_TTL_ENV, COLLECT_BOX_CACHE_TTL_DEFAULT)


def account_cache_prefix(account: Account) -> str:
    return f"acct:{getattr(account, 'id', 0)}:"


def invalidate_account(account: Account) -> int:
    """写类操作（认领 / 处理 / 删除）之后调用，避免用户读完立刻看到旧列表。"""
    return _cache().invalidate(account_cache_prefix(account))


# --- 认领互斥（两人同抢同一件） ---------------------------------------------
# 认领是**不可撤销的上游动作**：一旦 claim_to_shop 打过去，妙手那边就多了一条
# 采集箱记录，本工具没有接口把它退回来。两个人同时点认领，上游就收到两次。
#
# 锁本身在 core/locks.py（按业务键一把锁，不是一把全局锁，也不是哈希桶）：
# 全局锁会让「A 认领第 1 件、B 认领第 2 件」互相排队，而这两件事毫无关系；
# 哈希桶会让两个无关商品偶然撞进同一桶、随机互相阻塞，现场复现不了。
#
# 「刚被认领」的时间窗（秒）。锁只在同进程内有效；跨进程/重启后靠这个窗口兜底：
# 5 分钟足够覆盖「同一个人连点两次」和「两个人前后脚点」，
# 又短到不会把正常的「过一会儿再认领一次」挡住（重复认领本来就会被上游拒）。
CLAIM_RECENT_SECONDS = 300.0


def _recent_claim_blocker(db: Session, key: str) -> None:
    """同一件商品刚被认领过就拒绝（锁管不到跨进程/重启，这里补上）。

    只看本地库：上游没有「查这件在不在采集箱」的廉价接口，真去查一次
    等于把认领的耗时翻倍，而本地 claimed_at 已经足够回答「刚刚有人点过」。
    """
    product = (db.query(Product)
               .filter(Product.warehouse_key == key, Product.deleted_at.is_(None))
               .one_or_none())
    if product is None or not product.claimed:
        return
    claimed_at = float(product.claimed_at or 0)
    if not claimed_at:
        return
    age = now_ts() - claimed_at
    if age < 0 or age >= CLAIM_RECENT_SECONDS:
        # 负数说明系统时钟被改过，不拿它当「刚刚」的证据
        return
    from core.utils import clock_text

    when = clock_text(claimed_at)
    raise errors.busy(
        f"这件商品刚被认领（{when}），如果确实需要请稍后重试",
        claimedAt=claimed_at,
        claimedShopId=product.claimed_shop_id or "",
        retryAfterSeconds=int(CLAIM_RECENT_SECONDS - age) + 1,
    )


def _cache_key(account: Account, *, page: int, page_size: int, status: str,
               keyword: str, shop_id: str | None, site: str | None,
               sort: str) -> str:
    """所有会影响上游结果的参数都进 key，缺一个就会串页。"""
    return (
        f"{account_cache_prefix(account)}"
        f"page={int(page)}|size={int(page_size)}|status={status}"
        f"|kw={str(keyword or '').strip().casefold()}"
        f"|shop={str(shop_id or '').strip()}"
        f"|site={str(site or '').strip().upper()}"
        f"|sort={sort}"
    )



def _row_title(row: Mapping[str, Any]) -> str:
    nested = row.get("sourceProductInfo") or row.get("productInfo") or {}
    if not isinstance(nested, Mapping):
        nested = {}
    return first_text(
        row.get("title"), row.get("itemTitle"), row.get("productTitle"),
        row.get("goodsName"), row.get("sourceTitle"), row.get("name"),
        nested.get("title"), nested.get("itemTitle"), nested.get("productTitle"),
    ) or "（妙手未返回标题）"


def _row_image(row: Mapping[str, Any]) -> str:
    nested = row.get("sourceProductInfo") or row.get("productInfo") or {}
    if not isinstance(nested, Mapping):
        nested = {}
    for key in ("mainImgUrl", "mainImageUrl", "imgUrl", "imageUrl", "coverUrl",
                "thumbnail", "image", "imgUrls", "images"):
        candidate = image_url(row.get(key)) or image_url(nested.get(key))
        if candidate:
            return candidate
    return ""


def listing_rows(response: Mapping[str, Any]) -> list[dict[str, Any]]:
    """collect_box_list → 每行 (key, detailId, shopId, shopName, site, title, image, sourceId)。"""
    data = (response or {}).get("data") or {}
    rows = data.get("detailList")
    if not isinstance(rows, list):
        raise errors.upstream("采集箱返回结构异常", reason="detailList_missing")
    products: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("editModel") not in (None, "shop"):
            continue
        detail_raw = row.get("collectBoxDetailId") or row.get("detailId")
        shops = row.get("collectBoxDetailShopList") or []
        if not isinstance(shops, list):
            continue
        if not shops and row.get("shopId") not in (None, ""):
            shops = [{"shopId": row.get("shopId"), "shopName": row.get("shopName")}]
        for shop in shops:
            if not isinstance(shop, dict) or shop.get("shopId") in (None, ""):
                continue
            try:
                detail_id, shop_id = int(detail_raw), int(shop["shopId"])
            except (TypeError, ValueError):
                continue
            key = product_key(detail_id, shop_id)
            if key in seen:
                continue
            seen.add(key)
            products.append({
                "key": key,
                "detailId": str(detail_id),
                "shopId": str(shop_id),
                "shopName": first_text(shop.get("shopName"), shop.get("shopTitle"),
                                       shop.get("shopNick")),
                "site": first_text(row.get("site"), row.get("siteCode")).upper(),
                "title": _row_title(row),
                "imageUrl": _row_image(row),
                "sourceId": first_text(row.get("sourceItemId"), row.get("sourceId")) or None,
                "status": "未发布",
                "editModel": str(row.get("editModel") or "shop"),
            })
            if len(products) >= COLLECT_BOX_MAX_ITEMS:
                return products
    return products


def _scan_upstream(account: Account, *, page: int, page_size: int, status: str,
                   keyword: str, shop_id: str | None, site: str | None
                   ) -> dict[str, Any]:
    """真正打妙手的那一段（缓存未命中时才走这里）。返回值可 JSON 化。"""
    client = account_client(account)
    try:
        upstream_status = None if status == "all" else status
        request_page = page
        items: list[dict[str, Any]] = []
        total = 0
        scanned = 0
        # 关键词/店铺/站点过滤在服务端做，因此按页向上游翻，直到凑够一页或扫满上限。
        max_upstream_pages = max(1, min(10, -(-page_size // COLLECT_BOX_UPSTREAM_PAGE)))
        while request_page <= max_upstream_pages and len(items) < page_size:
            response = client.collect_box_list(
                status=upstream_status, keyword=keyword or "",
                page_no=request_page, page_size=COLLECT_BOX_UPSTREAM_PAGE)
            data = (response or {}).get("data") or {}
            rows = listing_rows(response)
            total = max(total, int(data.get("total") or 0))
            scanned += len(rows)
            for row in rows:
                if shop_id and str(row["shopId"]) != str(shop_id):
                    continue
                if site and str(row["site"]).upper() != str(site).upper():
                    continue
                if keyword and not _matches_keyword(row, keyword):
                    continue
                items.append(row)
            upstream_rows = data.get("detailList") or []
            if len(upstream_rows) < COLLECT_BOX_UPSTREAM_PAGE:
                break
            request_page += 1
        items = items[:page_size]
    finally:
        client.close()
    return {"items": items, "total": total, "scanned": scanned}


def list_items(db: Session, account: Account, *, page: int = 1, page_size: int = 20,
               status: str = "notPublished", keyword: str = "", shop_id: str | None = None,
               site: str | None = None, sort: str = "default") -> dict[str, Any]:
    """GET /collect-box。pageSize 上限 COLLECT_BOX_PAGE_MAX（默认 100）。

    妙手上游单次只给 20 条，这里按页向上游翻直到凑够调用方要的条数。

    带 TTL 本地缓存：命中直接返回（``cached=True``）；上游失败时回落到旧值并置
    ``degraded=True``（连过期旧值都算），实在没有任何旧值才走原来的 502 映射。
    """
    if page < 1:
        raise errors.validation("页码必须不小于 1", field="page", value=page)
    if not 1 <= page_size <= COLLECT_BOX_PAGE_MAX:
        raise errors.validation(f"每页数量必须在 1 到 {COLLECT_BOX_PAGE_MAX} 之间",
                                field="pageSize", value=page_size)
    if status not in VALID_STATUS:
        raise errors.validation("status 只能是 notPublished / published / all",
                                field="status", value=status)
    if sort not in ("default", "savedDesc"):
        raise errors.validation("sort 只能是 default / savedDesc", field="sort", value=sort)

    key = _cache_key(account, page=page, page_size=page_size, status=status,
                     keyword=keyword, shop_id=shop_id, site=site, sort=sort)
    try:
        outcome: FetchResult = _cache().fetch(
            key,
            lambda: _scan_upstream(account, page=page, page_size=page_size, status=status,
                                   keyword=keyword, shop_id=shop_id, site=site),
            ttl=cache_ttl(),
            stale_on=MiaoshouError,
        )
    except MiaoshouError as exc:
        raise map_upstream_error(exc) from None

    snapshot = outcome.value or {}
    items = snapshot.get("items") or []
    total = int(snapshot.get("total") or 0)
    scanned = int(snapshot.get("scanned") or 0)

    # 仓库标注查的是本地库，必须每次按当前库状态重算，不能跟着缓存走
    _annotate_warehouse(db, items)
    if sort == "savedDesc":
        items.sort(key=lambda row: row.get("inWarehouse", False), reverse=True)
    return {
        "items": items,
        "page": page,
        "pageSize": page_size,
        "total": max(total, len(items)),
        "hasMore": len(items) >= page_size,
        "scannedAt": now_ts(),
        "degraded": bool(outcome.degraded),
        "staleSeconds": int(outcome.stale_seconds),
        "cached": bool(outcome.cached),
        "scanned": scanned,
    }



def _matches_keyword(row: Mapping[str, Any], keyword: str) -> bool:
    needle = str(keyword or "").strip().casefold()
    if not needle:
        return True
    haystack = f"{row.get('title') or ''} {row.get('detailId') or ''} {row.get('sourceId') or ''}"
    return needle in haystack.casefold()


def _annotate_warehouse(db: Session, items: list[dict[str, Any]]) -> None:
    """标记是否已在仓库、已采集进哪些账号（列表页要显示这两列）。"""
    if not items:
        return
    keys = [row["key"] for row in items]
    rows = (db.query(Product)
            .filter(Product.warehouse_key.in_(keys), Product.deleted_at.is_(None)).all())
    by_key = {row.warehouse_key: row for row in rows}
    for item in items:
        product = by_key.get(item["key"])
        item["warehouseId"] = str(product.id) if product else None
        item["inWarehouse"] = product is not None
        collected = (product.collected if product and isinstance(product.collected, dict) else {})
        item["hasDraftIn"] = list(collected.keys())


def detail(db: Session, account: Account, *, detail_id: str, shop_id: str,
           mode: str = "shop") -> dict[str, Any]:
    if not str(detail_id or "").strip().isdigit():
        raise errors.validation("detailId 必须是数字", field="detailId", value=detail_id)
    if not str(shop_id or "").strip().isdigit():
        raise errors.validation("shopId 必须是数字", field="shopId", value=shop_id)
    if mode not in ("shop", "site"):
        raise errors.validation("mode 只能是 shop / site", field="mode", value=mode)
    client = account_client(account)
    try:
        if mode == "site":
            response = client.site_detail(detail_id, str(shop_id))
        else:
            response = client.shop_detail(detail_id, shop_id)
    except MiaoshouError as exc:
        raise map_upstream_error(exc) from None
    finally:
        client.close()
    info = product_service.detail_block(response, mode)["info"]
    cid = str(info.get("cid") or "").strip()
    category_path = ""
    if cid:
        from services import category_service

        category_path = category_service.path_of(db, cid, site=str(info.get("site") or ""),
                                                account_id=account.id)
    summary = product_service.detail_summary(response, detail_id=detail_id, shop_id=shop_id,
                                             mode=mode, category_path=category_path)
    key = product_key(detail_id, shop_id)
    product = (db.query(Product)
               .filter(Product.warehouse_key == key, Product.deleted_at.is_(None))
               .one_or_none())
    summary["inWarehouse"] = product is not None
    summary["warehouseId"] = str(product.id) if product else None
    return summary


def claim(db: Session, account: Account, *, detail_id: str, shop_id: str,
          target_shop_id: str, mark_collected: bool = True) -> dict[str, Any]:
    """认领进目标店铺采集箱。只认领，不发布。

    互斥两道（见上方「认领互斥」注释与 core/locks.py）：
      1. 同商品键的会话锁 —— 挡住同一进程内两个人同时点；
      2. 本地 claimed_at 5 分钟窗口 —— 挡住跨进程、重启后的「前后脚」。
    两道都不改上游调用本身的语义：拿到锁的那一次，仍然只调一次 claim_to_shop。

    锁是**会话级**的（`session_lock`）：一直持到路由里那次 commit 之后才放。
    这不是洁癖 —— 用函数级的锁实测会漏：A 认领完、还没提交时锁就
    放了，B 进得来且读不到 A 未提交的 claimed，于是 claim_to_shop 被打两次。
    """
    if not str(target_shop_id or "").strip():
        raise errors.validation("请选择认领到哪个店铺", field="targetShopId")
    if not str(detail_id or "").strip().isdigit():
        raise errors.validation("detailId 必须是数字", field="detailId", value=detail_id)
    if not str(shop_id or "").strip().isdigit():
        raise errors.validation("shopId 必须是数字", field="shopId", value=shop_id)

    # 锁键用「源商品」的 warehouse_key：同一件商品被两个人认领到不同店铺时，
    # 上游动作仍然是同一个 claim_to_shop，必须串起来。
    # 非阻塞：抢不到立刻告诉人，不让人盯着转圈等一个几秒的上游调用。
    lock_key = product_key(detail_id, shop_id)
    with session_lock(
        db,
        [lock_key], blocking=False,
        busy_message="这件商品正在被别人认领，请稍后重试",
        busy_details={"detailId": str(detail_id), "shopId": str(shop_id)},
    ):
        # 锁内再查一次本地认领状态：跨进程/重启时锁是空的，这个窗口是唯一的兜底
        _recent_claim_blocker(db, lock_key)

        target_shop = require_shop(db, account, target_shop_id)
        source_shop = require_shop(db, account, shop_id)
        same = str(target_shop_id) == str(shop_id)
        client = account_client(account)
        failures: list[str] = []
        detail_ids: list[str] = []
        mode = "direct"
        try:
            if same:
                try:
                    client.claim_to_shop([int(detail_id)], [int(target_shop_id)])
                    detail_ids = [str(detail_id)]
                except MiaoshouError as exc:
                    message = str(exc)
                    if "已" in message and "采集箱" in message:
                        raise errors.AppError(errors.ALREADY_CLAIMED,
                                              "该明细已在目标店铺的采集箱里",
                                              status=409) from None
                    raise map_upstream_error(exc) from None
            else:
                mode = "carrier"
                snapshot = client.shop_detail(detail_id, shop_id)
                info = product_service.detail_block(snapshot)["info"]
                if not info:
                    raise errors.not_found(f"上游查无明细 {detail_id}", detailId=str(detail_id))
                # 仓库里这份商品可能已经被处理过（重量加增量、包裹尺寸归一、
                # 标题清理）。跨店认领直接搬上游原始 info，会把处理结果全丢掉——
                # 新店铺里的商品变成没加重量、没尺寸的原始态。
                # 老版认领走的是仓库里的 payload，这里对齐。
                stored = (db.query(Product)
                          .filter(Product.warehouse_key == product_key(detail_id, shop_id),
                                  Product.deleted_at.is_(None))
                          .one_or_none())
                if stored is not None and isinstance(stored.payload, dict) and stored.payload:
                    info = {**info, **stored.payload}
                carriers = carrier_service.collect_carriers(db, client, count=1)
                filled = carrier_service.fill_carriers(
                    client, carriers, [info], shop_id=int(target_shop_id))
                detail_ids = [str(value) for value in filled["saved"]]
                failures.extend(filled["failures"])
        except MiaoshouError as exc:
            raise map_upstream_error(exc) from None
        finally:
            client.close()

        # 认领成功 = 采集箱内容变了：本账号的列表缓存立刻作废，
        # 下一个 GET 不会还拿认领前的旧快照。
        invalidate_account(account)

        collected: dict[str, Any] = {}
        if mark_collected:
            key = product_key(detail_id, shop_id)
            product = (db.query(Product)
                       .filter(Product.warehouse_key == key, Product.deleted_at.is_(None))
                       .one_or_none())
            if product is not None:
                state = dict(product.collected or {}) if isinstance(product.collected, dict) else {}
                state[account.name] = {"at": now_ts(),
                                       "detailId": detail_ids[0] if detail_ids else str(detail_id)}
                product.collected = state
                product.claimed = True
                product.claimed_at = now_ts()
                product.claimed_shop_id = str(target_shop_id)
                collected = state
                # 认领改了商品行，要推版本号：别人手上「未认领」的旧页面
                # 不该还能保存并覆盖掉这次认领。
                warehouse_service.bump_revision(product)
                db.flush()

        return {
            "claimed": bool(detail_ids) and not failures,
            "detailId": str(detail_id),
            "fromShopId": str(shop_id),
            "targetShopId": str(target_shop_id),
            "targetShopName": target_shop.get("name") or "",
            "sourceShopName": source_shop.get("name") or "",
            "mode": mode,
            "count": len(detail_ids),
            "detailIds": detail_ids,
            "failures": failures,
            "collected": collected,
        }


def release(db: Session, account: Account, *, detail_id: str, shop_id: str,
            target_shop_id: str = "") -> dict[str, Any]:
    """撤销认领标记（老版批量认领里 `claimed=false` 那条分支）。

    只清本地认领状态，**不动妙手上已认领的商品** —— 上游没有「取消认领」接口，
    真删要走 `delete()`。这里做的是「我标错了，把标记退回来」，让商品重新
    出现在待认领列表里，而不是假装上游也退回了。

    为什么必须有这条：`BatchClaimRequest.claimed` 这个字段以前定义了但完全没用，
    传 false 也照样认领。运营想「反悔」只能去数据库改，或者重新入仓一遍。
    """
    key = product_key(detail_id, shop_id)
    product = (db.query(Product)
               .filter(Product.warehouse_key == key, Product.deleted_at.is_(None))
               .one_or_none())
    if product is None:
        raise errors.not_found(f"仓库里没有这件商品：{key}", key=key)
    state = dict(product.collected or {}) if isinstance(product.collected, dict) else {}
    # 只退本账号那一条：跨店认领时 collected 是按账号名分键的，
    # 整份清空会把别的账号的认领记录也抹掉。
    removed = state.pop(account.name, None)
    product.collected = state
    # 还有别的账号认领记录就保持已认领，否则才真的退回未认领
    still_claimed = bool(state)
    product.claimed = still_claimed
    if not still_claimed:
        product.claimed_at = None
        product.claimed_shop_id = None
    # 撤销认领同样改了这一行（claimed / collected）。不推版本号的话，
    # 别人手上那份「已认领」的旧页面还能照常保存，把这次撤销悄悄改回去。
    warehouse_service.bump_revision(product)
    db.flush()
    return {
        "claimed": False,
        "detailId": str(detail_id),
        "shopId": str(shop_id),
        "targetShopId": str(target_shop_id or product.claimed_shop_id or ""),
        "released": removed is not None,
        "stillClaimed": still_claimed,
        "collected": state,
        "count": 0,
        "detailIds": [],
        "failures": [],
    }


def delete(db: Session, account: Account, detail_ids: list[str]) -> dict[str, Any]:
    """从妙手采集箱删除商品。

    用于清理带公司水印这类不能上架的废品；本地仓库里的同名商品不受影响。
    """
    ids: list[int] = []
    for value in detail_ids:
        try:
            ids.append(int(value))
        except (TypeError, ValueError):
            raise errors.validation(f"detailId 不是有效数字：{value}", field="detailIds") from None
    if not ids:
        raise errors.validation("请选择要删除的商品", field="detailIds")
    if len(ids) > 200:
        raise errors.validation("一次最多删除 200 件", field="detailIds")

    client = account_client(account)
    try:
        response = client.delete_collect_box(ids)
    except MiaoshouError as exc:
        raise map_upstream_error(exc) from None
    finally:
        client.close()
    # 删除同样改变了采集箱；哪怕上游只删掉一部分，旧列表也已经不可信。
    invalidate_account(account)
    data = response.get("data") or {}
    return {
        "requested": len(ids),
        "deleted": int(data.get("successNum") or 0),
        "failed": int(data.get("failNum") or 0),
        "errors": data.get("errorMap") or {},
    }
