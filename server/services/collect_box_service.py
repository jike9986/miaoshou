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
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
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
from services import carrier_service, product_service, source_link, warehouse_service
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

# 批量认领 site 商品时每批带多少件（2026-11-21）。
#
# 上游 claim_to_shop 收数组，但单次长度有上限，而且要过我们自己的 Pacer
# （0.90 秒/次）。取 20：47 件 = 3 批，约 3 秒认领完，用户几乎无感；
# 再大没有明显收益，反而在一次失败时连累更多件。
SITE_CLAIM_BATCH = 20


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
    """缓存键。

    注意 page / page_size 虽然还收着，但**不参与 key**（2026-11-27 改）：
    `list_items` 现在缓存的是整份快照、翻页在本地切片，同一组筛选条件
    所有页共用一个条目。参数留着是为了不破坏 `test_cache_force_refresh`
    等按页调用 key 的既有测试。

    改成全量快照的原因：原来每页各打一次妙手，而取第 N 页必须从上游第 1 页
    重扫，页越深越慢（实测 pageSize=20 第 1 页 1.26s、第 2 页 2.7s，
    pageSize=100 第 2 页 9.9s）。负责人反馈的「采集箱要等很久才会加载出来」
    正是这个。
    """
    return (
        f"{account_cache_prefix(account)}"
        f"status={status}"
        f"|kw={str(keyword or '').strip().casefold()}"
        f"|shop={str(shop_id or '').strip()}"
        f"|site={str(site or '').strip().upper()}"
        f"|sort={sort}"
    )


