"""采集箱：分页查询、单件详情、认领。

「扫描即查询」：不再一次性拉全部 100 条，改为分页 + 服务端过滤。

上游结果带一层 TTL 本地缓存（``services.cache``）：妙手不可达时不再让用户
干等整段超时，有旧值就降级返回（``degraded=True`` + ``staleSeconds``）。
"""
from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from core import errors
from core.config import get_settings
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
from models.publish_log import PublishLog
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
# 上游结果默认缓存 10 秒：连点几次采集箱不会次次回源，同时限制新商品的
# 最长自然延迟。手动刷新会绕过此缓存。要改就设 AOHUANG_COLLECT_BOX_CACHE_TTL（秒）。
COLLECT_BOX_CACHE_TTL_ENV = "AOHUANG_COLLECT_BOX_CACHE_TTL"
COLLECT_BOX_CACHE_TTL_DEFAULT = 10.0
# 缓存表按「账号 × 参数组合」存，256 条足够
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
    """Scope snapshots to both the account row and the active credentials.

    Shared-mode requests use transient account objects with id=0, so the row id
    alone would let unrelated AppKeys reuse the same upstream snapshot. The
    credential tuple is keyed with the installation secret and never appears
    in the cache key or logs.
    """
    secret = get_settings().local_secret().encode("utf-8")
    credential_material = (
        f"{getattr(account, 'app_key', '') or ''}\0"
        f"{getattr(account, 'app_secret_enc', '') or ''}"
    ).encode()
    fingerprint = hmac.new(secret, credential_material, hashlib.sha256).hexdigest()
    return f"acct:{getattr(account, 'id', 0)}:{fingerprint}:"


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


def _account_claim_key(account: Account) -> str:
    """Return the stable key used for this account's local claim ledger.

    Account names are editable display labels and were used by an older build
    as the JSON key.  Using the numeric account id keeps the ledger stable when
    a name changes and, more importantly, prevents two accounts with the same
    label from sharing claim state.  Readers still accept the legacy name key
    while old rows are being migrated naturally.
    """
    return f"account:{int(account.id)}"


def _operator_claim_key(account: Account, operator_id: int) -> str:
    return f"operator:{int(operator_id)}:account:{int(account.id)}"


def _record_carrier_claim(db: Session, account: Account, product: Product | None, *,
                          detail_id: str, shop_id: str, operator_id: int | None,
                          claimed_at: float) -> None:
    if product is None:
        return
    state = dict(product.collected or {}) if isinstance(product.collected, dict) else {}
    marker = {"at": claimed_at, "detailId": detail_id, "shopId": shop_id}
    state[_account_claim_key(account)] = marker
    if operator_id is not None:
        state[_operator_claim_key(account, operator_id)] = marker
        warehouse_service.claim_product(db, product.id, operator_id)
    product.collected = state
    product.claimed = True
    product.claimed_at = claimed_at
    product.claimed_shop_id = shop_id
    warehouse_service.bump_revision(product)


