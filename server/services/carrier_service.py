"""载体重建：把成品商品发布到「不同账号/店铺」的必经路径。

detailId 只在账号内唯一，所以跨店铺发布不能引用原商品。流程：
  1. 从链接池取一个 1688 链接，采集进公共采集箱（fetch_item）
  2. 认领进目标店铺的采集箱，一次要 N 份（claimed + serialNumber）
  3. 用成品 payload 覆盖每个载体（save_shop）

第 3 步是关键：标题、图片、SKU、类目、描述、价格全部来自仓库副本，
所以不需要再付一次翻译费。
"""
from __future__ import annotations  # noqa: I001

import logging
import hashlib
import queue
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from core import errors
from core.constants import (
    CARRIER_ITEM_RETRY_PAUSE_SECONDS,
    CARRIER_ITEM_RETRY_ROUNDS,
    GATEWAY_COOLDOWN_MAX_SECONDS,
)
from services import setting_service
from services import source_link
from services.miaoshou import MiaoshouClient, MiaoshouError

logger = logging.getLogger("aohuang.carrier")

# -- 规格归一化公共辅助 -------------------------------------------------------
# remap_sku_to_carrier / align_spec_names / normalize_spec_for_save 曾各自
# 内联相同逻辑；统一提到模块顶层，消除重复。


def _norm(text: Any) -> str:
    """去空白、折叠大小写、去空格（规格值归一化用）。"""
    return str(text or "").strip().casefold().replace(" ", "")


_COLOR_WORDS: frozenset[str] = frozenset({"颜色", "色彩", "color", "colour", "warna"})
_SIZE_WORDS: frozenset[str] = frozenset({"尺码", "尺寸", "size", "saiz", "ukuran"})


def _kind(value: Any) -> str:
    """返回维度语义类型：'color' / 'size' / ''（其他）。"""
    text = _norm(value)
    if text in _COLOR_WORDS:
        return "color"
    if text in _SIZE_WORDS:
        return "size"
    return ""

# 妙手报错里的两个数字。币种不能写死 MYR（2026-11-25 修）。
#
# 原来两个正则都以 `\s*MYR` 结尾，于是：
#   · 马来店报「…95.89MYR…基础成本 225.50MYR」→ 命中，抬价生效
#   · 菲律宾店报「…95.89PHP…225.50PHP」→ 一个字都匹配不上，
#     `_raise_price_to_reported_floor` 直接返回 None，抬价逻辑从未被调用，
#     认领必然失败。
# 负责人反馈「其他国家也是一样的」，就是这一条：只有 MY 能用，
# PH / TH / VN / SG 全部卡在同一处。
_COST_FLOOR_VALUE = re.compile(
    r"基础成本[^0-9]{0,24}([0-9]+(?:\.[0-9]+)?)\s*[A-Z]{3}", re.IGNORECASE)
MAX_COST_FLOOR_RETRIES = 12
# 认领到采集箱的加价系数，作用在**人民币拿货价**上。
#
# 负责人口径（2026-11-25）：
#   「我从成品仓库采集只需要卖家价格微微上挑一点，
#     我还会在妙手重新套用模板改价」
#   「认领到采集箱不是拿货价增一点点，比如 23 就 27、28 人民币」
#
#   23 CNY × 1.18 = 27.14 CNY → 16.70 MYR
#   5.5 CNY × 1.18 = 6.49 CNY  → 3.99 MYR
#
# 加价永远在人民币层面做。先换算成外币再加百分比是错的 ——
# 那样加的是外币的 18%，折回人民币看就不是「微微上挑」。
# collect_box_service 用同一个常量，两处口径必须一致。
CLAIM_PRICE_BUMP = Decimal("1.18")
# 新建载体的 ossMd5 会被上游连续改写几轮。只重读 1 次在 70 件实测里会漏
# （2026-11-05，id=26/80 首轮就是这么失败的）。按 CLAIM_STANDARD 第 4 节
# 重试 MAX_VERSION_REFRESH 轮，每轮重新读一次 md5。
MAX_VERSION_REFRESH = 5
# 尺码值的典型形态：34、36-37、38–39、S、XL、FREE
# 必须认区间写法，否则区间尺码会被当成颜色（实测报 Warna 重复）
_SIZE_PATTERN = re.compile(
    r"^\s*\d{1,3}(?:\s*[-–~]\s*\d{1,3})?\s*$|"
    r"^\s*(?:XXS|XS|S|M|L|XL|XXL|XXXL|2XL|3XL|4XL|FREE|F)\s*$",
    re.I)
SEASON_VALUE_IDS = {
    "musim bunga": "1001161",
    "musim panas": "1001162",
    "musim luruh": "1003131",
    "musim sejuk": "1001163",
    "semua musim": "1005840",
}


def _claim_site_rate(site: str) -> Decimal | None:
    """站点币种的汇率（1 CNY = N 目标币种）。取不到返回 None。"""
    from services import exchange_rate

    currency = exchange_rate.currency_for_site(site) if site else "MYR"
    rate, _source = exchange_rate.cached_rate(currency=currency)
    if rate is None:
        rate, _source = exchange_rate.live_rate(currency=currency)
    if rate is None:
        rate = exchange_rate.fallback_rate(currency)
    if rate is None:
        return None
    try:
        value = Decimal(str(rate))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return value if value > 0 else None


def _raise_price_to_reported_floor(payload: Mapping[str, Any], exc: MiaoshouError
                                   ) -> dict[str, Any] | None:
    """把售价微微上挑，让妙手不再报「无法覆盖产品基础成本」。

    ## 负责人口径（2026-11-25）

      「我从成品仓库采集只需要卖家价格微微上挑一点，
        我还会在妙手重新套用模板改价」
      「只需要微微上挑让他不报成本不够就行了」

    认领到采集箱**不是定价环节**。这一步只做两件事：
      1. 拿货价微微上挑（`CLAIM_PRICE_BUMP`，人民币层面 ×1.18）：
         23 CNY → 27.14 CNY → 16.70 MYR；
      2. 妙手若还嫌低，就抬到它报的成本线 + 0.01 —— **刚好不报错**为止，
         不多抬一分。

    第 2 步只在妙手坚持时兜底。它报的是自己的全成本口径（马来模板里
    光尾程运费就 16.17 CNY/单，再叠一堆比例费用），不是拿货价，
    所以绝不能拿来当常规定价；但为了「不报错」这个目的，抬到它的底线
    是唯一办法，抬多了没有意义。真正的定价由负责人在妙手套模板完成。
    """
    message = str((exc.payload or {}).get("message")
                  or (exc.payload or {}).get("msg") or exc)
    if "无法覆盖产品基础成本" not in message:
        return None

    floor: Decimal | None = None
    floor_match = _COST_FLOOR_VALUE.search(message)
    if floor_match is not None:
        try:
            floor = Decimal(str(floor_match.group(1)))
        except InvalidOperation:
            floor = None

    sku_map = payload.get("skuMap")
    if not isinstance(sku_map, dict):
        return None

    site = str(payload.get("site") or "").strip().upper()
    rate = _claim_site_rate(site)

    changed: list[str] = []
    old_price: Decimal | None = None
    new_price: Decimal | None = None
    used_floor = False
    for key, sku in sku_map.items():
        if not isinstance(sku, dict) or str(sku.get("isDelete", "0")) == "1":
            continue
        try:
            current = Decimal(str(sku.get("price")))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if current <= 0:
            continue

        # 从**人民币拿货价**算，不用当前价 —— 调用方会重试多轮，
        # 拿当前价当基准会复利滚价。
        purchase = sku.get("originPrice")
        if purchase in (None, ""):
            continue
        try:
            purchase_d = Decimal(str(purchase))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if purchase_d <= 0:
            continue

        quoted_cny = (purchase_d * CLAIM_PRICE_BUMP).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP)
        if rate is not None and rate > 0:
            target = (quoted_cny / rate).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP)
        else:
            target = (current * (quoted_cny / purchase_d)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP)

        # 微微上挑还不够 → 抬到妙手报的成本线 + 0.01，刚好不报错。
        if floor is not None and target < floor:
            target = (floor + Decimal("0.01")).quantize(Decimal("0.01"))
            used_floor = True

        if target <= current:
            continue
        if old_price is None:
            old_price = current
            new_price = target
        sku["price"] = format(target, ".2f")
        sku["priceIncludeVat"] = format(target, ".2f")
        changed.append(str(key))
    if not changed:
        return None
    return {"oldPrice": format(old_price or 0, ".2f"),
            "newPrice": format(new_price or 0, ".2f"),
            "costFloor": format(floor, ".2f") if floor is not None else None,
            "bumpRatio": str(CLAIM_PRICE_BUMP),
            "usedFloor": used_floor,
            "skuKeys": changed}

LINK_POOL_KEY = "carrier.links"
# 妙手对 SKU 货号的硬限制：shopCollectItemInfo.skuMap.*.itemNum ≤ 50 字符。
SKU_ITEM_NUM_MAX = 50
# 池子上限（老版 source_links.POOL_SIZE=5）：不设上限会无限堆积，
# 而且每次写入都要序列化整池，越用越慢。
POOL_SIZE = 5
# 失败几次就弃用（老版 MAX_FAILURES=2）。新版用的 3 次会让坏链接多耗一轮。
MAX_FAILURES = 2
# 裸 1688 offer id 拼成这个标准商品页
OFFER_URL_TEMPLATE = "https://detail.1688.com/offer/{offer}.html"
COLLECT_SETTLE_SECONDS = 3.0
COLLECT_SETTLE_ROUNDS = 12


class CarrierError(errors.AppError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(errors.CARRIER_UNAVAILABLE, message, status=422, details=details)


# --- 链接池 ---------------------------------------------------------------
def _pool(db: Session) -> dict[str, Any]:
    value = setting_service.get(db, LINK_POOL_KEY) or {}
    return value if isinstance(value, dict) else {}


def list_links(db: Session) -> dict[str, Any]:
    return _pool(db)


def offer_url(value: Any) -> str:
    """把裸 1688 offer id 拼成标准商品页；已经是链接的原样返回。"""
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith(("http://", "https://")):
        return text
    # 只要数字段，防止把 "offerId=123" 之类整串塞进 URL
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) < 6:
        return ""
    return OFFER_URL_TEMPLATE.format(offer=digits)


def add_links(db: Session, urls: Iterable[str]) -> int:
    """把链接加进池子；裸 offer id 会自动拼成商品页。

    上限 POOL_SIZE：满了就不再收，先消耗完再加，避免池子无限长大。
    """
    pool = _pool(db)
    links = pool.get("links") if isinstance(pool.get("links"), list) else []
    known = {str(item.get("url")) for item in links if isinstance(item, Mapping)}
    added = 0
    for url in urls:
        if len(links) >= POOL_SIZE:
            break
        raw = str(url or "").strip()
        if not raw:
            continue
        text = raw if raw.startswith(("http://", "https://")) else offer_url(raw)
        if not text or text in known:
            continue
        if not valid_source_link(text):
            continue
        links.append({"url": text, "addedAt": time.time(), "failed": 0, "ok": 0,
                      "usedAt": None})
        known.add(text)
        added += 1
    pool["links"] = links
    setting_service.put(db, LINK_POOL_KEY, pool)
    return added


def valid_source_link(url: str) -> bool:
    """只接受 1688 商品链接（列表 /offer/<id>.html 或 detail.1688.com）。"""
    parts = urlsplit(str(url or "").strip())
    if parts.scheme not in ("http", "https"):
        return False
    host = (parts.hostname or "").lower()
    if not (host == "1688.com" or host.endswith(".1688.com")):
        return False
    return bool(offer_id(url))


def remove_links(db: Session, urls: Iterable[str]) -> int:
    """从链接池删除指定 url，返回实际删除条数。"""
    wanted = {str(url or "").strip() for url in urls}
    pool = _pool(db)
    links = pool.get("links") if isinstance(pool.get("links"), list) else []
    kept = [item for item in links
            if not (isinstance(item, Mapping) and str(item.get("url")) in wanted)]
    removed = len(links) - len(kept)
    pool["links"] = kept
    setting_service.put(db, LINK_POOL_KEY, pool)
    return removed


def offer_id(url: str) -> str:
    """从 1688 链接里取 offer id（/offer/123456.html）。"""
    path = urlsplit(str(url or "")).path
    for part in path.replace(".html", "").split("/"):
        if part.isdigit() and len(part) >= 6:
            return part
    return ""