def _scan_all(account: Account, *, status: str, keyword: str,
              shop_id: str | None, site: str | None) -> dict[str, Any]:
    """把所有上游页拉完，返回过滤后的完整列表（供本地切片）。

    与 `_scan_upstream` 的分工：后者保持「取第 N 页」的既有契约不变
    （`test_collect_box_pagination` 的 9 个用例锁的就是它），本函数专门服务
    `list_items` 的全量快照。翻页因此变成纯内存操作。
    """
    upstream_page_size = COLLECT_BOX_UPSTREAM_PAGE
    client = account_client(account)
    try:
        upstream_status = None if status == "all" else status
        request_page = 1
        items: list[dict[str, Any]] = []
        scanned = 0
        seen: set[str] = set()
        while True:
            response = client.collect_box_list(
                status=upstream_status, keyword=keyword or "",
                page_no=request_page, page_size=upstream_page_size)
            data = (response or {}).get("data") or {}
            upstream_rows = data.get("detailList") or []
            rows = listing_rows(response)
            scanned += len(rows)
            for row in rows:
                if shop_id and str(row["shopId"]) != str(shop_id):
                    continue
                if site and str(row["site"]).upper() != str(site).upper():
                    continue
                if keyword and not _matches_keyword(row, keyword):
                    continue
                # listing_rows 会把一个商品按店铺展开成多行，key 是唯一身份。
                # 上游翻页偶尔给重复行，这里兜一下，别让列表出现同一商品两次。
                row_key = str(row.get("key") or "")
                if row_key and row_key in seen:
                    continue
                if row_key:
                    seen.add(row_key)
                items.append(row)
            if len(upstream_rows) < upstream_page_size:
                break
            request_page += 1
            if request_page > 40:  # 兜底：500 x 40 = 2 万件
                break
    finally:
        client.close()
    return {"items": items, "total": len(items), "scanned": scanned}



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
    """collect_box_list → 每行 (key, detailId, shopId, shopName, site, ...)。

    ## 两种 editModel（2026-11-21 修）

    妙手采集箱的商品有两种存在形态：

    **shop 模式**（`editModel='shop'`）
        已经分配到某个店铺，行里有 `collectBoxDetailShopList`。
        这是原来唯一支持的模式。

    **site 模式**（`editModel='site'`）
        只采集到采集箱、**还没分配到店铺**，行里没有 shop 列表、
        `shopId` 也是 None。数据本身是完整的（标题/图/价格/库存/
        commonCollectBoxDetailId 全都有），只是缺店铺归属。

    ### 原来为什么出 bug

    旧代码 `if row.get("editModel") not in (None, "shop"): continue` 把
    site 模式整行丢弃。但 `_scan_upstream` 的 total 用的是**原始行数**
    （raw_count，不受这个过滤影响）。

    结果就是负责人看到的：**「采集箱 / 49」但列表空白**。
    账号 8 的 49 条全是 site 模式，一条都没显示出来。

    ### 现在怎么处理

    site 模式的商品**照常返回**，用 `site` 作为店铺维度的替代标识：

        key     = f"{detailId}:site:{site}"
        shopId  = ""（空字符串，前端和下游据此识别「未分配店铺」）
        shopName= "未分配店铺（{site}）"

    这样用户至少能看到商品、能选中、能删除。真正要发布时再走
    `claim_to_shop`（`site_detail` 返回的 `claimToShopIds` 提供候选店铺）。

    注意 `site` 在行里通常是 None，需要从 `platform` / `siteCode` 兜底，
    并且 `site_detail` 只在账号真正绑定了该站点时才成功 ——
    账号 8 实测 MY/PH 可取、SG/TH/ID/VN 报「当前站点没有绑定的店」。
    """
    data = (response or {}).get("data") or {}
    rows = data.get("detailList")
    if not isinstance(rows, list):
        raise errors.upstream("采集箱返回结构异常", reason="detailList_missing")
    products: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        model = str(row.get("editModel") or "shop")
        # 不能用 `or`：`0` 是合法 detailId 但是 falsy，`a or b` 会把它跳过、
        # 继续取下一个键，最后拿到 None 然后整行被丢弃。
        # 线上 detailId 不会真的是 0，但测试和无害输入会踩到，行为必须正确。
        detail_raw = row.get("collectBoxDetailId")
        if detail_raw in (None, ""):
            detail_raw = row.get("detailId")

        if model == "site":
            # site 模式：没有店铺，用 site 作为维度
            try:
                detail_id = int(detail_raw)
            except (TypeError, ValueError):
                continue
            site = first_text(row.get("site"), row.get("siteCode"),
                              row.get("platform")).upper()
            key = f"{detail_id}:site:{site}"
            if key in seen:
                continue
            seen.add(key)
            products.append({
                "key": key,
                "detailId": str(detail_id),
                # 空字符串显式表示「还没分配到店铺」，前端据此显示提示
                "shopId": "",
                "shopName": f"未分配店铺（{site}）" if site else "未分配店铺",
                "site": site,
                "title": _row_title(row),
                "imageUrl": _row_image(row),
                "sourceId": first_text(row.get("sourceItemId"),
                                       row.get("sourceId")) or None,
                # 同上：公共采集箱 ID，货源链接的唯一来源
                "commonCollectBoxDetailId": first_text(
                    row.get("commonCollectBoxDetailId")) or None,
                "status": "未发布",
                "editModel": "site",
            })
            if len(products) >= COLLECT_BOX_MAX_ITEMS:
                return products
            continue

        if model != "shop":
            # 未知模式：保留原来的保守行为（跳过），避免把不认识的
            # 结构塞进列表导致下游崩。
            continue

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
                # 公共采集箱明细 ID —— 货源链接的唯一可靠来源（2026-11-21）。
                # 店铺侧完全不带 1688 信息，只有这个 ID 能查到
                # editCommonCollectBoxDetail.sourceList[0].sourceItemUrl。
                # 它只存在于列表行里，所以必须从这里一路传到入仓。
                "commonCollectBoxDetailId": first_text(
                    row.get("commonCollectBoxDetailId")) or None,
                "status": "未发布",
                "editModel": "shop",
            })
            if len(products) >= _listing_row_limit():
                return products
    return products