def _recent_claim_blocker(db: Session, account: Account, key: str) -> None:
    """同一件商品刚被认领过就拒绝（锁管不到跨进程/重启，这里补上）。

    只看本地库：上游没有「查这件在不在采集箱」的廉价接口，真去查一次
    等于把认领的耗时翻倍，而本地 claimed_at 已经足够回答「刚刚有人点过」。
    """
    product = (db.query(Product)
               .filter(Product.account_id == account.id,
                       Product.warehouse_key == key,
                       Product.deleted_at.is_(None))
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


def verify_items_present(client: Any, detail_ids: set[str] | list[str], shop_id: str,
                         *, max_pages: int = 50, retries: int = 2,
                         retry_delay: float = 0.25) -> set[str]:
    """Return detail IDs confirmed in the target account's collect-box listing.

    Save/claim responses can acknowledge writes before the collect-box list is
    updated. Only IDs visible under the requested shop count as imported.
    """
    expected = {str(value) for value in detail_ids if str(value).isdigit()}
    if not expected:
        return set()
    found: set[str] = set()
    for attempt in range(max(0, retries) + 1):
        page_no = 1
        while expected - found and page_no <= max_pages:
            response = client.collect_box_list(
                status="all", page_no=page_no, page_size=COLLECT_BOX_UPSTREAM_PAGE)
            data = (response or {}).get("data") or {}
            upstream_rows = data.get("detailList") or []
            for row in upstream_rows:
                if not isinstance(row, Mapping):
                    continue
                detail_id = str(row.get("collectBoxDetailId") or row.get("detailId") or "")
                if detail_id not in expected:
                    continue
                shops = row.get("collectBoxDetailShopList") or []
                if not shops and row.get("shopId") not in (None, ""):
                    shops = [{"shopId": row.get("shopId")}]
                if any(isinstance(shop, Mapping) and str(shop.get("shopId")) == str(shop_id)
                       for shop in shops):
                    found.add(detail_id)
            total = int(data.get("total") or 0)
            if (len(upstream_rows) < COLLECT_BOX_UPSTREAM_PAGE
                    or (total and page_no * COLLECT_BOX_UPSTREAM_PAGE >= total)):
                break
            page_no += 1
        if expected <= found or attempt >= retries:
            break
        time.sleep(max(0.0, retry_delay))
    return found


def _scan_upstream(account: Account, *, page: int, page_size: int, status: str,
                   keyword: str, shop_id: str | None, site: str | None
                   ) -> dict[str, Any]:
    """真正打妙手的那一段（缓存未命中时才走这里）。返回值可 JSON 化。"""
    client = account_client(account)
    try:
        upstream_status = None if status == "all" else status
        # 上游按原始商品分页，listing_rows 还会按店铺展开，因此不能把客户端
        # 页码直接传过去；筛选后的行必须先跳过前面各页，再取本页。
        request_page = 1
        offset = (page - 1) * page_size
        matched = 0
        items: list[dict[str, Any]] = []
        total = 0
        scanned = 0
        has_more = False
        while True:
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
                matched += 1
                if matched <= offset:
                    continue
                if len(items) >= page_size:
                    has_more = True
                    break
                items.append(row)
            upstream_rows = data.get("detailList") or []
            if (has_more or len(upstream_rows) < COLLECT_BOX_UPSTREAM_PAGE
                    or (total and request_page * COLLECT_BOX_UPSTREAM_PAGE >= total)):
                break
            request_page += 1
    finally:
        client.close()
    return {"items": items, "total": total, "scanned": scanned, "hasMore": has_more}


def list_items(db: Session, account: Account, *, page: int = 1, page_size: int = 20,
               status: str = "notPublished", keyword: str = "", shop_id: str | None = None,
               site: str | None = None, sort: str = "default",
               force_refresh: bool = False) -> dict[str, Any]:
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
            force_refresh=force_refresh,
            stale_on=MiaoshouError,
        )
    except MiaoshouError as exc:
        raise map_upstream_error(exc) from None

    snapshot = outcome.value or {}
    items = snapshot.get("items") or []
    total = int(snapshot.get("total") or 0)
    scanned = int(snapshot.get("scanned") or 0)

    # 仓库标注查的是本地库，必须每次按当前库状态重算，不能跟着缓存走
    _annotate_warehouse(db, account, items)
    if sort == "savedDesc":
        items.sort(key=lambda row: row.get("inWarehouse", False), reverse=True)
    return {
        "items": items,
        "page": page,
        "pageSize": page_size,
        "total": max(total, len(items)),
        "hasMore": bool(snapshot.get("hasMore", len(items) >= page_size)),
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


def _annotate_warehouse(db: Session, account: Account,
                        items: list[dict[str, Any]]) -> None:
    """标记是否已在仓库、已采集进哪些账号（列表页要显示这两列）。"""
    if not items:
        return
    keys = [row["key"] for row in items]
    rows = (db.query(Product)
            .filter(Product.account_id == account.id,
                    Product.warehouse_key.in_(keys),
                    Product.deleted_at.is_(None)).all())
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
               .filter(Product.account_id == account.id,
                       Product.warehouse_key == key,
                       Product.deleted_at.is_(None))
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
        _recent_claim_blocker(db, account, lock_key)

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
                          .filter(Product.account_id == account.id,
                                  Product.warehouse_key == product_key(detail_id, shop_id),
                                  Product.deleted_at.is_(None))
                          .one_or_none())
                if stored is not None and isinstance(stored.payload, dict) and stored.payload:
                    info = {**info, **stored.payload}
                carriers = carrier_service.collect_carriers(db, client, count=1)
                filled = carrier_service.fill_carriers(
                    client, carriers, [info], shop_id=int(target_shop_id),
                    verify_readback=False)
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
        if mark_collected and detail_ids:
            key = product_key(detail_id, shop_id)
            product = (db.query(Product)
                       .filter(Product.account_id == account.id,
                               Product.warehouse_key == key,
                               Product.deleted_at.is_(None))
                       .one_or_none())
            if product is not None:
                state = dict(product.collected or {}) if isinstance(product.collected, dict) else {}
                state[_account_claim_key(account)] = {
                    "at": now_ts(),
                    "detailId": detail_ids[0],
                }
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