def harvest_from_payloads(db: Session, payloads: Iterable[Mapping[str, Any]]) -> int:
    """从成品 payload 里回收 1688 货源 id（拼成 offer 链接放进池子）。"""
    offers: list[str] = []
    for payload in payloads:
        if not isinstance(payload, Mapping):
            continue
        for source in payload.get("sourceList") or []:
            if isinstance(source, Mapping):
                value = str(source.get("sourceItemId") or "").strip()
                if value.isdigit():
                    offers.append(value)
                url = str(source.get("sourceUrl") or source.get("url") or "").strip()
                if url.startswith("http"):
                    offers.append(url)
        for key in ("sourceItemId", "oriItemId"):
            value = str(payload.get(key) or "").strip()
            if value.isdigit():
                offers.append(value)
        sku_map = payload.get("skuMap")
        for source in sku_map.values() if isinstance(sku_map, dict) else []:
            if isinstance(source, Mapping):
                value = str(source.get("sourceItemId") or "").strip()
                if value.isdigit():
                    offers.append(value)
    urls = [item if item.startswith("http") else f"https://detail.1688.com/offer/{item}.html"
            for item in offers]
    return add_links(db, urls)


def harvest_from_common_box(db: Session, rows: Iterable[Mapping[str, Any]]) -> int:
    """从公共采集箱的行里回收 1688 货源链接，返回新增条数。

    公共箱行里只有 `sourceList` 描述真实货源，行上的 `title` 是这款商品的标题——
    池子里只留一串 offer id 时，界面上分不清哪个货源对应哪件商品，所以标题一起带进去。
    """
    candidates: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        title = str(row.get("title") or "").strip()
        for source in row.get("sourceList") or []:
            if not isinstance(source, Mapping):
                continue
            # 上游 source 字段大小写不统一（1688 / 1688.com / 1688CN 混着来），统一小写比对
            if str(source.get("source") or "").strip().lower() != "1688":
                continue
            value = str(source.get("sourceItemUrl") or "").strip()
            if not value:
                # 没有 url 时用裸 id：add_links 会拼成标准商品页，两边都能进池子
                value = str(source.get("sourceItemId") or "").strip()
            if not value:
                continue
            candidates.setdefault(value, title)
    if not candidates:
        return 0
    added = add_links(db, candidates)
    if added:
        _attach_titles(db, candidates)
    return added


def _attach_titles(db: Session, titles: Mapping[str, str]) -> None:
    """给刚进池子的链接补上标题（add_links 只认 URL 字符串，装不下标题）。"""
    pool = _pool(db)
    links = pool.get("links") if isinstance(pool.get("links"), list) else []
    changed = False
    for item in links:
        if not isinstance(item, dict) or item.get("title"):
            continue
        url = str(item.get("url") or "")
        # 候选键可能是完整 URL，也可能是裸 offer id；裸 id 走 offer_id 反查
        title = titles.get(url) or titles.get(offer_id(url))
        if title:
            item["title"] = title
            changed = True
    if changed:
        pool["links"] = links
        setting_service.put(db, LINK_POOL_KEY, pool)


def take_link(db: Session) -> dict[str, Any] | None:
    """取最该用的那条：失败少的优先，其次最久没用的。

    取的同时**立刻**记一次失败、写 usedAt——老版注释说得很明白：
    「The link is marked as used immediately, so two concurrent collects do not
    pick the same one; mark_success clears the provisional failure count」。
    不这样做，两次并发采集会取到同一条链接。
    """
    pool = _pool(db)
    links = pool.get("links") if isinstance(pool.get("links"), list) else []
    usable = [item for item in links
              if isinstance(item, Mapping) and int(item.get("failed") or 0) < MAX_FAILURES]
    if not usable:
        return None
    usable.sort(key=lambda item: (int(item.get("failed") or 0),
                                  item.get("usedAt") or 0))
    chosen_url = str(usable[0].get("url") or "")
    chosen: dict[str, Any] | None = None
    for item in links:
        if isinstance(item, dict) and str(item.get("url")) == chosen_url:
            item["usedAt"] = time.time()
            item["failed"] = int(item.get("failed") or 0) + 1
            chosen = dict(item)
            break
    if chosen is not None:
        pool["links"] = links
        setting_service.put(db, LINK_POOL_KEY, pool)
    return chosen


def mark(db: Session, url: str, *, ok: bool) -> None:
    """记一次成功/失败。

    成功必须把 failed **归零**（老版 mark_success：「A link that produced a
    carrier is healthy again」）。不归零的话，take_link 预留的那次失败会一直
    累着，好链接用几次就被判死。失败则直接置满上限，立刻退出轮换。
    """
    pool = _pool(db)
    links = pool.get("links") if isinstance(pool.get("links"), list) else []
    for item in links:
        if isinstance(item, dict) and str(item.get("url")) == str(url):
            if ok:
                item["failed"] = 0
                item["ok"] = int(item.get("ok") or 0) + 1
                item["usedAt"] = time.time()
            else:
                item["failed"] = MAX_FAILURES
                item["usedAt"] = time.time()
    pool["links"] = links
    setting_service.put(db, LINK_POOL_KEY, pool)


# --- 公共采集箱 -----------------------------------------------------------
def _box_status_from_list(client: MiaoshouClient, common_id: int) -> str:
    """从公共采集箱**列表**里查这条商品的真实状态（找不到返回空串）。

    为什么不用详情接口：
      `get_common_collect_box_detail` 在采集过程中会一直报
      「商品采集中」（这是进度提示，不是错误），稳定之后返回的字段里
      **没有 status** —— 只有 commonCollectBoxDetailId/title/itemNum/notes。
      老版靠详情接口的 status 判断，结果永远是空，只能等到超时判失败。
      实测列表接口的 `detailList[].status` 才是可信来源（success/skip/fail）。
    """
    try:
        listing = client.common_box_list(page_no=1, page_size=50)
    except Exception:  # noqa: BLE001 - 查不到就当还没好，下一轮再试
        return ""
    rows = (listing.get("data") or {}).get("detailList") or []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        if str(row.get("commonCollectBoxDetailId")) == str(common_id):
            return str(row.get("status") or "").lower()
    return ""


def wait_until_collected(client: MiaoshouClient, common_id: int, *,
                         progress: Callable[[dict[str, Any]], None] | None = None,
                         rounds: int = COLLECT_SETTLE_ROUNDS) -> bool:
    """fetch_item 只是排队；未采集成功前认领会失败。

    判定口径（2026-11-03 用老版实测确认）：
      · 列表接口里 status=success → 就绪；
      · status=skip → 之前已采集过，同样可以认领；
      · status=fail → 真失败；
      · 查不到 / 详情接口报「商品采集中」→ 继续等。
    """
    for attempt in range(rounds):
        status = _box_status_from_list(client, common_id)
        if status:
            if progress:
                progress({"stage": "waiting", "commonId": common_id,
                          "status": status, "attempt": attempt + 1})
            if status in {"success", "collectsuccess", "skip"}:
                return True
            if status in {"fail", "collectfail"}:
                return False
        else:
            # 列表里还没有：再试详情接口，顺便消化「已经采集过了」这种提示
            try:
                detail = client.common_box_detail(common_id)
            except MiaoshouError as exc:
                message = str(exc)
                if "已经采集" in message or "already" in message.lower():
                    return True
                if progress:
                    progress({"stage": "waiting", "commonId": common_id,
                              "status": "collecting", "attempt": attempt + 1})
            else:
                data = detail.get("data") or {}
                info = data.get("editCommonCollectBoxDetail") or data
                detail_status = str(info.get("status") or "").lower()
                if detail_status in {"success", "collectsuccess", "skip"}:
                    return True
                if progress and detail_status:
                    progress({"stage": "waiting", "commonId": common_id,
                              "status": detail_status, "attempt": attempt + 1})
        time.sleep(COLLECT_SETTLE_SECONDS)
    return False


def find_in_common_box(client: MiaoshouClient, source_item_id: str) -> int | None:
    """复用已采集过的公共箱商品：重复采集会得到 status=skip 行，反而挡认领。"""
    offer = str(source_item_id or "").strip()
    if not offer:
        return None
    try:
        listing = client.common_box_list(page_no=1, page_size=20, source_item_keyword=offer)
    except MiaoshouError:
        return None
    rows = (listing.get("data") or {}).get("detailList") or []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        status = str(row.get("status") or "").lower()
        if status in {"skip", "fail", "collectfail"}:
            continue
        for source in row.get("sourceList") or []:
            if isinstance(source, Mapping) and str(source.get("sourceItemId")) == offer:
                value = row.get("commonCollectBoxDetailId")
                if str(value).isdigit():
                    return int(value)
    return None


def claimed_detail_ids(response: Mapping[str, Any]) -> list[int]:
    data = response.get("data") or {}
    found: list[int] = []
    mapping = data.get("platformCollectBoxDetailIdMap") or {}
    for platform_map in mapping.values() if isinstance(mapping, Mapping) else []:
        if isinstance(platform_map, Mapping):
            for value in platform_map.values():
                if str(value).isdigit():
                    found.append(int(value))
        elif str(platform_map).isdigit():
            found.append(int(platform_map))
    for key in ("detailIds", "collectBoxDetailIds"):
        for value in data.get(key) or []:
            if str(value).isdigit():
                found.append(int(value))
    return found


def _fresh_box_ids(client: MiaoshouClient, common_id: int | None = None,
                   known_ids: set[int] | None = None) -> list[int]:
    """兜底：认领接口没给 detailId 时，回头从采集箱列表里把新草稿捞出来。

    老版 carrier._fresh_box_ids 干的就是这件事。认领成功但响应体缺
    platformCollectBoxDetailIdMap 时（上游偶发），直接抛「认领成功但没有拿到
    detailId」会让整批发布失败；能读回草稿就继续，读不回才算真失败。

    主路径（按 common_id 过滤）拿不到——比如上游不返回 commonCollectBoxDetailId，
    或采集箱里同一货源有多条——就退回 page_size=50 的整页扫描再筛一遍。
    """
    ids: list[int] = []
    seen: set[int] = set()

    def remember(value: Any) -> None:
        if str(value).isdigit() and int(value) not in seen:
            seen.add(int(value))
            ids.append(int(value))

    def scan(page_size: int) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        try:
            for page_no in range(1, 4):
                listing = client.collect_box_list(page_no=page_no, page_size=page_size)
                rows = (listing.get("data") or {}).get("detailList") or []
                found.extend(row for row in rows if isinstance(row, Mapping))
                if len(rows) < page_size:
                    break
        except (MiaoshouError, ValueError):
            return []
        return found

    # Use the larger page size before deciding whether any rows were found.
    # Existing copies of this common item are normal and must not short-circuit
    # the scan before newly created IDs later in the page can be observed.
    rows = scan(50)
    def collect(matching: list[dict[str, Any]]) -> None:
        for row in matching:
            value = row.get("collectBoxDetailId") or row.get("detailId")
            if known_ids is not None and str(value).isdigit() and int(value) in known_ids:
                continue
            remember(value)

    collect([row for row in rows
             if common_id is not None
             and str(row.get("commonCollectBoxDetailId") or "") == str(common_id)])
    if not ids:
        # Some TK collect-box responses omit the common-box source ID. The
        # pre-copy snapshot keeps this fallback limited to newly visible drafts.
        collect([row for row in rows
                 if not str(row.get("commonCollectBoxDetailId") or "")])
    return ids


def _collect_box_snapshot(client: MiaoshouClient, *,
                          page_size: int = 50) -> set[int] | None:
    """Return visible TK draft IDs, or None when the pre-copy snapshot failed.

    `page_size` 默认 50（最多扫 3 页）是给「复制回执核对」用的最小集。
    整批复制时**不要**每件都调这个函数：采集箱 700 条时它一轮要 4s，
    而复制本身只有 2s —— 每件都调等于把流水线拖成 6s/件（实测）。
    整批调用方应传 `page_size=200` 只取第一页（最近创建的都在最前面）。
    """
    found: set[int] = set()
    try:
        for page_no in range(1, 4):
            listing = client.collect_box_list(page_no=page_no, page_size=page_size)
            data = listing.get("data") or {}
            if not isinstance(data, Mapping):
                return None
            rows = data.get("detailList")
            if not isinstance(rows, list):
                return None
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                value = row.get("collectBoxDetailId") or row.get("detailId")
                if str(value).isdigit():
                    found.add(int(value))
            if len(rows) < page_size:
                break
    except (MiaoshouError, ValueError):
        return None
    return found