def _listing_row_limit() -> int:
    """`listing_rows` 最多展开多少行。

    原来是硬编码的 `COLLECT_BOX_MAX_ITEMS`（=100），因为调用方每次都只取
    一页（≤100 条），提前截断省事。

    2026-11-27 全量快照后这条限制变成了 bug：`_scan_all` 一次拉 231 行，
    `listing_rows` 走到第 100 行就 `return`，剩下 131 件商品凭空消失，
    `total` 也跟着变成 100 —— 负责人会看到「采集箱 / 100」但实际有 231 件。

    现在放宽到一次上游能返回的上限（`COLLECT_BOX_UPSTREAM_PAGE` ×
    `_scan_all` 最多翻的页数）。真要限流应该限**对外返回**的
    `pageSize`（那是 `COLLECT_BOX_PAGE_MAX` 的事），不该在数据源这一层
    把商品丢掉。
    """
    return COLLECT_BOX_UPSTREAM_PAGE * 40


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
            total = int(data.get("total") or data.get("totalCount") or 0)
            if (len(upstream_rows) < COLLECT_BOX_UPSTREAM_PAGE
                    or (total and page_no * COLLECT_BOX_UPSTREAM_PAGE >= total)):
                break
            page_no += 1
        if expected <= found or attempt >= retries:
            break
        time.sleep(max(0.0, retry_delay))
    return found