def claim_carrier_batch(db: Session, account: Account, *,
                        items: list[tuple[str, str]],
                        target_shop_id: str,
                        operator_id: int | None = None) -> list[dict[str, Any]]:
    """Batch cross-shop claims; each new carrier is covered as soon as it exists."""
    if not str(target_shop_id or "").strip().isdigit():
        raise errors.validation("请选择认领到哪个店铺", field="targetShopId")
    normalized = [(str(detail_id), str(shop_id)) for detail_id, shop_id in items]
    if not normalized or any(not detail.isdigit() or not shop.isdigit()
                             for detail, shop in normalized):
        raise errors.validation("商品明细或来源店铺无效")
    if len({product_key(detail, shop) for detail, shop in normalized}) != len(normalized):
        raise errors.validation("批次中包含重复商品")
    if any(shop == str(target_shop_id) for _, shop in normalized):
        raise errors.validation("批量流水认领只支持跨店商品")

    target_shop = require_shop(db, account, target_shop_id)
    keys = [product_key(detail, shop) for detail, shop in normalized]
    with session_lock(db, keys, blocking=False,
                      busy_message="部分商品正在被别人认领，请稍后重试"):
        products = (db.query(Product)
                    .filter(Product.account_id == account.id,
                            Product.warehouse_key.in_(keys),
                            Product.deleted_at.is_(None))
                    .all())
        by_key = {row.warehouse_key: row for row in products}
        client = account_client(account)
        try:
            pending_by_index: dict[int, tuple[PublishLog, list[str]]] = {}
            owner_filter = (PublishLog.operator_id.is_(None) if operator_id is None
                            else PublishLog.operator_id == operator_id)
            for index, (detail_id, _shop_id) in enumerate(normalized):
                log = (db.query(PublishLog)
                       .filter(PublishLog.account_id == account.id,
                               owner_filter,
                               PublishLog.detail_id == detail_id,
                               PublishLog.shop_id == str(target_shop_id))
                       .order_by(PublishLog.id.desc()).first())
                detail = log.detail if log and isinstance(log.detail, dict) else {}
                carrier_claim = detail.get("carrierClaim")
                if not isinstance(carrier_claim, dict):
                    continue
                target_ids = [str(value) for value in carrier_claim.get("targetDetailIds", [])
                              if str(value).isdigit()]
                if carrier_claim.get("status") == "confirmed":
                    raise errors.AppError(
                        errors.ALREADY_CLAIMED,
                        "该商品已认领到此店铺",
                        status=409,
                        details={"detailId": detail_id,
                                 "shopId": str(target_shop_id),
                                 "targetDetailIds": target_ids},
                    )
                if log.status == "processing" and not target_ids:
                    raise errors.busy(
                        "上次认领结果未返回载体 ID，请先检查目标采集箱；不能重复认领",
                        detailId=detail_id, targetShopId=str(target_shop_id))
                if target_ids and log.status in {"processing", "fail", "success"}:
                    pending_by_index[index] = (log, target_ids)
                elif log.status == "success":
                    raise errors.busy(
                        "上次认领记录缺少载体 ID，请先人工核对目标采集箱",
                        detailId=detail_id, targetShopId=str(target_shop_id))

            reconciled_by_index: dict[int, str] = {}
            if pending_by_index:
                pending_ids = sorted({value for _, ids in pending_by_index.values()
                                      for value in ids})
                confirmed_pending = verify_items_present(
                    client, pending_ids, str(target_shop_id))
                stamp = now_ts()
                for index, (log, target_ids) in pending_by_index.items():
                    if not target_ids or not set(target_ids).issubset(confirmed_pending):
                        raise errors.busy(
                            "上次认领结果仍未在目标采集箱确认，请稍后刷新核对；不能重复认领",
                            detailId=normalized[index][0],
                            targetShopId=str(target_shop_id),
                            targetDetailIds=target_ids)
                    detail = dict(log.detail or {})
                    carrier_claim = dict(detail.get("carrierClaim") or {})
                    covered_ids = {str(value) for value in
                                   carrier_claim.get("coveredDetailIds", [])}
                    if set(target_ids).issubset(covered_ids):
                        carrier_claim.update({"status": "confirmed",
                                              "confirmedAt": stamp})
                        log.detail = {**detail, "carrierClaim": carrier_claim}
                        log.status = "success"
                        log.reason = "认领已在目标采集箱确认"
                        log.finished_at = stamp
                        reconciled_by_index[index] = target_ids[0]
                        _record_carrier_claim(
                            db, account, by_key.get(product_key(*normalized[index])),
                            detail_id=target_ids[0], shop_id=str(target_shop_id),
                            operator_id=operator_id, claimed_at=stamp)

            source_shops = {shop_id: require_shop(db, account, shop_id)
                            for _, shop_id in normalized}
            payloads: list[dict[str, Any]] = []
            payload_indexes: list[int] = []
            errors_by_index: dict[int, str] = {}
            for index, (detail_id, shop_id) in enumerate(normalized):
                if index in reconciled_by_index:
                    continue
                stored = by_key.get(product_key(detail_id, shop_id))
                stored_payload = _stored_claim_payload(stored)
                if _claim_payload_is_complete(stored_payload):
                    payload_indexes.append(index)
                    payloads.append(stored_payload)
                    continue
                try:
                    snapshot = client.shop_detail(detail_id, shop_id)
                    info = product_service.detail_block(snapshot)["info"]
                    if not info:
                        raise errors.not_found(f"上游查无明细 {detail_id}",
                                               detailId=detail_id)
                except MiaoshouError as exc:
                    errors_by_index[index] = str(exc)[:120]
                    continue
                if stored_payload:
                    info = {**info, **stored_payload}
                payload_indexes.append(index)
                payloads.append(info)

            if payloads:
                from services import publish_service

                claim_entries = []
                for source_index, payload in zip(payload_indexes, payloads, strict=True):
                    detail_id, shop_id = normalized[source_index]
                    stored = by_key.get(product_key(detail_id, shop_id))
                    claim_entries.append({
                        "warehouseId": str(stored.id if stored is not None else detail_id),
                        "payload": payload,
                    })
                payloads, adjustments = publish_service.prepare_claim_payloads(
                    db, payloads, claim_entries, operator_id=operator_id)
                stamp = now_ts()
                for source_index in payload_indexes:
                    detail_id, source_shop_id = normalized[source_index]
                    product = by_key.get(product_key(detail_id, source_shop_id))
                    warehouse_key = (product.warehouse_key if product is not None
                                     else product_key(detail_id, source_shop_id))
                    existing = (db.query(PublishLog)
                                .filter(PublishLog.account_id == account.id,
                                        owner_filter,
                                        PublishLog.detail_id == detail_id,
                                        PublishLog.shop_id == str(target_shop_id))
                                .order_by(PublishLog.id.desc()).first())
                    existing_detail = (existing.detail if existing is not None
                                       and isinstance(existing.detail, dict) else {})
                    if (existing is not None
                            and not isinstance(existing_detail.get("carrierClaim"), dict)):
                        raise errors.busy(
                            "该商品在目标店铺已有发布记录，已阻止覆盖原记录",
                            detailId=detail_id, targetShopId=str(target_shop_id))
                    if existing is None:
                        existing = PublishLog(
                            account_id=account.id, operator_id=operator_id,
                            product_id=product.id if product is not None else None,
                            detail_id=detail_id, shop_id=str(target_shop_id),
                            warehouse_key=warehouse_key, mode="carrier",
                            status="processing", requested_at=stamp,
                            last_requested_at=stamp, attempts=1)
                        db.add(existing)
                    else:
                        existing.product_id = product.id if product is not None else None
                        existing.warehouse_key = warehouse_key
                        existing.mode = "carrier"
                        existing.status = "processing"
                        existing.requested_at = existing.requested_at or stamp
                        existing.last_requested_at = stamp
                        existing.attempts = int(existing.attempts or 0) + 1
                        existing.finished_at = None
                    previous_ids = (pending_by_index[source_index][1]
                                    if source_index in pending_by_index else [])
                    item_adjustments = [item for item in adjustments
                                        if str(item.get("warehouseId") or "") ==
                                        str(product.id if product is not None else detail_id)]
                    existing.detail = {
                        "claimOnly": True,
                        "carrierClaim": {
                            "status": "pending",
                            "sourceShopId": source_shop_id,
                            "targetShopId": str(target_shop_id),
                            "targetDetailIds": previous_ids,
                            "coveredDetailIds": [],
                            "adjustments": item_adjustments,
                        },
                    }
                    existing.reason = "认领结果待采集箱核对，请勿重复认领"
                db.flush()
                db.commit()
                invalidate_account(account)

                def persist_carrier_progress(event: dict[str, Any]) -> None:
                    stage = event.get("stage")
                    if stage not in {"carrier_created", "saved", "verified"}:
                        return
                    source_index = event.get("sourceIndex")
                    detail_id = str(event.get("detailId") or "")
                    if (not isinstance(source_index, int)
                            or not 0 <= source_index < len(payload_indexes)
                            or not detail_id.isdigit()):
                        return
                    original_index = payload_indexes[source_index]
                    original_detail, _source_shop = normalized[original_index]
                    row = (db.query(PublishLog)
                           .filter(PublishLog.account_id == account.id,
                                   owner_filter,
                                   PublishLog.detail_id == original_detail,
                                   PublishLog.shop_id == str(target_shop_id),
                                   PublishLog.status == "processing")
                           .order_by(PublishLog.id.desc()).first())
                    if row is None:
                        return
                    detail = dict(row.detail or {})
                    carrier_claim = dict(detail.get("carrierClaim") or {})
                    target_ids = [str(value) for value in
                                  carrier_claim.get("targetDetailIds", [])]
                    if detail_id not in target_ids:
                        target_ids.append(detail_id)
                    carrier_claim["targetDetailIds"] = target_ids
                    if stage in {"saved", "verified"}:
                        covered_ids = [str(value) for value in
                                       carrier_claim.get("coveredDetailIds", [])]
                        if detail_id not in covered_ids:
                            covered_ids.append(detail_id)
                        carrier_claim["coveredDetailIds"] = covered_ids
                    row.detail = {**detail, "carrierClaim": carrier_claim}
                    db.commit()

                filled = {"carriers": [], "saved": [], "savedSources": [],
                          "failedSources": [], "failures": [],
                          "failureDetails": [], "priceAdjustments": []}

                def merge_filled(result: dict[str, Any], positions: list[int]) -> None:
                    local_saved_sources = result.get("savedSources") or []
                    for detail_value, local_source in zip(
                            result.get("saved") or [], local_saved_sources, strict=True):
                        filled["saved"].append(detail_value)
                        filled["savedSources"].append(positions[local_source])
                    saved_set = set(local_saved_sources)
                    failed_sources = result.get("failedSources")
                    if failed_sources is None:
                        failed_sources = [i for i in range(len(positions))
                                          if i not in saved_set]
                    filled["failedSources"].extend(
                        positions[i] for i in failed_sources)
                    filled["failures"].extend(result.get("failures") or [])
                    for failure in result.get("failureDetails") or []:
                        mapped = dict(failure)
                        local_source = mapped.get("sourceIndex")
                        if isinstance(local_source, int) and 0 <= local_source < len(positions):
                            mapped["sourceIndex"] = positions[local_source]
                        filled["failureDetails"].append(mapped)
                    for adjustment in result.get("priceAdjustments") or []:
                        mapped = dict(adjustment)
                        local_source = mapped.get("sourceIndex")
                        if isinstance(local_source, int) and 0 <= local_source < len(positions):
                            mapped["sourceIndex"] = positions[local_source]
                        filled["priceAdjustments"].append(mapped)

                recovery_positions = [local_index for local_index, original_index
                                      in enumerate(payload_indexes)
                                      if original_index in pending_by_index]
                new_positions = [local_index for local_index, original_index
                                 in enumerate(payload_indexes)
                                 if original_index not in pending_by_index]
                try:
                    if recovery_positions:
                        recovery_ids = []
                        for local_index in recovery_positions:
                            original_index = payload_indexes[local_index]
                            target_ids = pending_by_index[original_index][1]
                            if len(target_ids) != 1:
                                raise errors.busy(
                                    "上次操作产生多个载体，请先人工核对，不能继续复制",
                                    detailId=normalized[original_index][0],
                                    targetDetailIds=target_ids)
                            recovery_ids.append(int(target_ids[0]))
                        recovered = carrier_service.fill_carriers(
                            client, recovery_ids,
                            [payloads[i] for i in recovery_positions],
                            shop_id=int(target_shop_id), verify_readback=False)
                        recovery_position_by_id = {
                            str(detail_id): position
                            for detail_id, position in zip(
                                recovery_ids, recovery_positions, strict=True)
                        }
                        for failure in recovered.get("failureDetails") or []:
                            if ("sourceIndex" not in failure
                                    and str(failure.get("detailId") or "")
                                    in recovery_position_by_id):
                                failure["sourceIndex"] = recovery_position_by_id[
                                    str(failure["detailId"])]
                        merge_filled(recovered, recovery_positions)
                    if new_positions:
                        def remap_new_progress(event: dict[str, Any]) -> None:
                            local_source = event.get("sourceIndex")
                            if (isinstance(local_source, int)
                                    and 0 <= local_source < len(new_positions)):
                                persist_carrier_progress({
                                    **event, "sourceIndex": new_positions[local_source]})

                        created = carrier_service.collect_and_fill_carriers(
                            db, client, payloads=[payloads[i] for i in new_positions],
                            shop_id=int(target_shop_id), progress=remap_new_progress,
                            verify_readback=False)
                        merge_filled(created, new_positions)
                finally:
                    invalidate_account(account)
            else:
                filled = {"saved": [], "savedSources": [], "failedSources": [],
                          "failures": [], "failureDetails": []}
        except MiaoshouError as exc:
            client.close()
            raise map_upstream_error(exc) from None
        except Exception:
            client.close()
            raise

        for failure in filled.get("failureDetails") or []:
            source_index = failure.get("sourceIndex")
            if not isinstance(source_index, int) or not 0 <= source_index < len(payload_indexes):
                continue
            original_index = payload_indexes[source_index]
            errors_by_index.setdefault(
                original_index, str(failure.get("reason") or "载体覆盖失败")[:120])
        for source_index in filled.get("failedSources", []):
            original_index = payload_indexes[source_index]
            if original_index not in errors_by_index:
                failures = filled.get("failures") or []
                reason = failures[0] if len(failures) == 1 else "载体覆盖失败，单件原因未返回"
                errors_by_index[original_index] = str(reason)[:120]

        saved_by_source = {
            payload_indexes[source_index]: str(detail_id)
            for source_index, detail_id in zip(
                filled.get("savedSources", []), filled.get("saved", []), strict=True)
        }
        for source_index, detail_id in saved_by_source.items():
            detail_id_source, source_shop_id = normalized[source_index]
            log = (db.query(PublishLog)
                   .filter(PublishLog.account_id == account.id,
                           PublishLog.detail_id == detail_id_source,
                           PublishLog.shop_id == str(target_shop_id),
                           owner_filter)
                   .order_by(PublishLog.id.desc()).first())
            if log is not None:
                detail = dict(log.detail or {})
                carrier_claim = dict(detail.get("carrierClaim") or {})
                carrier_claim.update({"sourceShopId": source_shop_id,
                                      "targetDetailIds": [detail_id],
                                      "coveredDetailIds": [detail_id]})
                log.detail = {**detail, "carrierClaim": carrier_claim}
        if saved_by_source:
            db.commit()
        try:
            try:
                confirmed_ids = verify_items_present(
                    client, list(saved_by_source.values()), str(target_shop_id))
                verification_error = ""
            except MiaoshouError as exc:
                confirmed_ids = set()
                verification_error = str(exc)[:80]
            detail_by_source = {}
            for source_index, detail_id in saved_by_source.items():
                if detail_id in confirmed_ids:
                    detail_by_source[source_index] = detail_id
                    original_index = source_index
                    detail_id_source, _source_shop_id = normalized[original_index]
                    log = (db.query(PublishLog)
                           .filter(PublishLog.account_id == account.id,
                                   PublishLog.detail_id == detail_id_source,
                                   PublishLog.shop_id == str(target_shop_id),
                                   owner_filter)
                           .order_by(PublishLog.id.desc()).first())
                    if log is not None:
                        detail = dict(log.detail or {})
                        carrier_claim = dict(detail.get("carrierClaim") or {})
                        carrier_claim.update({"status": "confirmed",
                                              "confirmedAt": now_ts()})
                        log.detail = {**detail, "carrierClaim": carrier_claim}
                        log.status = "success"
                        log.reason = "认领已在目标采集箱确认"
                        log.finished_at = now_ts()
                else:
                    reason = (f"保存回执已返回，但采集箱核对失败：{verification_error}；"
                              if verification_error else "已保存但采集箱列表暂未确认；")
                    errors_by_index.setdefault(
                        source_index,
                        reason + "请先刷新采集箱核对，勿重复认领")
        finally:
            client.close()

        results: list[dict[str, Any]] = []
        stamp = now_ts()
        for index, (detail_id, shop_id) in enumerate(normalized):
            target_detail_id = detail_by_source.get(
                index, reconciled_by_index.get(index))
            product = by_key.get(product_key(detail_id, shop_id))
            collected: dict[str, Any] = {}
            if target_detail_id and product:
                collected = {"at": stamp, "detailId": target_detail_id,
                             "shopId": str(target_shop_id)}
                _record_carrier_claim(
                    db, account, product, detail_id=target_detail_id,
                    shop_id=str(target_shop_id), operator_id=operator_id,
                    claimed_at=stamp)
            if not target_detail_id and index in payload_indexes:
                source_index = payload_indexes.index(index)
                failure = next((item for item in (filled.get("failureDetails") or [])
                                if item.get("sourceIndex") == source_index), None)
                if failure and failure.get("certain") is True:
                    detail_id_source, _source_shop_id = normalized[index]
                    log = (db.query(PublishLog)
                           .filter(PublishLog.account_id == account.id,
                                   PublishLog.detail_id == detail_id_source,
                                   PublishLog.shop_id == str(target_shop_id),
                                   owner_filter)
                           .order_by(PublishLog.id.desc()).first())
                    if log is not None:
                        log.status = "fail"
                        log.reason = str(failure.get("reason") or "载体覆盖失败")[:200]
                        log.finished_at = now_ts()
                        detail = dict(log.detail or {})
                        carrier_claim = dict(detail.get("carrierClaim") or {})
                        carrier_claim.update({"status": "failed"})
                        log.detail = {**detail, "carrierClaim": carrier_claim}
            results.append({
                "claimed": target_detail_id is not None and index not in errors_by_index,
                "detailId": detail_id,
                "fromShopId": shop_id,
                "targetShopId": str(target_shop_id),
                "targetShopName": target_shop.get("name") or "",
                "sourceShopName": source_shops[shop_id].get("name") or "",
                "mode": "carrier",
                "count": 1 if target_detail_id else 0,
                "detailIds": [target_detail_id] if target_detail_id else [],
                "failures": ([errors_by_index[index]] if index in errors_by_index else []),
                "collected": collected,
            })
        invalidate_account(account)
        db.flush()
        return results