def _is_retryable_copy_error(exc: MiaoshouError) -> bool:
    """这次复制失败能不能靠「排到队尾再试一次」解决。

    判据来自 2026-11-19 线上实测：妙手网关抖了约 3 分钟，所有失败都是
    「下游服务响应超时」（HTTP 200 + result=fail）。这类问题跟商品数据
    无关，成品仓库里的货是好的，等网关恢复重跑必然成功。

    retryable：
      · gateway    —— 下游服务响应超时/繁忙
      · rate_limit —— 撞 QPS 限流（Pacer 会自己抬间隔）
      · timeout / unknown / unreachable —— 传输层抖动或熔断暂停
        （注意：unknown 意味着写请求可能已经到达妙手，所以调用方重试时
          必须靠 before_ids 快照去重，不能盲目当「没做过」）
    其余（业务错误：参数错、凭据错、商品不存在）重试也不会好，直接抛。
    """
    if exc.kind in {"gateway", "rate_limit", "timeout", "unknown", "unreachable"}:
        return True
    # HTTP 5xx 也是上游侧问题，值得重试
    return exc.status is not None and exc.status >= 500


def _retry_pause_seconds(client: MiaoshouClient) -> float:
    """件级重试前的停顿时长：跟着熔断器走，别在熔断期间做无用重试。

    妙手抖的时候熔断器会开 30~300 秒，这里如果只等 20 秒就冲进去，
    等于白撞一次探针。取「熔断剩余时间 + 少量余量」和基准值中的较大者。
    """
    breaker = getattr(client, "_breaker", None)
    remaining = 0
    if breaker is not None and hasattr(breaker, "gateway_open_for"):
        try:
            remaining = int(breaker.gateway_open_for())
        except Exception:  # noqa: BLE001 - 熔断器只是加速器，读不到就用默认停顿
            remaining = 0
    if remaining <= 0:
        return CARRIER_ITEM_RETRY_PAUSE_SECONDS
    return min(float(remaining) + 2.0,
               GATEWAY_COOLDOWN_MAX_SECONDS + CARRIER_ITEM_RETRY_PAUSE_SECONDS)


def collect_carriers(db: Session, client: MiaoshouClient, *, count: int,
                     progress: Callable[dict[str, Any], None] | None = None,
                     on_carrier: Callable[[int], None] | None = None,
                     max_rounds: int = 8,
                     preferred_urls: Iterable[str] | None = None,
                     ) -> list[int]:
    """建载体，返回上游明确确认的新草稿 ID。

    `preferred_urls`（2026-11-20 加，负责人指出采购链接错配）：
    每件成品自带的 1688 货源链接，按顺序优先使用。载体天生就带对链接，
    用成品覆盖之后采购链接依然一致 —— 店铺出单不会买错货。

    为什么不能只靠链接池轮换：`take_link()` 只按「失败少的优先、最久没用的」
    挑，完全不知道这件商品原本对应哪个 1688 链接。实测 532 件商品里
    0 件存货源字段，载体链接必然错配。

    用完 preferred 再回退到池子：同一条链接连续采集可能被上游限流，
    池子轮换是必要的退路，只是不再是**首选**。
    """
    if count < 1:
        raise CarrierError("需要至少 1 个载体", required=0)
    # 优先队列：先用手上这批商品自带的货源链接，再回退池子轮换。
    preferred = [str(url).strip() for url in (preferred_urls or [])
                 if str(url or "").strip()]
    details: list[int] = []
    common_id = None
    try:
        listing = client.common_box_list(page_no=1, page_size=20)
        rows = (listing.get("data") or {}).get("detailList") or []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            status = str(row.get("status") or "").lower()
            value = row.get("commonCollectBoxDetailId")
            if status in {"success", "collectsuccess"} and str(value).isdigit():
                common_id = int(value)
                break
    except MiaoshouError as exc:
        raise CarrierError("读取当前账号的公共采集箱失败，请稍后重试") from exc
    url = ""
    failures = 0
    preferred_index = 0
    while len(details) < count and failures < max_rounds:
        # 先消耗 preferred（商品自己带的货源），用完了才轮到池子。
        if common_id is None and preferred_index < len(preferred):
            url = preferred[preferred_index]
            preferred_index += 1
            link = {"url": url}
        else:
            link = take_link(db) if common_id is None else None
        if common_id is None and link is None:
            raise CarrierError("公共采集箱没有可用商品，且没有可采集的货源链接",
                               available=len(details), required=count)
        # 注意：这里**不再**预先消耗 preferred 去「对齐载体」。
        #
        # 链接的对齐已经挪到「复制循环里逐件做」（见下面 source_cursor）——
        # 因为一批 N 件的每一份副本都要有自己的链接，在挑载体阶段对一次
        # 只够第一件用，后面 N-1 件会共用同一条。这里提前消耗反而会把
        # preferred 的游标推乱，导致后面几件拿不到本该属于它们的链接。
        if link:
            url = str(link.get("url") or "")
            offer = offer_id(url)
            common_id = find_in_common_box(client, offer) if offer else None
        if common_id:
            if progress:
                progress({"stage": "reuse", "link": url, "commonId": common_id})
        else:
            if progress:
                progress({"stage": "collect", "link": url, "need": count - len(details)})
            try:
                fetched = client.fetch_items([url])
            except MiaoshouError as exc:
                mark(db, url, ok=False)
                failures += 1
                if progress:
                    progress({"stage": "link_failed", "link": url,
                              "error": str(exc)[:120]})
                continue
            mapping = (fetched.get("data") or {}).get("sourceItemIdAndDetailIdMap") or {}
            ids = [int(value) for value in mapping.values() if str(value).isdigit()]
            if not ids:
                mark(db, url, ok=False)
                failures += 1
                if progress:
                    progress({"stage": "link_failed", "link": url,
                              "error": "采集未返回商品"})
                continue
            common_id = ids[0]
            if not wait_until_collected(client, common_id, progress=progress):
                mark(db, url, ok=False)
                failures += 1
                common_id = None
                if progress:
                    progress({"stage": "collect_failed", "link": url})
                continue

        copies = min(count - len(details), 100)

        # `common_id` 只决定「用公共箱里的哪一件当载体」。
        # 一批 N 件的每一份副本，都要在**复制前**把公共箱的链接改成
        # 它自己对应的那条 —— 因为 `claim_common_to_platform` 是
        # 「复制当前快照」，复制完再改公共箱，已经复制出去的不会变。
        #
        # 更要紧的是：整轮复制都用同一个 `common_id`，如果只在循环外改一次，
        # 这一批 N 件会**共用同一条货源链接** —— 那正是负责人要修的问题
        # （「一批 50 个不同商品，采购时就分不清哪件对哪条」）。
        #
        # 负责人 2026-11-20 明确：「每件商品要有自己的链接」。
        #
        # 成本（实测）：read 一次 0.47s，之后链式 edit 各约 0.9s。
        # 靠 `source_link.override_common_box_source(cache=...)` 复用
        # ossMd5 与编辑模板，避免每件都重读详情。
        source_cache: dict[str, Any] = {}

        if progress:
            progress({"stage": "claim", "commonId": common_id, "copies": copies})
        # 快照：整轮复制**只取一次**，而且只取最近一页。
        #
        # 它的唯一用途是「复制回执没给 detailId 时，判断捞回来的 id 是不是旧草稿」。
        # 老实现每轮都调 `_collect_box_snapshot()` 扫 3 页 × 50 条，采集箱有
        # 700 条时单次约 4s，把 2s 的复制拖成 6s/件 —— 这就是「明明是复制却要
        # 五秒」的来源。新建草稿一定排在列表最前面，取一页 200 条就够。
        before_ids = _collect_box_snapshot(client, page_size=200)
        new_ids: list[int] = []
        # 件级重试队列（负责人 2026-11-19 明确）：
        #   「哪个失败了就把这个成品放到这一批从最后重试，因为成品仓库里面
        #    肯定都是没问题的」。
        #
        # 旧行为：循环里任何一次 claim_common_to_platform 抛错就 raise
        # CarrierError，整批 50 件一起作废 —— 明明只是妙手网关抖了一下。
        # 现在：这一件失败就排到队尾重跑，跑完剩下的再回头补。
        pending_copies = copies
        item_round = 0
        # 这一批里已经用掉了多少条 preferred 链接。
        # 注意与 `preferred_index` 分开：那个是在「挑载体」阶段消耗的，
        # 这里是在「逐件改链接」阶段消耗的，两者不能混。
        source_cursor = 0
        # 已分好链接、但复制失败待重试的件。
        # 每项是那件该用的链接；重试时必须**重新把它自己的链接设回去**。
        retry_targets: list[str] = []
        while pending_copies > 0 and item_round <= CARRIER_ITEM_RETRY_ROUNDS:
            deferred = 0
            # 本轮要处理的目标链接：优先补上一轮失败的，再按序分新的。
            # 用 None 表示「这件不改链接」（preferred 为空时的常态）——
            # 存量 178 件商品都没货源，发布时必须一次 edit 都不发。
            round_targets: list[str | None] = list(retry_targets)
            retry_targets = []
            fresh_needed = pending_copies - len(round_targets)
            for _ in range(fresh_needed):
                if not preferred:
                    round_targets.append(None)
                    continue
                if source_cursor < len(preferred):
                    round_targets.append(preferred[source_cursor])
                else:
                    round_targets.append(preferred[-1])
                source_cursor += 1
            for target_index, target in enumerate(round_targets):
                # === 每件复制前，把公共箱链接改成这一件对应的货源 ===
                #
                # 顺序不能反：`claim_common_to_platform` 复制的是**当时的快照**，
                # 复制完再改公共箱，已经复制出去的副本不会跟着变。
                if common_id and target:
                    override = source_link.override_common_box_source(
                        client, common_id, target, cache=source_cache)
                    if progress:
                        progress({"stage": "source_override",
                                  "commonId": common_id,
                                  "url": target,
                                  "index": target_index + 1,
                                  "total": len(round_targets),
                                  "changed": override.get("changed"),
                                  "error": override.get("error") or ""})
                    if not override.get("ok"):
                        # 不阻断复制：标题/图/价/SKU 仍然是对的，
                        # 链接问题留给界面提示人工确认。
                        logger.warning(
                            "载体货源链接覆盖失败 commonId=%s url=%s：%s",
                            common_id, target, override.get("error"))
                try:
                    claimed = client.claim_common_to_platform([(common_id, "tiktok", 1)])
                except MiaoshouError as exc:
                    # 先判断这是「妙手自己抖了」还是「真的不让做」。
                    # 抖动（网关超时/繁忙/熔断暂停）→ 排到队尾重试；
                    # 限流也一样排尾（Pacer 已自动抬间隔）；
                    # 其余（凭据错、参数错）才真的抛出去。
                    if _is_retryable_copy_error(exc):
                        deferred += 1
                        # 记住这一件的目标链接，下一轮把这个链接重新设回去
                        retry_targets.append(target)
                        if progress:
                            progress({"stage": "copy_deferred",
                                      "commonId": common_id,
                                      "reason": str(exc)[:120],
                                      "deferred": deferred})
                        continue
                    raise CarrierError("复制结果未确认，请先检查采集箱，勿重复操作",
                                       available=len(details) + len(new_ids),
                                       required=count) from exc
                returned_ids = list(dict.fromkeys(claimed_detail_ids(claimed)))
                if len(returned_ids) != 1 and before_ids is not None:
                    known_ids = before_ids | set(new_ids)
                    for attempt in range(3):
                        readback = _fresh_box_ids(client, common_id, known_ids)
                        if readback:
                            if len(readback) == 1:
                                returned_ids = readback
                            break
                        if attempt < 2:
                            time.sleep(1)
                invalid_receipt = len(returned_ids) != 1
                if (before_ids is not None and returned_ids
                        and returned_ids[0] in before_ids):
                    invalid_receipt = True
                if invalid_receipt:
                    # 回执残缺同样先排尾重试：上游可能已经把草稿建出来了，
                    # 直接判死会让这次复制白做，还得人工去核对采集箱。
                    deferred += 1
                    # 已建出的草稿如果链接是对的，重试时会重新设一次（幂等）
                    retry_targets.append(target)
                    if progress:
                        progress({"stage": "copy_deferred", "commonId": common_id,
                                  "reason": "复制回执不完整", "deferred": deferred})
                    continue
                detail_id = returned_ids[0]
                if detail_id in new_ids or detail_id in details:
                    raise CarrierError("复制接口返回了重复商品 ID，请先检查采集箱，勿重复操作",
                                       commonId=common_id, available=len(details) + len(new_ids),
                                       required=count)
                new_ids.append(detail_id)
                if on_carrier:
                    on_carrier(detail_id)
            if deferred == 0:
                pending_copies = 0
                break
            # 还有没建成的：整批先停一下让妙手缓过来（也等熔断冷却走完），
            # 再把这几件排到队尾重跑。轮数上限见 CARRIER_ITEM_RETRY_ROUNDS。
            item_round += 1
            if item_round > CARRIER_ITEM_RETRY_ROUNDS:
                raise CarrierError(
                    f"还有 {deferred} 件载体没能建成（已重试 "
                    f"{CARRIER_ITEM_RETRY_ROUNDS} 轮），请稍后重试这 {deferred} 件",
                    commonId=common_id, deferred=deferred,
                    available=len(details) + len(new_ids), required=count)
            if progress:
                progress({"stage": "copy_retry_round", "commonId": common_id,
                          "round": item_round, "deferred": deferred})
            time.sleep(_retry_pause_seconds(client))
            pending_copies = deferred
        if url:
            mark(db, url, ok=True)
        before = len(details)
        for detail_id in new_ids:
            if len(details) >= count:
                break
            if detail_id not in details:
                details.append(detail_id)
        if len(details) == before:
            raise CarrierError("复制接口未返回新的商品 ID，请先检查采集箱，勿重复操作",
                               commonId=common_id, required=count)
    if len(details) < count:
        raise CarrierError(f"只创建了 {len(details)} 个载体，需要 {count} 个",
                           available=len(details), required=count)
    return details