def _scan_upstream(account: Account, *, page: int, page_size: int, status: str,
                   keyword: str, shop_id: str | None, site: str | None,
                   count_all: bool = False
                   ) -> dict[str, Any]:
    """真正打妙手的那一段（缓存未命中时才走这里）。返回值可 JSON 化。

    count_all=True：用 pageSize=500 一次尽量拉完所有商品，以 raw_count（原始行数）
    作为 total，不受 listing_rows 的 editModel/shopId 过滤影响，不依赖上游 total 字段。
    count_all=False（默认）：凑够本页所需 items 就停，速度快。
    """
    # 妙手接口 pageSize 上限 500；count_all 时用最大值以减少翻页次数
    upstream_page_size = 500 if count_all else COLLECT_BOX_UPSTREAM_PAGE
    client = account_client(account)
    try:
        upstream_status = None if status == "all" else status
        # 上游按原始商品分页，listing_rows 还会按店铺展开，因此不能把客户端
        # 页码直接传过去；筛选后的行必须先跳过前面各页，再取本页。
        request_page = 1
        offset = (page - 1) * page_size
        matched = 0
        raw_count = 0  # 原始 detailList 行数（不受 editModel 过滤影响）
        items: list[dict[str, Any]] = []
        scanned = 0
        has_more = False
        items_done = False  # 本页所需 items 已凑够
        while True:
            response = client.collect_box_list(
                status=upstream_status, keyword=keyword or "",
                page_no=request_page, page_size=upstream_page_size)
            data = (response or {}).get("data") or {}
            upstream_rows = data.get("detailList") or []
            raw_count += len(upstream_rows)  # 原始行数，含 site 模式等
            rows = listing_rows(response)
            scanned += len(rows)
            for row in rows:
                if shop_id and str(row["shopId"]) != str(shop_id):
                    continue
                if site and str(row["site"]).upper() != str(site).upper():
                    continue
                if keyword and not _matches_keyword(row, keyword):
                    continue
                matched += 1
                if items_done or matched <= offset:
                    continue
                if len(items) >= page_size:
                    has_more = True
                    items_done = True
                else:
                    items.append(row)
            if not count_all and (has_more or items_done):
                # 快速模式：items 已经凑够，不必继续翻页
                break
            # 上游返回行数不足一页说明到底了
            if len(upstream_rows) < upstream_page_size:
                break
            request_page += 1
    finally:
        client.close()
    # count_all 模式：用原始行数（不受 editModel 过滤）作为总数，贴近妙手界面显示的数字
    # 普通模式：用 matched（已通过过滤的行数）作为 total
    total = raw_count if count_all else matched
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
            lambda: _scan_all(account, status=status, keyword=keyword,
                              shop_id=shop_id, site=site),
            ttl=cache_ttl(),
            force_refresh=force_refresh,
            stale_on=MiaoshouError,
        )
    except MiaoshouError as exc:
        raise map_upstream_error(exc) from None

    snapshot = outcome.value or {}
    # 缓存里是整份快照，翻页在本地切片（2026-11-27）。
    all_items = list(snapshot.get("items") or [])
    total = int(snapshot.get("total") or 0)
    scanned = int(snapshot.get("scanned") or 0)

    # 仓库标注查的是本地库，必须每次按当前库状态重算，不能跟着缓存走。
    # 在切片**之前**标注：savedDesc 排序要用到 inWarehouse，切片后再排只排了本页。
    _annotate_warehouse(db, account, all_items)
    if sort == "savedDesc":
        all_items.sort(key=lambda row: row.get("inWarehouse", False), reverse=True)

    start = (page - 1) * page_size
    items = all_items[start:start + page_size]
    return {
        "items": items,
        "page": page,
        "pageSize": page_size,
        "total": max(total, len(all_items)),
        "hasMore": start + page_size < len(all_items),
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


def claim_site_batch(db: Session, account: Account, *, target_shop_id: str,
                     detail_ids: list[str] | None = None,
                     max_items: int = 200) -> dict[str, Any]:
    """把采集箱里**所有还没分配店铺**的商品批量认领到目标店铺。

    ## 负责人要求（2026-11-21）

    「打开这个软件如果有未分配的自动分配，在还没有开始自动化处理就把这些
    产品全部自动弄好」

    也就是：软件一打开就先把 site 商品分好店，而不是等到用户点「开始自动化」
    时才逐件认领。这样用户看到的采集箱永远是「都能直接处理」的状态。

    ## 为什么要分批 + 为什么要限流

    `claim_to_shop(detailIds, shopIds)` 一次收一批，实测账号 8 有 47 条、
    账号 2 有 6 条。但上游对单次数组长度有上限（载体流程里也是分批调），
    而且这接口在我们自己的 Pacer 里是要限速的（0.90 秒/次）。

    所以：
      · 每批 `SITE_CLAIM_BATCH` 件（保守取 20）
      · 批之间走 Pacer 的节奏，不硬刷上游
      · 整体给一个 `max_items` 上限，防止某个账号的采集箱异常大时
        在启动阶段跑太久

    ## 幂等与容错

    · 已经分好店的商品不会出现在 site 列表里，天然跳过
    · 上游对「已在目标店铺」报错时当成功（`_is_already_claimed`）
    · 单批失败不整体中断：记下失败件数，继续下一批。
      启动阶段不能因为一条脏数据就放弃所有商品。

    返回：{"claimed": n, "failed": n, "skipped": n, "total": n, "errors": [...]}
    """
    if not str(target_shop_id or "").strip():
        raise errors.validation("请选择要认领到哪个店铺", field="targetShopId")
    require_shop(db, account, target_shop_id)

    client = account_client(account)
    try:
        # 先扫描出全部 site 商品（不受分页影响：用大 pageSize 一次拿）
        try:
            response = client.collect_box_list(
                status="notPublished", keyword="", page_no=1, page_size=500)
        except MiaoshouError as exc:
            raise errors.upstream(
                f"读取采集箱失败：{exc}", reason="collect_box_scan_failed") from None

        site_items = [row for row in listing_rows(response)
                      if row.get("editModel") == "site"]
        if detail_ids is not None:
            wanted = {str(value).strip() for value in detail_ids}
            site_items = [row for row in site_items if row["detailId"] in wanted]

        total = len(site_items)
        if total > max_items:
            site_items = site_items[:max_items]

        claimed = 0
        failed = 0
        error_messages: list[str] = []

        for start in range(0, len(site_items), SITE_CLAIM_BATCH):
            batch = [int(row["detailId"])
                     for row in site_items[start:start + SITE_CLAIM_BATCH]]
            if not batch:
                continue
            try:
                client.claim_to_shop(batch, [int(target_shop_id)])
                claimed += len(batch)
            except MiaoshouError as exc:
                if _is_already_claimed(str(exc)):
                    # 这批里有些已经在目标店铺里了。逐件重试，
                    # 好让「部分成功」的批也能记准件数。
                    for detail_id in batch:
                        try:
                            client.claim_to_shop([detail_id], [int(target_shop_id)])
                            claimed += 1
                        except MiaoshouError as inner:
                            if _is_already_claimed(str(inner)):
                                claimed += 1
                            else:
                                failed += 1
                                error_messages.append(f"{detail_id}: {inner}")
                else:
                    failed += len(batch)
                    error_messages.append(f"批 {start}-{start + len(batch)}: {exc}")

        if claimed:
            invalidate_account(account)
        return {
            "claimed": claimed,
            "failed": failed,
            "skipped": max(0, total - claimed - failed),
            "total": total,
            "targetShopId": str(target_shop_id),
            "errors": error_messages[:10],
        }
    finally:
        client.close()


def _is_already_claimed(message: str) -> bool:
    """上游「已经在目标店铺采集箱里」这类报错，语义上是幂等成功。

    中文和英文都要覆盖：妙手的报错是中文（「该商品已在采集箱中」），
    但上游偶发会透出英文（`already exists`），两种都当成成功，
    否则批量认领会把「其实已经好了」的件计成失败，用户看到一堆假失败。
    """
    text = str(message or "").lower()
    has_already = ("已" in text or "已经" in text
                   or "already" in text or "exist" in text)
    has_target = ("采集箱" in text or "店铺" in text or "存在" in text
                  or "shop" in text or "collect" in text)
    return has_already and has_target


def claim_site_item(db: Session, account: Account, *, detail_id: str,
                    target_shop_id: str) -> str:
    """把一件 **site 模式**（还没分配店铺）的采集箱商品认领到目标店铺。

    ## 为什么要这个函数（2026-11-21）

    负责人要求：「自动化处理不能默认给他选择一个店铺吗，加到文字处理里面」

    背景：妙手采集箱的商品有两种形态（见 `listing_rows` 注释）：
      · `shop` —— 已分配到店铺
      · `site` —— 只采集到采集箱、没有店铺归属，`shopId` 是 None

    `process` 接口必须拿到 `shopId` 才能入库，所以 site 模式的商品
    原来根本没法处理 —— 只能让用户去妙手界面手动分配。

    实测 `claim_to_shop([detailId], [shopId])` 对 site 商品**有效**：

        认领前: editModel=site, shopId=''      key=3549599651:site:TIKTOK
        claim_to_shop([3549599651], [19974446]) → {'result': 'success'}
        认领后: editModel=shop, shopId=19974446  key=3549599651:19974446

    所以可以在处理前自动认领一步，让 site 商品也能走自动化。

    ## 和 `claim()` 的区别

    `claim()` 是「把 A 店铺的商品认领到 B 店铺」，要求源 `shop_id` 是数字，
    还有源/目标店铺校验、本地 claimed_at 账本、跨进程窗口。

    这里是「把**没有店铺**的商品第一次分配出去」，源店铺不存在，
    所以：
      · 不做源店铺校验（它本来就没有）
      · 不写 claimed_at 账本（那是防「同一件被两人抢到同一店」，
        而这里认领后 editModel 就变成 shop，上游天然幂等）
      · 但仍要校验目标店铺属于该账号

    返回：认领后的 shopId（字符串）。失败抛 AppError。
    """
    if not str(target_shop_id or "").strip():
        raise errors.validation("请选择要认领到哪个店铺", field="targetShopId")
    if not str(detail_id or "").strip().isdigit():
        raise errors.validation("detailId 必须是数字", field="detailId",
                                value=detail_id)

    # 目标店铺必须属于这个账号 —— 否则上游会失败，但错误信息很难懂
    require_shop(db, account, target_shop_id)
    client = account_client(account)
    try:
        try:
            client.claim_to_shop([int(detail_id)], [int(target_shop_id)])
        except MiaoshouError as exc:
            message = str(exc)
            # 已经在这个店铺里了：不算错误，幂等放过
            if _is_already_claimed(message):
                return str(target_shop_id)
            raise errors.upstream(
                f"认领到店铺失败：{message}", reason="claim_site_failed") from None
    finally:
        client.close()
    return str(target_shop_id)


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
                carriers = carrier_service.collect_carriers(
                    db, client, count=1,
                    # 载体用这件商品自己的 1688 货源（2026-11-20 修）：
                    # info 上面已合并仓库 payload，货源链接就在里面。
                    # 拿不到就走池子轮换，不阻断。
                    preferred_urls=source_link.source_urls_of([info]))
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
                # 认领到采集箱：价格必须是「拿货价换算成目标站点币种」，
                # 不能套定价模板。
                #
                # 负责人口径（2026-11-18）：
                #   「从成品仓库认领到采集箱的，后面会在妙手套定价模板再改价的」
                #   「成品仓库显示的价格都是拿货价，导入采集箱应该也是拿货价」
                #
                # 原来这里直接用仓库 payload 里已有的 skuMap.price —— 那个价是
                # **入仓时套模板算出来的售价**（马来模板会把 22 CNY 算成 40+ MYR），
                # 于是「认领」看起来像「莫名其妙涨到 50 多」。
                # 真正的定价发生在妙手那边套模板，不在这里。
                adjustments = list(adjustments) + _reset_claim_prices_to_purchase(
                    db, account, payloads, claim_entries,
                    target_shop_id=str(target_shop_id))
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




def _reset_claim_prices_to_purchase(
        db: Session, account: Account, payloads: list[dict[str, Any]],
        entries: list[dict[str, Any]], *, target_shop_id: str
) -> list[dict[str, Any]]:
    """认领前把 skuMap.price 重置成「拿货价按目标站点汇率换算」的结果。

    为什么必须重置（负责人 2026-11-18）：
      · 成品仓库卡片显示的是拿货价（purchasePriceMin/Max，币种 CNY）；
      · 但仓库 payload 里的 skuMap.price 是**入仓时套定价模板算出的售价**；
      · 认领直接沿用 payload，于是「拿货价 22 CNY 的商品」进采集箱后变成
        40+ MYR（≈67 CNY），看起来像凭空涨价。

    真正的定价发生在妙手那边套模板，认领这一步只需要把拿货价换算成
    目标站点币种。妙手若判定低于成本下限会自己报错，调用方再按
    「下限 + 0.01」兜底（见 carrier_service._raise_price_to_reported_floor）。

    换算规则：
      · 目标币种 = 目标店铺站点对应币种（MY→MYR / PH→PHP / TH→THB ...）；
      · 拿货价优先取 originPrice（CNY），缺失时退回采购价字段；
      · priceIncludeVat 同步成同一个数（妙手按含税价展示）。
    """
    from services import exchange_rate

    adjustments: list[dict[str, Any]] = []
    try:
        shop = require_shop(db, account, target_shop_id)
    except Exception:  # noqa: BLE001 - 拿不到站点就退回 MYR，不阻断认领
        shop = None
    site = str((shop or {}).get("site") or "MY").strip().upper()
    currency = exchange_rate.currency_for_site(site)

    rate, rate_source = exchange_rate.cached_rate(currency=currency)
    if rate is None:
        rate, rate_source = exchange_rate.live_rate(currency=currency)
    if rate is None:
        rate = exchange_rate.fallback_rate(currency)
        rate_source = "内置兜底"
    rate_d = Decimal(str(rate))

    for index, payload in enumerate(payloads):
        sku_map = payload.get("skuMap")
        if not isinstance(sku_map, dict) or not sku_map:
            continue
        changed = 0
        for sku in sku_map.values():
            if not isinstance(sku, dict) or str(sku.get("isDelete", "0")) == "1":
                continue
            purchase = sku.get("originPrice")
            if purchase in (None, ""):
                continue
            try:
                purchase_d = Decimal(str(purchase))
            except (InvalidOperation, TypeError, ValueError):
                continue
            if purchase_d <= 0:
                continue
            # 加价在**人民币**层面（负责人 2026-11-25：
            # 「认领到采集箱不是拿货价增一点点，比如 23 就 27、28 人民币」）。
            #
            # 23 CNY × 1.18 = 27.14 CNY → 16.70 MYR。
            # 先换算再加价是错的：那样加的是「外币的 18%」，
            # 折回人民币看就不是「加一点点」，与负责人的口径对不上。
            # 系数与 carrier_service._raise_price_to_reported_floor 同一来源。
            quoted = (purchase_d * carrier_service.CLAIM_PRICE_BUMP).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP)
            local = (quoted / rate_d).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP)
            old = sku.get("price")
            try:
                old_d = Decimal(str(old))
            except (InvalidOperation, TypeError, ValueError):
                old_d = None
            if old_d is not None and old_d == local:
                continue
            sku["price"] = format(local, ".2f")
            sku["priceIncludeVat"] = format(local, ".2f")
            changed += 1
        if changed:
            entry = entries[index] if index < len(entries) else {}
            adjustments.append({
                "warehouseId": entry.get("warehouseId"),
                "stage": "claimPrice",
                "site": site,
                "currency": currency,
                "rate": str(rate_d),
                "rateSource": rate_source,
                "skuCount": changed,
            })
    return adjustments


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