def _stored_claim_payload(product: Product | None) -> dict[str, Any]:
    """Use the warehouse snapshot as the source when rebuilding a finished item."""
    if product is None or not isinstance(product.payload, dict) or not product.payload:
        return {}
    payload = dict(product.payload)
    for key, value in (("title", product.title), ("site", product.site),
                       ("cid", product.cid), ("imgUrls", product.images),
                       ("skuPropertyList", product.sku_properties)):
        if not payload.get(key) and value:
            payload[key] = value
    return payload


def _claim_payload_is_complete(payload: Mapping[str, Any]) -> bool:
    """A stored finished snapshot can stand in for a deleted upstream source."""
    if not all(str(payload.get(key) or "").strip()
               for key in ("title", "site", "cid")):
        return False
    images = payload.get("imgUrls")
    if not isinstance(images, list) or not any(str(value or "").strip() for value in images):
        return False
    sku_map = payload.get("skuMap")
    if not isinstance(sku_map, Mapping):
        return False
    return any(isinstance(sku, Mapping) and str(sku.get("isDelete", "0")) != "1"
               for sku in sku_map.values())


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
               .filter(Product.account_id == account.id,
                       Product.warehouse_key == key,
                       Product.deleted_at.is_(None))
               .one_or_none())
    if product is None:
        raise errors.not_found(f"仓库里没有这件商品：{key}", key=key)
    state = dict(product.collected or {}) if isinstance(product.collected, dict) else {}
    # 只退本账号那一条：跨店认领时 collected 是按账号名分键的，
    # 整份清空会把别的账号的认领记录也抹掉。
    # New writes use the immutable account id.  Also remove the old display
    # name key so accounts created by older builds can still be released after
    # upgrading (and so a renamed account does not leave a stale claim behind).
    removed = state.pop(_account_claim_key(account), None)
    legacy_removed = state.pop(account.name, None) if account.name else None
    if removed is None:
        removed = legacy_removed
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