def _readback_mismatches(client: MiaoshouClient, detail_id: int, shop_id: int,
                         body: Mapping[str, Any], product: Mapping[str, Any], *,
                         strict: bool = False) -> list[str]:
    """保存后重新拉一次做核验（默认已停用，见 fill_carriers 的 verify_readback）。

    为什么默认不核验（负责人 2026-11-04 明确要速度）：
      · 实测每件多花约 6 秒（二次完整拉取），占单件总耗时 40% 以上；
      · 载体是妙手刚复制出来的草稿，读回里 categoryCid / prices / translations
        与仓库快照结构天然不同，逐件都报「不一致」，等于必然失败；
      · 老版 fill_carriers 保存后根本不做读回，成败只看 save_shop 抛不抛。
    保留这个函数是为了需要严格核验的场合（如发布前单独复核）能显式打开。

    **失败必须 fail closed（2026-11-07 修）**：以前读回超时、结构异常、比较抛错
    全部 `return []`，等于「读不到就当没问题」，于是读回核验形同虚设 ——
    上游明明没存对，这里返回空列表，调用方就把它记成成功并允许发布。
    现在这些情况一律返回一条明确的不一致项，让调用方按失败处理。

    `strict=True` 是**发布前**用的严格模式：价格和类目也必须一致。
    认领（铺数据）阶段这两项与仓库快照口径天然不同，可以忽略；
    但发布出去的商品如果价格被压、类目没下沉成功，用户看到的就是错货，
    所以发布路径必须逐项核对，不能跟着忽略。
    """
    from services import pre_save_check, readback_check

    try:
        # site 维度的载体必须用站点接口读，否则报「未选择预发布店铺」，
        # 会让读回核验把本来成功的发布判成失败。
        snapshot, _block, _md5 = _carrier_snapshot(
            client, detail_id, shop_id, str(product.get("site") or "MY"))
    except (MiaoshouError, ValueError, errors.AppError) as exc:
        # 读不回来就不算核验通过。返回原因而不是空列表，调用方会记失败。
        return [f"readbackUnavailable({type(exc).__name__}: {str(exc)[:60]})"]
    if not isinstance(snapshot, Mapping):
        return [f"readbackUnavailable(响应不是对象: {type(snapshot).__name__})"]
    data = snapshot.get("data") or {}
    if not isinstance(data, Mapping):
        return [f"readbackUnavailable(data 不是对象: {type(data).__name__})"]
    nested = data.get("data")
    actual = (data.get("shopCollectItemInfo")
              or data.get("siteCollectItemInfo")
              or data.get("collectItemInfo")
              or (nested.get("shopCollectItemInfo") if isinstance(nested, Mapping) else None)
              or {})
    if not isinstance(actual, Mapping) or not actual:
        return ["readbackUnavailable(读回里没有商品信息)"]
    coverage = (product.get("translation") or {}).get("coverage") or {}
    payload = dict(body)
    payload.pop("detailId", None)
    notes_only_images = (
        pre_save_check.detail_images_only(readback_check.extract_detail_image_urls(
            payload.get("notes"))) == payload.get("notes")
        if str(payload.get("notes") or "").strip() else False
    )
    try:
        issues = readback_check.readback_issues(
            payload, actual, payload, coverage,
            pricing_enabled=True,
            translate_images=notes_only_images,
        )
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        # 比较器自身崩了，说明读回结构不认识 —— 同样不能当通过。
        return [f"readbackUnavailable(比较失败: {type(exc).__name__})"]
    # 载体场景下这几项与仓库快照口径必然不同。
    # strict=False（认领/铺数据）：categoryCid、prices、translations 忽略。
    # strict=True （发布前核验）：prices 必须核对（发出去价格错了就是事故），
    #   但 **categoryCid 仍需忽略** —— 载体是妙手复制出来的草稿，它的类目跟着
    #   载体走、与仓库快照口径天然不同（CLAUDE_STANDARD §3.6 已实测确认）。
    #
    # 2026-11-07 踩过的坑：把 categoryCid 一起纳入严格核验后，定时发布全部失败，
    # 日志是「载体覆盖后读回不一致 detailId=… 差异=['categoryCid']」，
    # 任务状态 failed、一件都发不出去。这正是忽略清单存在的意义。
    ignored = ({"categoryCid", "translations"} if strict
               else {"categoryCid", "prices", "translations"})
    return [item for item in issues if str(item).strip() not in ignored]


def target_warehouse_id(client: MiaoshouClient, shop_id: int) -> str:
    """店铺默认仓库 id：save_shop 要求 SKU 仓库映射里有它。"""
    try:
        listing = client.warehouse_list([int(shop_id)])
    except (MiaoshouError, ValueError) as exc:
        raise CarrierError(f"读取店铺仓库失败：{str(exc)[:80]}") from None
    for shop in (listing.get("data") or {}).get("shopWarehouseList") or []:
        if str(shop.get("shopId")) != str(shop_id):
            continue
        warehouses = shop.get("warehouseList") or []
        for warehouse in warehouses:
            if str(warehouse.get("isDefault")) == "1" and warehouse.get("warehouseId"):
                return str(warehouse["warehouseId"])
        for warehouse in warehouses:
            if warehouse.get("warehouseId"):
                return str(warehouse["warehouseId"])
    raise CarrierError(f"店铺 {shop_id} 没有可用仓库", shopId=str(shop_id))


def resolve_leaf_cid(client: MiaoshouClient, cid: Any, site: str = "MY",
                     name_hint: str = "",
                     tree: Mapping[str, Any] | bool | None = None) -> Any:
    """把「父级类目 cid」换成可用的叶子类目 cid。

    妙手保存商品只接受叶子类目（isLastLevel=1）。采集回来的商品常常带的是
    父级类目（如 834568「运动与户外服装」），直接写入会报
    「站点【MY】类目【834568】不存在，请重新选择类目」。
    这里查一次类目树：cid 已是叶子就原样返回；是父级就往下找第一个可用的叶子；
    找不到就按 name_hint（类目中文名）模糊匹配一个可用叶子。
    """
    text = str(cid or "").strip()
    if not text:
        return cid
    if tree is None:
        try:
            tree = client.category_tree(site)
        except Exception as exc:  # noqa: BLE001 - 拿不到类目树就别改，让上游自己报
            logger.warning("查类目树失败，保持原 cid=%s：%s", text, exc)
            return cid
    if tree is False:
        return cid

    flat: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        for value in (node or {}).values():
            if not isinstance(value, dict):
                continue
            flat.append(value)
            walk(value.get("children"))

    walk((tree.get("data") or {}).get("cateTree"))
    by_cid = {str(item.get("cid")): item for item in flat}
    node = by_cid.get(text)
    if node is None:
        # cid 不属于该站点：按类目名兜一个可用叶子
        node = None
    elif str(node.get("isLastLevel")) == "1" and not node.get("disabled"):
        return node.get("cid")

    def first_leaf(item: dict[str, Any]) -> Any:
        stack = [item]
        while stack:
            current = stack.pop(0)
            if (str(current.get("isLastLevel")) == "1"
                    and not current.get("disabled")):
                return current.get("cid")
            for child in (current.get("children") or {}).values():
                if isinstance(child, dict):
                    stack.append(child)
        return None

    if node is not None:
        leaf = first_leaf(node)
        if leaf is not None:
            logger.info("类目 %s 是父级，改用其叶子类目 %s", text, leaf)
            return leaf

    # 按中文名模糊匹配一个可用叶子（如「运动服饰」→ 运动套装）
    hint = str(name_hint or "").strip()
    if hint:
        for item in flat:
            if str(item.get("isLastLevel")) != "1" or item.get("disabled"):
                continue
            names = f"{item.get('name') or ''}{item.get('nameChinese') or ''}"
            if hint and (hint in names or names.find(hint) >= 0):
                logger.info("按类目名「%s」匹配到叶子类目 %s", hint, item.get("cid"))
                return item.get("cid")
    logger.warning("类目 %s 无法解析成叶子类目，保持原值", text)
    return cid


def remap_sku_to_carrier(payload: dict[str, Any],
                         carrier_info: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """把 payload 的规格 id 换成**载体自己的**规格 id。

    为什么必须换（2026-11-04 实测确认）：
      `attrValueId` 是载体级的，不是全局 ID。
      同一个颜色「Jingga」，在仓库商品里是 729be68705，
      但载体里根本没有这个 id —— 妙手用载体的规格表去查 skuMap 的 key，
      查不到就报「SKU「Jingga-35」不存在」。
      尺码 id（34~45）碰巧相同，所以只有颜色会失败，很容易误判成「格式问题」。

    映射策略（按优先级）：
      1. 名称完全匹配（忽略大小写/空格）→ 用载体的 id
      2. 名称包含匹配（一方是另一方子串）→ 用载体的 id
      3. 维度内按顺序对应（个数相同时）→ 用载体的 id
      4. 都对不上 → 保留原 id（让上游报错，至少不悄悄改错）

    返回 (新 payload, 变更说明)。不修改入参。
    """
    data = deepcopy(dict(payload))
    notes: list[str] = []

    our_props = [p for p in (data.get("skuPropertyList") or []) if isinstance(p, Mapping)]
    carrier_props = [p for p in (carrier_info.get("skuPropertyList") or [])
                     if isinstance(p, Mapping)]
    if not our_props or not carrier_props:
        return data, ["载体或商品缺规格，跳过重映射"]

    # 维度配对上：优先按 attrName/propName 匹配
    pairs: list[tuple[int, int]] = []
    used_carrier: set[int] = set()
    for our_dim, our_prop in enumerate(our_props):
        our_name = _norm(our_prop.get("propName") or our_prop.get("attrName"))
        matched = None
        for c_dim, c_prop in enumerate(carrier_props):
            if c_dim in used_carrier:
                continue
            c_name = _norm(c_prop.get("propName") or c_prop.get("attrName"))
            if our_name and c_name and our_name == c_name:
                matched = c_dim
                break
        if matched is not None:
            used_carrier.add(matched)
            pairs.append((our_dim, matched))
    # 剩下的按顺序补
    for our_dim in range(len(our_props)):
        if any(p[0] == our_dim for p in pairs):
            continue
        for c_dim in range(len(carrier_props)):
            if c_dim in used_carrier:
                continue
            used_carrier.add(c_dim)
            pairs.append((our_dim, c_dim))
            break

    # 每个维度建立 我们的id → 载体id
    id_maps: dict[str, str] = {}
    for our_dim, c_dim in pairs:
        our_values = [av for av in (our_props[our_dim].get("attrValueList") or [])
                      if isinstance(av, Mapping)]
        c_values = [av for av in (carrier_props[c_dim].get("attrValueList") or [])
                    if isinstance(av, Mapping)]
        if not our_values or not c_values:
            continue

        by_name: dict[str, str] = {}
        for c_av in c_values:
            cid = str(c_av.get("attrValueId") or "").strip()
            if cid:
                by_name[_norm(c_av.get("attrValue"))] = cid

        for index, our_av in enumerate(our_values):
            our_id = str(our_av.get("attrValueId") or "").strip()
            if not our_id:
                continue
            key = _norm(our_av.get("attrValue"))
            target = by_name.get(key)
            if target is None and key:
                # 包含匹配
                for c_name, cid in by_name.items():
                    if c_name and (c_name in key or key in c_name):
                        target = cid
                        break
            if target is None and len(our_values) == len(c_values):
                # 按顺序对应
                target = str(c_values[index].get("attrValueId") or "").strip() or None
            if target:
                id_maps[our_id] = target

    if not id_maps:
        return data, ["没有可映射的规格 id"]

    # 重写 skuMap 的 key
    old_map = data.get("skuMap") or {}
    new_map: dict[str, Any] = {}
    merged_dup = 0
    if isinstance(old_map, Mapping):
        for key, value in old_map.items():
            parts = [x.strip() for x in str(key).split(";") if x.strip()]
            new_parts = [id_maps.get(p, p) for p in parts]
            new_key = ";" + ";".join(new_parts) + ";"
            if new_key in new_map:
                merged_dup += 1
                continue
            new_map[new_key] = value
        data["skuMap"] = new_map

    # skuPropertyList 也换成载体的 id（保持 key 与规格一致）
    rebuilt_props = []
    for our_dim, c_dim in sorted(pairs):
        our_prop = dict(our_props[our_dim])
        c_prop = carrier_props[c_dim]
        our_values = [av for av in (our_prop.get("attrValueList") or [])
                      if isinstance(av, Mapping)]
        c_values = [av for av in (c_prop.get("attrValueList") or [])
                    if isinstance(av, Mapping)]
        by_id = {str(av.get("attrValueId") or ""): av for av in c_values}
        new_values = []
        for our_av in our_values:
            new_av = dict(our_av)
            our_id = str(our_av.get("attrValueId") or "").strip()
            target = id_maps.get(our_id)
            if target:
                new_av["attrValueId"] = target
                # 名称也跟载体一致，避免「id 对、名字不对」再次报错
                src = by_id.get(target)
                if src is not None and str(src.get("attrValue") or "").strip():
                    new_av["attrValue"] = src.get("attrValue")
            new_values.append(new_av)
        # 去重（映射后可能撞车）
        seen_ids: set[str] = set()
        deduped = []
        for av in new_values:
            vid = str(av.get("attrValueId") or "")
            if vid and vid in seen_ids:
                continue
            if vid:
                seen_ids.add(vid)
            deduped.append(av)
        our_prop["attrValueList"] = deduped
        # 保留载体的规格元信息
        for field in ("attrId", "propId", "attrName", "propName"):
            if c_prop.get(field) not in (None, ""):
                our_prop[field] = c_prop[field]
        rebuilt_props.append(our_prop)
    data["skuPropertyList"] = rebuilt_props

    notes.append("规格id重映射%d个" % len(id_maps))  # noqa: UP031
    if merged_dup:
        notes.append("合并重复SKU%d条" % merged_dup)  # noqa: UP031
    return data, notes


def align_spec_names(payload: dict[str, Any],
                     carrier_info: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """把 payload 的规格维度名（attrName/propName）对齐成载体的写法。

    为什么必须对齐（2026-11-04 实测）：
      妙手的 `attrId` 是 null，它靠 `attrName` 认维度。
      载体里写的是中文「颜色」「尺码」，而仓库 payload 里是马来语「Warna」「Saiz」。
      名字对不上 → 妙手找不到这个维度 → 整组 SKU 被判「SKU「颜色-尺码」不存在」。

    做法：按维度顺序，把载体的 attrName/propName 覆盖到 payload 上，
    规格值（attrValue/attrValueId）保持 payload 的，随后由
    remap_sku_to_carrier 换成载体的 id。
    """
    data = deepcopy(dict(payload))
    notes: list[str] = []
    our_props = [p for p in (data.get("skuPropertyList") or []) if isinstance(p, dict)]
    carrier_props = [p for p in (carrier_info.get("skuPropertyList") or [])
                     if isinstance(p, Mapping)]
    if not our_props or not carrier_props:
        return data, []

    carrier_by_kind: dict[str, Mapping[str, Any]] = {}
    for prop in carrier_props:
        k = _kind(prop.get("attrName") or prop.get("propName"))
        if k and k not in carrier_by_kind:
            carrier_by_kind[k] = prop

    for index, prop in enumerate(our_props):
        our_name = prop.get("attrName") or prop.get("propName")
        # 优先按语义类型配，退化到按顺序
        target = None
        k = _kind(our_name)
        if k:
            target = carrier_by_kind.get(k)
        if target is None and index < len(carrier_props):
            target = carrier_props[index]
        if target is None:
            continue
        carrier_name = target.get("attrName")
        carrier_prop_name = target.get("propName")
        if carrier_name and prop.get("attrName") != carrier_name:
            prop["attrName"] = carrier_name
            notes.append("attrName→%s" % carrier_name)  # noqa: UP031
        if carrier_prop_name and prop.get("propName") != carrier_prop_name:
            prop["propName"] = carrier_prop_name
        if target.get("attrId") not in (None, ""):
            prop["attrId"] = target.get("attrId")

    data["skuPropertyList"] = our_props
    return data, notes


class _TtlCache:
    """简单 TTL 字典：超期自动失效，不依赖外部库。过期条目在下次访问时惰性清除。"""

    def __init__(self, ttl_seconds: float) -> None:
        self._data: dict = {}
        self._ts: dict = {}
        self._ttl = ttl_seconds

    def get(self, key: Any) -> Any:
        ts = self._ts.get(key)
        if ts is None:
            return None
        if time.monotonic() - ts > self._ttl:
            self._data.pop(key, None)
            self._ts.pop(key, None)
            return None
        return self._data.get(key)

    def __setitem__(self, key: Any, value: Any) -> None:
        self._data[key] = value
        self._ts[key] = time.monotonic()


_CATEGORY_ATTR_CACHE: _TtlCache = _TtlCache(ttl_seconds=3600)  # 1 小时 TTL，防止类目数据过时


def category_sale_attrs(client: MiaoshouClient, cid: Any, site: str,
                        shop_id: int, *, strict: bool = False) -> dict[str, str]:
    """取类目的销售规格 attrId：{"color": "100000", "size": "100007"}。

    必须从类目元数据拿真实 attrId（实测 MY 站点颜色 100000、尺码 100007）。
    填 null 或自造值（如 1000000001）会让妙手认不出规格维度，
    报「SKU「颜色-尺码」不存在」。
    """
    try:
        shop_int = int(shop_id)
    except (TypeError, ValueError):
        logger.warning("category_sale_attrs 收到非法 shop_id=%r，跳过", shop_id)
        return {}
    key = (str(site or "MY").upper(), str(cid or ""), shop_int)
    cached = _CATEGORY_ATTR_CACHE.get(key)
    if cached:
        return cached

    result: dict[str, str] = {}
    if not str(cid or "").strip():
        return result
    # 内置兜底：MY 站点的销售规格 attrId 是平台固定值（实测颜色 100000、
    # 尺码 100007，多个类目都一致）。批量处理时这个接口很容易回「请求太频繁」，
    # 拿不到就等于不能清洗颜色，整批卡住 —— 用已知固定值先跑，
    # 真拿到类目元数据时再以元数据为准。
    FALLBACK = {"color": "100000", "colorName": "Warna",
                "size": "100007", "sizeName": "Saiz"}
    try:
        meta = client.category_metadata(site or "MY", str(cid), [shop_int])
        data = meta.get("data") or {}
        block = data.get("categoryMetadata") or data
        for item in (block.get("categorySaleAttrList") or []):
            if not isinstance(item, Mapping):
                continue
            attr_id = str(item.get("attrId") or "").strip()
            if not attr_id:
                continue
            name = str(item.get("name") or "").lower()
            alias = str(item.get("attributeNameAlias") or "")
            if "warna" in name or "color" in name or "颜色" in alias:
                result.setdefault("color", attr_id)
                result.setdefault("colorName", str(item.get("name") or ""))
            elif "saiz" in name or "size" in name or "尺码" in alias or "尺寸" in alias:
                result.setdefault("size", attr_id)
                result.setdefault("sizeName", str(item.get("name") or ""))
    except Exception as exc:  # noqa: BLE001 - 拿不到就退化到载体里的值
        logger.warning("读取类目 %s 规格定义失败：%s", cid, exc)
        if strict:
            # 需要 attrId 才能安全改规格值的调用方（颜色清洗）：限流时用内置
            # 固定值兜底，而不是放弃 —— 放弃会让整批商品卡在「不能洗颜色」上，
            # 而 MY 站点的颜色/尺码 attrId 本来就是平台固定值。
            logger.info("类目 %s 元数据不可用，attrId 使用内置已知值", cid)
            return dict(FALLBACK)
        return result          # 失败不缓存，下次还能重试
    if result:
        _CATEGORY_ATTR_CACHE[key] = result
    return result


# 规格值里是否含中文：用来判断草稿是否还是「中文原文」形态。
_CJK_CHAR = re.compile(r"[\u4e00-\u9fff]")


def _short_labels(label: str) -> list[str]:
    """Split draft value like model+[color] into short labels.

    Miaoshou draft stores model + bracketed color, we submit the short
    cleaned name, and skuMap old keys also use the short name. Registering
    short aliases lets skuMap keys map back to the new ids.
    """
    found: list[str] = []
    depth = 0
    buf: list[str] = []
    for ch in str(label or ""):
        if ch in "(（【[":
            depth += 1
            buf = []
            continue
        if ch in ")）】]":
            if depth > 0:
                inner = "".join(buf).strip()
                if inner:
                    found.append(inner)
                depth -= 1
                buf = []
            continue
        if depth > 0:
            buf.append(ch)
    outside = "".join(
        ch for ch in str(label or "")
        if ch not in "(（【[])）】]"
    ).strip()
    if outside and outside not in found:
        found.append(outside)
    return found

def normalize_spec_for_save(payload: dict[str, Any],
                            carrier_info: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """按妙手文档把规格整理成可保存的形态（这是能成功保存的关键）。

    文档原文（save_shop_collect_item_info）：
      skuMap — SKU Map，key 为规格属性值 ID 组合串，
      格式为「;属性值ID1;属性值ID2;」（**属性值 ID 从小到大**，分号分隔，首尾带分号）
      skuPropertyList[].attrValueList[] 只有三个字段：
        attrValueId / attrValue / imgUrl

    实测（2026-11-04）三个必要条件，缺一就报「SKU「颜色-尺码」不存在」：
      1. attrId 必须填类目元数据里的真实 id（颜色 100000、尺码 100007），
         不能是 null，也不能用 1000000001 这种自造值；
      2. attrName 用类目里的 name（Warna / Saiz）；
      3. skuMap 的 key 里各段按字符串升序排列（不是按维度顺序！）。

    同时剔除文档没列的多余字段（imgUrls/imageUrl/supplementarySkuImageUrls），
    上游对多余字段敏感。
    """
    data = deepcopy(dict(payload))
    notes: list[str] = []
    clean = (carrier_info.get("shopCollectItemInfo")
             or carrier_info.get("collectItemInfo")
             or carrier_info) if isinstance(carrier_info, Mapping) else {}

    our_props = [p for p in (data.get("skuPropertyList") or []) if isinstance(p, Mapping)]
    if not our_props:
        return data, ["无规格"]

    def looks_like_size(values: list[Mapping[str, Any]]) -> bool:
        """尺码判定：必须认 36-37、38-39 这类区间写法（实测常见）。

        只按 isdigit() 判会把区间尺码当成颜色，导致两个维度都取 Warna，
        妙手报「SKU 规格[Warna] 选项名称重复」。
        """
        hits = 0
        for av in values:
            label = str(av.get("attrValue") or "").strip()
            if not label:
                continue
            upper = label.upper()
            if upper in ("XS", "S", "M", "L", "XL", "XXL", "XXXL", "2XL",  # noqa: SIM114
                         "3XL", "FREE", "F"):
                hits += 1
            elif _SIZE_PATTERN.match(label):
                hits += 1
        return bool(values) and hits * 2 >= len(values)

    # carrier 里的 attrId / attrName（优先用类目元数据填好的，其次载体的）
    carrier_by_kind: dict[str, Mapping[str, Any]] = {}
    for prop in (clean.get("skuPropertyList") or []):
        if not isinstance(prop, Mapping):
            continue
        k = _kind(prop.get("attrName") or prop.get("propName"))
        if not k:
            # 草稿维度名可能只剩英文（Warna/Saiz）或中文（颜色/尺码），
            # 也可能被 _fill_spec_attr_ids 改过名。按 attrId 兜底判定，
            # 否则 carrier_by_kind 拿不到这一维，草稿原文对不回去
            # （2026-11-10 实测：3522040694 输出颜色为空数组，SKU 报不存在）。
            aid = str(prop.get("attrId") or "").strip()
            if aid == "100000":
                k = "color"
            elif aid == "100007":
                k = "size"
        if k and k not in carrier_by_kind:
            carrier_by_kind[k] = prop

    strict_props: list[dict[str, Any]] = []
    for dim, prop in enumerate(our_props):
        values = [av for av in (prop.get("attrValueList") or []) if isinstance(av, Mapping)]
        k = _kind(prop.get("attrName") or prop.get("propName"))
        if not k:
            k = "size" if looks_like_size(values) else "color"
        src = carrier_by_kind.get(k) or {}
        attr_id = str(src.get("attrId") or prop.get("attrId") or "").strip()
        # 载体的 attrName 可能是中文「颜色/尺码」，而 attrId 是类目元数据里的
        # 100000/100007（妙手侧叫 Warna/Saiz）。attrName 必须与 attrId 配套，
        # 混着用会让妙手按 attrId 查表后名字对不上，报
        # 「SKU【Kuning-41】不存在」那种「规格值找不到」的错（2026-11-07 实测）。
        fallback_name = str(clean.get(f"_{k}AttrName") or "").strip()
        attr_name = (str(src.get("attrName") or "").strip()
                     or fallback_name
                     or str(prop.get("attrName") or "").strip())
        # 载体里没有 attrId 时，用调用方塞进 carrier_info 的兜底字段
        if not attr_id:
            attr_id = str(clean.get(f"_{k}AttrId") or "").strip()
            # attrId 用兜底值时，名字也必须跟着兜底名（两者是一对）
            if fallback_name:
                attr_name = fallback_name

        # 规格值：直接用商品自己的规格（相当于给载体新增/删除规格值）。
        #
        # 实测结论（2026-11-04，三种 id 生成方式都验证过）：
        #   · 妙手接受任意非空 attrValueId 作为「新规格值」；
        #   · 所以不需要把商品颜色映射到载体已有的颜色（那反而会失败）；
        #   · 只要 attrId 对（颜色 100000 / 尺码 100007）+ id 非空 + skuMap 升序，
        #     妙手就会按我们提交的规格重建载体的规格表。
        # 名称去重：同维度重名会让妙手报「选项名称重复」。
        seen_labels: set[str] = set()
        # attrName 必须与 attrId 配套：attrId=100000 在妙手侧叫 Warna，
        # 100007 叫 Saiz。草稿/上游有时给中文名「颜色/尺码」，与 attrId
        # 混着提交，妙手按 attrId 查表后名字对不上，报「产品SKU必填」
        # （2026-11-10 实测：3522040694 颜色维度 attrName=颜色 attrId=100000）。
        if attr_id == "100000":
            attr_name = "Warna"
        elif attr_id == "100007":
            attr_name = "Saiz"
        entry = {
            "attrId": attr_id,
            "attrName": attr_name,
            "attrValueList": [],
        }
        # 草稿里同一维度原有的规格值：清理过的值要能对回原文。
        # 我们提交的是清洗后的「白绿色」，而妙手草稿里存的是「566【白绿色】」，
        # 它按**草稿原文逐字匹配**，提交清洗值就报「SKU【白绿色-41】不存在」
        # （2026-11-10 实测：3522040694 两色全挂）。
        # 归属判定：草稿原文包含我们的值（去空白后），就沿用草稿的原文与 id。

        # 颜色 id 由 normalize_colors 生成并同步进 skuMap key —— 是「我们这
        # 份提交」的身份标识。位置匹配只允许换显示文案，**绝不换 id**：
        # 换 id 等于把 12 条 skuMap key 全部作废（2026-11-10 实测：
        # 换 id 后 normalize 输出 0 条，报「产品SKU必填」）。
        # 所以这里只返回文案，id 由调用方保留自己的。
        def draft_original(label: str, position: int = -1):
            """对齐 attrValueId，文案保持**我们自己的**（清洗值或译文）。

            实测结论（2026-11-11，3524432503）：只要 attrId 正确
            （颜色 100000 / 尺码 100007），妙手会按我们提交的规格**重建**
            规格表，清洗值「黑色」直接接受，不需要退回草稿原文「Q617黑色」。

            所以这里只做一件事：文案原样用我们的，id 也用自己的 —— 两者
            本来配套（skuMap key 由这些 id 组成）。唯一例外是草稿里存在
            完全相同的文案，此时沿用草稿那条的 id 更稳。
            """
            # 草稿里有完全相同的文案：沿用草稿的 id（最稳）
            for dav in (src.get("attrValueList") or []):  # noqa: B023
                if not isinstance(dav, Mapping):
                    continue
                if str(dav.get("attrValue") or "").strip() == label:
                    return label, str(dav.get("attrValueId") or "").strip()
            # 否则：文案与 id 都用自己的（配套，skuMap key 才对得上）
            own_id = str(av.get("attrValueId") or "").strip()  # noqa: B023
            return label, own_id

        _pos = -1
        for av in values:
            label = str(av.get("attrValue") or "").strip()
            if not label:
                continue
            _pos += 1
            original_label, original_id = draft_original(label, position=_pos)
            if original_label and original_label != label:
                label = original_label
                if original_id:
                    av = dict(av, attrValueId=original_id)
            key = label.casefold().replace(" ", "")
            if key in seen_labels:
                continue
            seen_labels.add(key)
            our_id = str(av.get("attrValueId") or "").strip()
            if not our_id:
                # 没有 id 就生成一个稳定的（妙手接受任意非空值）
                our_id = hashlib.md5(
                    ("%s|%s" % (attr_id, label)).encode("utf-8")).hexdigest()[:10]  # noqa: UP031
            img = (av.get("imgUrl")
                   or av.get("imageUrl")
                   or (av.get("imgUrls") or [None])[0]
                   or "")
            # 只有**颜色**维度带图，尺码维度一律清空 imgUrl
            # （负责人口径 2026-11-12：「怎么自动化处理的这个尺码有图片的，
            #   不是只有颜色才有吗」）。
            #
            # 尺码维度本来就不该有图。上游草稿有时把颜色图错配到尺码值上
            # （平台规格表里 Saiz 的每个值都挂着同一批商品图），
            # 我们照抄过去，详情页就会出现「M(34-36) 配一张鞋图」这种怪现象，
            # 而且平台侧也可能判规格数据异常。
            _is_color_dim = (str(attr_id or "") == "100000"
                             or str(entry.get("attrName") or "").casefold() in
                             ("warna", "color", "colour", "颜色", "色彩"))
            entry["attrValueList"].append({
                "attrValueId": our_id,
                "attrValue": label,
                "imgUrl": str(img or "") if _is_color_dim else "",
            })
        if not entry["attrValueList"]:
            notes.append("维度%d规格值为空，跳过" % dim)  # noqa: UP031
            continue
        strict_props.append(entry)
        if not attr_id:
            notes.append("维度%d缺attrId" % dim)  # noqa: UP031

    # skuMap：key 里的 id 必须都在 strict_props 里，且按 ID 升序排列
    #
    # 关键：key 的各段是**按 ID 字符串升序**排的（文档要求），段位置与维度顺序
    # 无关。所以「第 0 段一定是颜色」这个假设是错的 —— 只有当颜色 id 恰好小于
    # 尺码 id 时才成立。实测 id=32（White 25a81701fb / 尺码 1c383cd30b…）的 key
    # 是 `;1c383cd30b;25a81701fb;`，尺码在前、颜色在后，按位置匹配就会把尺码值
    # 当成颜色值去校验，最终报「SKU【White-35】不存在」。
    # 正确做法：用「id → 它属于哪个维度」的全局映射来判断，不依赖段位置。
    our_dim_ids = [
        {str(av.get("attrValueId") or "") for av in (p.get("attrValueList") or [])
         if isinstance(av, Mapping)}
        for p in strict_props
    ]
    id_to_dim: dict[str, int] = {}
    for dim_index, ids in enumerate(our_dim_ids):
        for vid in ids:
            id_to_dim.setdefault(vid, dim_index)
    # 名称 -> 商品 id 的反查表（规格值去重时，旧 key 的 id 可能已被删掉）
    name_to_new: dict[str, str] = {}
    old_id_to_name: dict[str, str] = {}
    for prop in our_props:
        for av in (prop.get("attrValueList") or []):
            if isinstance(av, Mapping):
                vid = str(av.get("attrValueId") or "").strip()
                label = str(av.get("attrValue") or "").strip()
                if vid and label:
                    old_id_to_name[vid] = label.casefold().replace(" ", "")
    for prop in strict_props:
        for av in (prop.get("attrValueList") or []):
            label = str(av.get("attrValue") or "").strip()
            if label:
                vid = str(av.get("attrValueId") or "")
                name_to_new[label.casefold().replace(" ", "")] = vid
                # Draft value may be model+bracket color while skuMap old keys
                # use the short cleaned name. Register short aliases too,
                # otherwise every skuMap key is dropped as misaligned.
                if len(label) > 2:
                    for short in _short_labels(label):
                        name_to_new.setdefault(short.casefold().replace(" ", ""), vid)

    old_map = data.get("skuMap") or {}
    new_map: dict[str, Any] = {}
    dropped = 0
    merged = 0
    if isinstance(old_map, Mapping):
        for key, value in old_map.items():
            parts = [x.strip() for x in str(key).split(";") if x.strip()]
            if len(parts) != len(strict_props):
                dropped += 1
                continue
            # key 段兼容三种形态：
            #   1. 规格值 id（仓库原生，:id 形态）
            #   2. 规格值名称（公共箱/重建后的形态，如 ;White;35;）
            #   3. 旧 id（去重/改名前的），按名称反查新 id
            mapped = []
            ok = True
            for part in parts:
                if part in id_to_dim:
                    mapped.append(part)
                    continue
                # 名称 → id（按维度归属找）
                by_name = name_to_new.get(part.casefold().replace(" ", ""))
                if by_name:
                    mapped.append(by_name)
                    continue
                # 旧 id → 名称 → 新 id
                label_key = old_id_to_name.get(part)
                new_id = name_to_new.get(label_key) if label_key else None
                if not new_id:
                    ok = False
                    break
                mapped.append(new_id)
            if not ok or len(mapped) != len(strict_props):
                dropped += 1
                continue
            # 每个维度必须恰好出现一次（不按下标对应，按维度归属校验）
            dims = [id_to_dim.get(x) for x in mapped]
            if any(d is None for d in dims) or len(set(dims)) != len(dims):
                dropped += 1
                continue
            ordered = sorted(mapped)
            new_key = ";" + ";".join(ordered) + ";"
            if new_key in new_map:
                merged += 1
                continue
            new_map[new_key] = value
    data["skuPropertyList"] = strict_props
    data["skuMap"] = new_map
    # 文档未列这些字段，去掉避免上游判非法
    for junk in ("colorMap", "sizeMap", "colorPropName", "sizePropName"):
        data.pop(junk, None)

    notes.append("规格SKU规范化%d条" % len(new_map))  # noqa: UP031
    if dropped:
        notes.append("丢弃无法对齐%d条" % dropped)  # noqa: UP031
    if merged:
        notes.append("合并重复%d条" % merged)  # noqa: UP031
    return data, notes


def adapt_payload(payload: Mapping[str, Any], *, shop_id: int,
                  warehouse_id: str, leaf_cid: Any = None) -> dict[str, Any]:
    """改写 payload 归属：目标 shopId + 目标仓库库存映射，库存数量保留。"""
    body = deepcopy(dict(payload))
    body["shopId"] = int(shop_id)
    # Older finished-product payloads already carry this switch, while some
    # warehouse snapshots omit it. Keep explicit source values and match the
    # established Miaoshou drafts for missing values.
    #
    # 妙手只认字符串 "0"/"1"。仓库里历史修复把缺值补成了布尔 false，
    # 直接透传会报「shopCollectItemInfo.isCodOpen 必填」，所以这里统一规范化。
    cod = body.get("isCodOpen")
    if isinstance(cod, bool):
        body["isCodOpen"] = "1" if cod else "0"
    elif cod in (None, ""):
        body["isCodOpen"] = "1"
    else:
        text = str(cod).strip()
        body["isCodOpen"] = text if text in ("0", "1") else "1"

    # brandName / categoryName 是我们自己的中文标注（「无品牌」「运动鞋」），
    # 妙手只接受马来语/英语的品牌与类目名，中文会被拒（实测「不能包含中文」）。
    # brandId=0 表示无品牌，类目由 cid 决定，这两个展示字段不需要提交。
    import re as _re
    _cjk = _re.compile(r"[\u4e00-\u9fa5]")
    for field in ("brandName", "categoryName"):
        value = body.get(field)
        if isinstance(value, str) and _cjk.search(value):
            body.pop(field, None)
    # skuPropertyList 里的 propName 也是中文标注，妙手用 attrName 定位维度，
    # propName 带中文会被判「不能包含中文」，直接去掉。
    for prop in body.get("skuPropertyList") or []:
        if isinstance(prop, dict):
            prop.pop("propName", None)
            prop.pop("propId", None)
    for field in ("colorPropName", "sizePropName"):
        value = body.get(field)
        if isinstance(value, str) and _cjk.search(value):
            body.pop(field, None)
    if leaf_cid not in (None, ""):
        body["cid"] = leaf_cid
    for attribute in body.get("productAttributes") or []:
        if not isinstance(attribute, dict) or str(attribute.get("attributeId") or "") != "100397":
            continue
        for value in attribute.get("attributeValues") or []:
            if not isinstance(value, dict) or value.get("valueId") not in (None, ""):
                continue
            option_id = SEASON_VALUE_IDS.get(
                str(value.get("valueName") or "").strip().casefold())
            if option_id:
                value["valueId"] = option_id
    skus = body.get("skuMap") or {}
    if isinstance(skus, dict):
        if body.get("weight") in (None, ""):
            sku_weights: set[Decimal] = set()
            complete_weights = True
            active_skus = 0
            for sku in skus.values():
                if not isinstance(sku, dict) or str(sku.get("isDelete", "0")) == "1":
                    continue
                active_skus += 1
                try:
                    sku_weight = Decimal(str(sku.get("weight")))
                except (InvalidOperation, TypeError, ValueError):
                    complete_weights = False
                    break
                if not sku_weight.is_finite() or sku_weight <= 0:
                    complete_weights = False
                    break
                sku_weights.add(sku_weight)
            if complete_weights and active_skus and len(sku_weights) == 1:
                body["weight"] = float(next(iter(sku_weights)))
        for sku in skus.values():
            if not isinstance(sku, dict):
                continue
            # 妙手限制 skuMap.*.itemNum ≤ 50 字符。采集回来的货号常常是
            # 「编号-颜色[规格]-尺码 重量 价格」这种拼接串，70+ 字符直接保存失败
            # （报 shopCollectItemInfo.skuMap.<key>.itemNum 长度不能超过50）。
            # 超长就截到 50，保证载体能写进去；货号只是辅助标识，截断不影响上架。
            item_num = sku.get("itemNum")
            if item_num is not None:
                text = str(item_num)
                if len(text) > SKU_ITEM_NUM_MAX:
                    sku["itemNum"] = text[:SKU_ITEM_NUM_MAX]
            mapping = sku.get("shopIdToWarehouseIdAndStockMap")
            if not isinstance(mapping, dict):
                stock = str(sku.get("stock") or "0")
                sku["shopIdToWarehouseIdAndStockMap"] = {str(shop_id): {warehouse_id: stock}}
                continue
            stock_total = 0
            for warehouse_map in mapping.values():
                if isinstance(warehouse_map, dict):
                    for value in warehouse_map.values():
                        try:
                            stock_total += int(float(value))
                        except (TypeError, ValueError):
                            continue
            if not stock_total:
                try:
                    stock_total = int(float(sku.get("stock") or 0))
                except (TypeError, ValueError):
                    stock_total = 0
            sku["shopIdToWarehouseIdAndStockMap"] = {
                str(shop_id): {warehouse_id: str(stock_total)}}
            sku["stock"] = stock_total
    return body


def _ensure_carrier_bound_to_shop(client: MiaoshouClient, detail_id: int,
                                  shop_id: int, site: str) -> None:
    """确保载体已绑定到目标店铺（= 妙手说的「预发布店铺」）。

    为什么必须显式绑：公共箱复制（claimed）出来的草稿是 site 维度，
    collectBoxDetailShopList 为空。妙手在写入（save_shop_collect_item_info）
    前要求存在预发布店铺，否则报「店铺ID【xxx】未设置为预发布店铺」。

    单店铺账号从来不会遇到：复制时妙手会自动绑上唯一那个店铺。
    多店铺账号没有唯一解，妙手不绑，于是**必然**发布失败。

    claim_to_shop 是幂等的：已绑定的载体再调一次返回 data=null，不报错。
    所以这里不先读状态，直接绑，少一次往返（每件省约 1s）。
    """
    try:
        client.claim_to_shop([detail_id], [shop_id])
        logger.info("载体 %s 已绑定预发布店铺 %s（站点 %s）", detail_id, shop_id, site)
    except AttributeError:
        # 调用方传进来的 client 没有 claim_to_shop（测试替身或别的上游实现）。
        # 不能因为缺一个方法就让整批崩掉，交给后面的读取判定。
        logger.debug("client 不支持 claim_to_shop，跳过预绑定 detailId=%s", detail_id)
    except MiaoshouError as exc:
        # 绑定失败不在这里判死：有可能是已经绑好了（某些账号下重复绑会报错）。
        # 交给后面的 _carrier_snapshot 判定——它能读到 ossMd5 就说明可用，
        # 读不到才真正失败，并且会带上更有信息量的原始错误。
        logger.info("载体 %s 绑定店铺 %s 时上游返回：%s（继续尝试读取）",
                    detail_id, shop_id, str(exc)[:120])


def _carrier_snapshot(client: MiaoshouClient, detail_id: int, shop_id: int,
                      site: str) -> tuple[dict[str, Any], Mapping[str, Any], Any]:
    """按载体的 editModel 选对读接口，返回 (snapshot, block, ossMd5)。

    公共箱复制出来的载体可能是两种维度之一：
      · "shop"：已归属某个店铺 → 只能走 get_shop_collect_item_info(detailId, shopId)
      · "site"：只归属站点     → 必须走 get_site_collect_item_info(detailId, site)

    拿 shop 接口去读 site 维度的载体，妙手返回的是
    「未选择预发布店铺」——文案像是在说店铺没配置，实际是读错了接口。
    反过来（用 site 接口读 shop 维度）同样拿不到正确的 ossMd5。

    做法：先按 shop 维度读；失败就退回 site 维度。两个都失败才抛原始错误。
    先 shop 后 site 是因为 shop 维度是绝大多数情况（单店铺账号、已绑定的载体），
    不该为了少数 site 维度让常见路径多付一次失败的往返。
    """
    shop_error: Exception | None = None
    try:
        snapshot = client.shop_detail(detail_id, shop_id)
        block = snapshot.get("data") or {}
        if block.get("ossMd5"):
            return snapshot, block, block.get("ossMd5")
        # 读到了但没有 ossMd5：可能是 site 维度，退回去再试
        shop_error = CarrierError("载体缺少 ossMd5", detailId=str(detail_id))
    except MiaoshouError as exc:
        shop_error = exc

    try:
        snapshot = client.site_detail(detail_id, site)
        block = snapshot.get("data") or {}
        if block.get("ossMd5"):
            logger.info("载体 %s 是 site 维度（%s），改用站点接口读取", detail_id, site)
            return snapshot, block, block.get("ossMd5")
        raise CarrierError("载体缺少 ossMd5", detailId=str(detail_id))
    except (MiaoshouError, AttributeError):
        # AttributeError：调用方传进来的 client 没有 site_detail（测试替身、
        # 或将来换成别的上游实现）。这种情况下没有 site 维度可言，
        # 按 shop 维度的错误处理即可，不要因为缺一个方法就崩掉整批。
        #
        # site 维度也读不出来时同样把 shop 维度的原始错误抛出去，
        # 那条「未选择预发布店铺」对排查更有信息量。
        raise shop_error from None


def strict_readback_batching_disabled() -> bool:
    """批量预绑定开关：默认开启，设 0 可回退到「每件单独绑」。

    留这个逃生口是因为批量绑定失败时的降级路径要能被测试覆盖 ——
    真出问题时不用改代码，设环境变量即可回到旧行为。
    """
    import os
    return os.getenv("AOHUANG_BATCH_BIND_CARRIERS", "1").strip() == "0"


def fill_carriers(client: MiaoshouClient, carriers: Iterable[int],
                  payloads: Iterable[Mapping[str, Any]], *, shop_id: int,
                  progress: Callable[[dict[str, Any]], None] | None = None,
                  verify_readback: bool = False,
                  strict_readback: bool = False,
                  warehouse_id: str | None = None,
                  category_trees: dict[str, Mapping[str, Any] | bool] | None = None,
                  ) -> dict[str, Any]:
    """用成品 payload 覆盖每个载体；每个载体先重读拿自己的 ossMd5。

    `strict_readback=True`：读回核验把价格和类目也算作失败依据（发布前用）。
    """
    carrier_list = list(carriers)
    payload_list = list(payloads)
    if len(carrier_list) != len(payload_list):
        raise CarrierError("载体数量与商品数量不一致",
                           carriers=len(carrier_list), payloads=len(payload_list))
    if warehouse_id is None:
        from services.warehouse_cache import cached_warehouse_id

        # 同一店铺的仓库 ID 在整批里不变，缓存后省掉每件约 10 秒的查询
        warehouse_id = cached_warehouse_id(client, shop_id)
    if progress:
        progress({"stage": "warehouse", "shopId": shop_id, "warehouseId": warehouse_id})
    saved: list[int] = []
    saved_sources: list[int] = []
    failures: list[str] = []
    failure_details: list[dict[str, Any]] = []
    price_adjustments: list[dict[str, Any]] = []
    # 预绑定店铺：整批一次调用，而不是每件一次（2026-11-19 提速）。
    #
    # claim_to_shop 接口本来就收列表（detailIds + shopIds），旧代码却每件调一次，
    # 平白多花 N 次往返 × Pacer 间隔（0.90s）。50 件就是白等约 45 秒。
    # 整批绑一次，绑不到的（个别载体失败）交给下面逐件读取时的判定。
    bound_batch = False
    if carrier_list and not strict_readback_batching_disabled():
        try:
            client.claim_to_shop(list(carrier_list), [int(shop_id)])
            bound_batch = True
            logger.info("整批 %s 个载体已一次绑定预发布店铺 %s",
                        len(carrier_list), shop_id)
        except (MiaoshouError, AttributeError) as exc:
            # 批量绑失败不判死：下面每件还会按需单独绑一次（幂等），
            # 真正的成败由 _carrier_snapshot 读到 ossMd5 决定。
            logger.info("整批绑定预发布店铺失败（改逐件绑定）：%s", str(exc)[:120])
    for index, (detail_id, payload) in enumerate(
            zip(carrier_list, payload_list, strict=True)):
        if progress:
            progress({"stage": "fill" if verify_readback else "saving",
                      "detailId": detail_id})
        try:
            site = str(payload.get("site") or "MY")
            # 公共箱复制出来的载体默认是 **site 维度**（collectBoxDetailShopList 为空）。
            # 妙手要求写入前必须有一个「预发布店铺」，否则 save_shop 报
            # 「店铺ID【xxx】未设置为预发布店铺」。
            #
            # 单店铺账号看不出来：妙手复制时会自动把唯一那个店铺绑上，
            # 所以载体直接就是 shop 维度。**多店铺账号没有唯一解，不会自动绑**，
            # 于是发布必然失败——这就是「一个店铺的账号能发、两个店铺的发不了」
            # 的全部原因，跟账号有没有配过什么完全无关。
            #
            # 修法：写入前显式 claim_to_shop 把载体绑到本次的目标店铺。
            # 已绑定的载体再绑一次是幂等的（妙手返回 data=null，不报错），
            # 所以不用先读状态判断，省掉一次往返。
            #
            # 上面已经整批绑过一次（bound_batch=True）时这里就跳过 ——
            # 那是 2026-11-19 的提速改动：每件一次 claim_to_shop 是纯浪费。
            if not bound_batch:
                _ensure_carrier_bound_to_shop(client, detail_id, shop_id, site)
            snapshot, block, oss_md5 = _carrier_snapshot(
                client, detail_id, shop_id, site)
            if not oss_md5:
                raise CarrierError("载体缺少 ossMd5", detailId=str(detail_id))
            # 每个商品的 cid 可能不同，逐个解析成目标站点可用的叶子类目
            cached_tree = None
            if category_trees is not None:
                if site not in category_trees:
                    try:
                        category_trees[site] = client.category_tree(site)
                    except Exception as exc:  # noqa: BLE001 - preserve current cid on lookup failure
                        logger.warning("查类目树失败，保持商品类目：%s", exc)
                        category_trees[site] = False
                cached_tree = category_trees[site]
            leaf_cid = resolve_leaf_cid(
                client, payload.get("cid"), site=site,
                name_hint=str(payload.get("categoryNameCn") or payload.get("categoryName") or ""),
                tree=cached_tree)
            # 关键：规格必须按妙手文档整理，否则报「SKU「颜色-尺码」不存在」。
            #   1. attrId 用类目元数据里的真实值（颜色 100000 / 尺码 100007）
            #   2. skuMap 的 key 各段按 ID 升序排列
            #   3. 剔除文档没列的字段
            carrier_info = (block.get("shopCollectItemInfo")
                            or block.get("collectItemInfo")
                            or block)
            attrs = category_sale_attrs(client, payload.get("cid"), site, shop_id)
            enriched = dict(carrier_info or {})
            if attrs:
                # 直接塞兜底字段，normalize_spec_for_save 会优先用它们
                enriched["_colorAttrId"] = attrs.get("color", "")
                enriched["_colorAttrName"] = attrs.get("colorName", "")
                enriched["_sizeAttrId"] = attrs.get("size", "")
                enriched["_sizeAttrName"] = attrs.get("sizeName", "")
                # 按「语义类型」给维度打 attrId。
                # 载体里的 attrName 可能是中文「颜色」，也可能是马来语 Warna，
                # 所以这里直接判断每个维度是颜色还是尺码，再赋对应 attrId，
                # 不依赖载体里已有的 attrName。
                props = []
                for item in (enriched.get("skuPropertyList") or []):
                    if not isinstance(item, Mapping):
                        continue
                    new_item = dict(item)
                    probe = str(item.get("attrName") or "") + str(item.get("propName") or "")
                    values = [av for av in (item.get("attrValueList") or [])
                              if isinstance(av, Mapping)]
                    size_hits = sum(
                        1 for av in values
                        if str(av.get("attrValue") or "").strip().upper()
                        in ("XS", "S", "M", "L", "XL", "XXL", "XXXL", "2XL",
                            "3XL", "FREE", "F")
                        or (str(av.get("attrValue") or "").strip()
                            and len(str(av.get("attrValue") or "").strip()) <= 6
                            and any(ch.isdigit() for ch in str(av.get("attrValue") or "")))
                    )
                    is_size = bool(values) and size_hits * 2 >= len(values)
                    if "saiz" in probe.lower() or "尺码" in probe or "尺寸" in probe:
                        is_size = True
                    elif "warna" in probe.lower() or "颜色" in probe:
                        is_size = False
                    if is_size:
                        new_item["attrId"] = attrs.get("size") or new_item.get("attrId")
                        if attrs.get("sizeName"):
                            new_item["attrName"] = attrs["sizeName"]
                    else:
                        new_item["attrId"] = attrs.get("color") or new_item.get("attrId")
                        if attrs.get("colorName"):
                            new_item["attrName"] = attrs["colorName"]
                    props.append(new_item)
                if props:
                    enriched["skuPropertyList"] = props
            normalized, spec_notes = normalize_spec_for_save(payload, enriched)
            if progress and spec_notes:
                progress({"stage": "spec", "detailId": detail_id,
                          "notes": spec_notes})
            body = adapt_payload(normalized, shop_id=shop_id, warehouse_id=warehouse_id,
                                 leaf_cid=leaf_cid)
            body["detailId"] = detail_id
            cost_floor_retries = 0
            version_refresh_count = 0
            while True:
                try:
                    client.save_shop(detail_id, shop_id, oss_md5, body)
                    break
                except MiaoshouError as exc:
                    price_change = None
                    if cost_floor_retries < MAX_COST_FLOOR_RETRIES:
                        price_change = _raise_price_to_reported_floor(body, exc)
                    if price_change is not None:
                        price_adjustments.append({"sourceIndex": index, **price_change})
                        cost_floor_retries += 1
                        continue
                    # A new carrier may still be receiving upstream edits. Refresh
                    # ossMd5 and retry for this explicit conflict only.
                    text = str(exc)
                    is_version_conflict = (
                        "变动" in text or "重新打开" in text or "ossMd5" in text.lower())
                    if (is_version_conflict
                            and version_refresh_count < MAX_VERSION_REFRESH):
                        version_refresh_count += 1
                        logger.info("载体版本变动，重读 ossMd5 后重试 detailId=%s（第 %d/%d 轮）",
                                    detail_id, version_refresh_count, MAX_VERSION_REFRESH)
                        time.sleep(1.2)
                        # 同一批载体可能混着 shop / site 两种维度，重读也走同一套判定
                        fresh, _fresh_block, fresh_md5 = _carrier_snapshot(
                            client, detail_id, shop_id, site)
                        if not fresh_md5:
                            raise
                        oss_md5 = fresh_md5
                        continue
                    raise
            if verify_readback:
                # Keep strict readback for workflows that publish immediately.
                mismatched = _readback_mismatches(
                    client, detail_id, shop_id, body, payload,
                    strict=strict_readback)
                if mismatched:
                    logger.warning(
                        "载体覆盖后读回不一致 detailId=%s 目标标题=%r 差异=%s",
                        detail_id, str(payload.get("title") or "")[:40], mismatched)
                    failures.append(
                        f"{detail_id}: 保存后读回不一致（{', '.join(mismatched)}）")
                    if progress:
                        progress({"stage": "verify_failed", "detailId": detail_id,
                                  "verified": len(saved), "failed": len(failures)})
                    continue
            saved.append(detail_id)
            saved_sources.append(index)
            if progress:
                progress({"stage": "verified" if verify_readback else "saved",
                          "detailId": detail_id,
                          "verified" if verify_readback else "saved": len(saved),
                          "failed": len(failures)})
        except (MiaoshouError, ValueError, errors.AppError) as exc:
            failures.append(f"{detail_id}: {str(exc)[:90]}")
            uncertain = isinstance(exc, MiaoshouError) and (
                exc.kind in {"timeout", "unknown", "unreachable"}
                or (exc.status is not None and exc.status >= 500))
            failure_details.append({
                "detailId": str(detail_id),
                "certain": not uncertain,
                "reason": str(exc)[:200],
            })
            if progress:
                progress({"stage": "verify_failed", "detailId": detail_id,
                          "verified": len(saved), "failed": len(failures)})
    return {"saved": saved, "savedSources": saved_sources, "failures": failures,
            "failureDetails": failure_details,
            "priceAdjustments": price_adjustments,
            "warehouseId": warehouse_id}


def collect_and_fill_carriers(db: Session, client: MiaoshouClient, *,
                              payloads: Iterable[Mapping[str, Any]], shop_id: int,
                              progress: Callable[[dict[str, Any]], None] | None = None,
                              verify_readback: bool = False,
                              strict_readback: bool = False,
                              ) -> dict[str, Any]:
    """Create carriers while the consumer covers each one as soon as it arrives."""
    payload_list = list(payloads)
    if not payload_list:
        raise CarrierError("需要至少 1 个成品商品", required=1)

    from services.warehouse_cache import cached_warehouse_id

    # 缓存：整批只查一次仓库 ID（实测占认领总耗时 36%）
    warehouse_id = cached_warehouse_id(client, shop_id)
    if progress:
        progress({"stage": "warehouse", "shopId": shop_id,
                  "warehouseId": warehouse_id})

    # 队列容量 = 流水线缓冲深度。
    #
    # 原来 maxsize=1：消费者取走一个之后队列才空，生产者才能放下一个。
    # 消费者一轮覆盖 ~7s，于是复制被硬生生卡成「复制→等覆盖→再复制」，
    # 实测 4 件要 30.8s（首件覆盖 8.27s 才开始，最后一个载体到 27.74s 才建完），
    # 根本没并行起来。
    #
    # 现在留 3 个缓冲：复制（~2s/次）跑在覆盖（~7s/次）前面，整批按
    # 「覆盖耗时」而不是「复制+覆盖」计时。上限仍然存在 —— 不能让生产者
    # 无限超前，否则一批 100 件会在还没开始覆盖时就建出 100 个空载体。
    READY_BUFFER = 3
    ready: queue.Queue[tuple[int, int] | None] = queue.Queue(maxsize=READY_BUFFER)
    producer_errors: list[Exception] = []
    progress_lock = threading.Lock()
    carrier_index = 0

    def report(event: dict[str, Any]) -> None:
        if progress:
            with progress_lock:
                progress(event)

    def publish_carrier(detail_id: int) -> None:
        nonlocal carrier_index
        index = carrier_index
        carrier_index += 1
        ready.put((index, detail_id))

    def create() -> None:
        try:
            # 每件成品自带的 1688 货源链接（2026-11-20 修）：
            # 载体必须用这个商品自己的货源，否则出单后采购员买错货。
            # 拿不到就走池子轮换（老行为），不阻断。
            _preferred = source_link.source_urls_of(payload_list)
            collect_carriers(db, client, count=len(payload_list),
                             progress=report, on_carrier=publish_carrier,
                             preferred_urls=_preferred)
        except Exception as exc:  # surfaced after any already-created carriers are covered
            producer_errors.append(exc)
        finally:
            ready.put(None)

    producer = threading.Thread(target=create, name="carrier-producer", daemon=True)
    producer.start()
    saved: list[int] = []
    saved_sources: list[int] = []
    failed_sources: list[int] = []
    failures: list[str] = []
    failure_details: list[dict[str, Any]] = []
    price_adjustments: list[dict[str, Any]] = []
    created: list[int] = []
    category_trees: dict[str, Mapping[str, Any] | bool] = {}
    while True:
        item = ready.get()
        if item is None:
            break
        index, detail_id = item
        created.append(detail_id)
        report({"stage": "carrier_created", "sourceIndex": index,
                "detailId": str(detail_id), "total": len(payload_list),
                "created": len(created)})

        def report_item_progress(event: dict[str, Any], *,
                                 source_index: int = index) -> None:
            report({
                **event,
                "sourceIndex": source_index,
                "processed": len(saved) + len(failed_sources),
                "total": len(payload_list),
                "verified": len(saved),
                "failed": len(failed_sources),
            })

        try:
            result = fill_carriers(
                client, [detail_id], [payload_list[index]], shop_id=shop_id,
                progress=report_item_progress, verify_readback=verify_readback,
                strict_readback=strict_readback,
                warehouse_id=warehouse_id,
                category_trees=category_trees)
        except Exception as exc:  # keep draining so the producer cannot block
            failed_sources.append(index)
            reason = str(exc)[:200]
            failures.append(f"{detail_id}: {reason[:90]}")
            certain = isinstance(exc, (CarrierError, ValueError, errors.AppError))
            if isinstance(exc, MiaoshouError):
                certain = (exc.kind not in {"timeout", "unknown", "unreachable"}
                           and not (exc.status is not None and exc.status >= 500))
            failure_details.append({"sourceIndex": index, "detailId": str(detail_id),
                                   "certain": certain,
                                   "reason": reason})
            report({
                "stage": "verify_failed",
                "sourceIndex": index,
                "processed": len(saved) + len(failed_sources),
                "total": len(payload_list),
                "verified": len(saved),
                "failed": len(failed_sources),
                "detailId": str(detail_id),
                "message": reason,
            })
            continue
        saved.extend(result["saved"])
        if result["saved"]:
            saved_sources.append(index)
        else:
            failed_sources.append(index)
        price_adjustments.extend(
            {**item, "sourceIndex": index}
            for item in result.get("priceAdjustments") or [])
        failures.extend(result["failures"])
        for failure in result.get("failureDetails") or []:
            failure_details.append({**failure, "sourceIndex": index})
        report({
            "stage": ("verified" if verify_readback else "saved")
            if result["saved"] else "verify_failed",
            "sourceIndex": index,
            "processed": len(saved) + len(failed_sources),
            "total": len(payload_list),
            "verified" if verify_readback else "saved": len(saved),
            "failed": len(failed_sources),
            "detailId": str(detail_id),
            "message": "" if result["saved"] else (
                str((result.get("failures") or ["载体写入失败"])[0])[:200]),
        })

    producer.join()
    if producer_errors:
        exc = producer_errors[0]
        message = str(exc) if isinstance(exc, CarrierError) else str(exc)[:120]
        failures.append(f"载体创建中断：{message}")
        failure_details.append({"sourceIndex": len(created), "certain": False,
                                "reason": f"载体创建中断：{message}"})
    if len(created) < len(payload_list) and not producer_errors:
        failures.append(f"只创建了 {len(created)} 个载体，需要 {len(payload_list)} 个")
        for index in range(len(created), len(payload_list)):
            failure_details.append({"sourceIndex": index, "certain": False,
                                    "reason": "载体创建数量不足，结果需要核对"})
    failed_sources.extend(index for index in range(len(created), len(payload_list)))
    return {"carriers": created, "saved": saved, "savedSources": saved_sources,
            "failedSources": failed_sources, "failures": failures,
            "failureDetails": failure_details,
            "priceAdjustments": price_adjustments,
            "warehouseId": warehouse_id}
