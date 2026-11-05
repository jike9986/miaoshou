"""载体重建：把成品商品发布到「不同账号/店铺」的必经路径。

detailId 只在账号内唯一，所以跨店铺发布不能引用原商品。流程：
  1. 从链接池取一个 1688 链接，采集进公共采集箱（fetch_item）
  2. 认领进目标店铺的采集箱，一次要 N 份（claimed + serialNumber）
  3. 用成品 payload 覆盖每个载体（save_shop）

第 3 步是关键：标题、图片、SKU、类目、描述、价格全部来自仓库副本，
所以不需要再付一次翻译费。
"""
from __future__ import annotations

import logging
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
from services import setting_service
from services.miaoshou import MiaoshouClient, MiaoshouError

logger = logging.getLogger("aohuang.carrier")

_COST_FLOOR_PRICE = re.compile(
    r"(?:本地(?:显示|展示)价|售价展示(?:价)?)[^0-9]{0,16}"
    r"([0-9]+(?:\.[0-9]+)?)\s*MYR", re.IGNORECASE)
_COST_FLOOR_VALUE = re.compile(
    r"基础成本.*?([0-9]+(?:\.[0-9]+)?)\s*MYR", re.IGNORECASE)
MAX_COST_FLOOR_RETRIES = 12
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


def _raise_price_to_reported_floor(payload: Mapping[str, Any], exc: MiaoshouError
                                   ) -> dict[str, Any] | None:
    message = str((exc.payload or {}).get("message")
                  or (exc.payload or {}).get("msg") or exc)
    if "无法覆盖产品基础成本" not in message:
        return None
    old_match = _COST_FLOOR_PRICE.search(message)
    floor_match = _COST_FLOOR_VALUE.search(message)
    if old_match is None or floor_match is None:
        return None
    try:
        old_price = Decimal(old_match.group(1))
        floor = Decimal(floor_match.group(1))
    except InvalidOperation:
        return None
    if old_price <= 0 or floor < old_price:
        return None
    new_price = floor + Decimal("0.01")
    sku_map = payload.get("skuMap")
    if not isinstance(sku_map, dict):
        return None
    changed: list[str] = []
    for key, sku in sku_map.items():
        if not isinstance(sku, dict) or str(sku.get("isDelete", "0")) == "1":
            continue
        try:
            current = Decimal(str(sku.get("price")))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if current != old_price:
            continue
        sku["price"] = format(new_price, ".2f")
        include_vat = new_price
        try:
            old_include_vat = Decimal(str(sku.get("priceIncludeVat")))
            if old_include_vat.is_finite() and old_price > 0:
                include_vat = (old_include_vat * new_price / old_price).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP)
        except (InvalidOperation, TypeError, ValueError):
            pass
        sku["priceIncludeVat"] = format(include_vat, ".2f")
        changed.append(str(key))
    if not changed:
        return None
    return {"oldPrice": format(old_price, ".2f"),
            "costFloor": format(floor, ".2f"),
            "newPrice": format(new_price, ".2f"),
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


def _collect_box_snapshot(client: MiaoshouClient) -> set[int] | None:
    """Return visible TK draft IDs, or None when the pre-copy snapshot failed."""
    found: set[int] = set()
    try:
        for page_no in range(1, 4):
            listing = client.collect_box_list(page_no=page_no, page_size=50)
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
            if len(rows) < 50:
                break
    except (MiaoshouError, ValueError):
        return None
    return found


def collect_carriers(db: Session, client: MiaoshouClient, *, count: int,
                     progress: Callable[[dict[str, Any]], None] | None = None,
                     on_carrier: Callable[[int], None] | None = None,
                     max_rounds: int = 8) -> list[int]:
    """优先复制公共采集箱已有商品，返回上游明确确认的新草稿 ID。"""
    if count < 1:
        raise CarrierError("需要至少 1 个载体", required=0)
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
    while len(details) < count and failures < max_rounds:
        link = take_link(db) if common_id is None else None
        if common_id is None and link is None:
            raise CarrierError("公共采集箱没有可用商品，且没有可采集的货源链接",
                               available=len(details), required=count)
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
        if progress:
            progress({"stage": "claim", "commonId": common_id, "copies": copies})
        before_ids = _collect_box_snapshot(client)
        new_ids: list[int] = []
        for _ in range(copies):
            try:
                claimed = client.claim_common_to_platform([(common_id, "tiktok", 1)])
            except MiaoshouError as exc:
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
                raise CarrierError("复制回执数量不完整，请先检查采集箱，勿重复操作",
                                   commonId=common_id, requested=copies,
                                   returned=len(returned_ids),
                                   available=len(details) + len(new_ids), required=count)
            detail_id = returned_ids[0]
            if detail_id in new_ids or detail_id in details:
                raise CarrierError("复制接口返回了重复商品 ID，请先检查采集箱，勿重复操作",
                                   commonId=common_id, available=len(details) + len(new_ids),
                                   required=count)
            new_ids.append(detail_id)
            if on_carrier:
                on_carrier(detail_id)
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
                         body: Mapping[str, Any], product: Mapping[str, Any]) -> list[str]:
    """保存后重新拉一次做核验（默认已停用，见 fill_carriers 的 verify_readback）。

    为什么默认不核验（负责人 2026-11-04 明确要速度）：
      · 实测每件多花约 6 秒（二次完整拉取），占单件总耗时 40% 以上；
      · 载体是妙手刚复制出来的草稿，读回里 categoryCid / prices / translations
        与仓库快照结构天然不同，逐件都报「不一致」，等于必然失败；
      · 老版 fill_carriers 保存后根本不做读回，成败只看 save_shop 抛不抛。
    保留这个函数是为了需要严格核验的场合（如发布前单独复核）能显式打开。
    """
    from services import pre_save_check, readback_check

    try:
        snapshot = client.shop_detail(detail_id, shop_id)
    except (MiaoshouError, ValueError, errors.AppError):
        return []
    if not isinstance(snapshot, Mapping):
        return []
    data = snapshot.get("data") or {}
    if not isinstance(data, Mapping):
        return []
    nested = data.get("data")
    actual = (data.get("shopCollectItemInfo")
              or data.get("siteCollectItemInfo")
              or data.get("collectItemInfo")
              or (nested.get("shopCollectItemInfo") if isinstance(nested, Mapping) else None)
              or {})
    if not isinstance(actual, Mapping) or not actual:
        return []
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
    except (ValueError, TypeError, KeyError, AttributeError):
        return []
    # 载体场景下这三项与仓库快照口径必然不同，不作为失败依据
    ignored = {"categoryCid", "prices", "translations", "readbackUnavailable"}
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

    def norm(text: Any) -> str:
        return str(text or "").strip().casefold().replace(" ", "")

    # 维度配对上：优先按 attrName/propName 匹配
    pairs: list[tuple[int, int]] = []
    used_carrier: set[int] = set()
    for our_dim, our_prop in enumerate(our_props):
        our_name = norm(our_prop.get("propName") or our_prop.get("attrName"))
        matched = None
        for c_dim, c_prop in enumerate(carrier_props):
            if c_dim in used_carrier:
                continue
            c_name = norm(c_prop.get("propName") or c_prop.get("attrName"))
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
                by_name[norm(c_av.get("attrValue"))] = cid

        for index, our_av in enumerate(our_values):
            our_id = str(our_av.get("attrValueId") or "").strip()
            if not our_id:
                continue
            key = norm(our_av.get("attrValue"))
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

    notes.append("规格id重映射%d个" % len(id_maps))
    if merged_dup:
        notes.append("合并重复SKU%d条" % merged_dup)
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

    def norm(text: Any) -> str:
        return str(text or "").strip().casefold().replace(" ", "")

    # 颜色维度名 / 尺码维度名的多语言集合，用来识别「哪个维度是颜色」
    COLOR_WORDS = {"颜色", "色彩", "color", "colour", "warna"}
    SIZE_WORDS = {"尺码", "尺寸", "size", "saiz", "ukuran"}

    def kind(value: Any) -> str:
        text = norm(value)
        if text in COLOR_WORDS:
            return "color"
        if text in SIZE_WORDS:
            return "size"
        return ""

    carrier_by_kind: dict[str, Mapping[str, Any]] = {}
    for prop in carrier_props:
        k = kind(prop.get("attrName") or prop.get("propName"))
        if k and k not in carrier_by_kind:
            carrier_by_kind[k] = prop

    for index, prop in enumerate(our_props):
        our_name = prop.get("attrName") or prop.get("propName")
        # 优先按语义类型配，退化到按顺序
        target = None
        k = kind(our_name)
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
            notes.append("attrName→%s" % carrier_name)
        if carrier_prop_name and prop.get("propName") != carrier_prop_name:
            prop["propName"] = carrier_prop_name
        if target.get("attrId") not in (None, ""):
            prop["attrId"] = target.get("attrId")

    data["skuPropertyList"] = our_props
    return data, notes


_CATEGORY_ATTR_CACHE: dict[tuple[str, str, int], dict[str, str]] = {}


def category_sale_attrs(client: MiaoshouClient, cid: Any, site: str,
                        shop_id: int) -> dict[str, str]:
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
        return result          # 失败不缓存，下次还能重试
    if result:
        _CATEGORY_ATTR_CACHE[key] = result
    return result


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

    # 维度归类：颜色 / 尺码
    COLOR_WORDS = {"颜色", "色彩", "color", "colour", "warna"}
    SIZE_WORDS = {"尺码", "尺寸", "size", "saiz", "ukuran"}

    def kind(value: Any) -> str:
        text = str(value or "").strip().casefold().replace(" ", "")
        if text in COLOR_WORDS:
            return "color"
        if text in SIZE_WORDS:
            return "size"
        return ""

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
            if upper in ("XS", "S", "M", "L", "XL", "XXL", "XXXL", "2XL",
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
        k = kind(prop.get("attrName") or prop.get("propName"))
        if k and k not in carrier_by_kind:
            carrier_by_kind[k] = prop

    strict_props: list[dict[str, Any]] = []
    for dim, prop in enumerate(our_props):
        values = [av for av in (prop.get("attrValueList") or []) if isinstance(av, Mapping)]
        k = kind(prop.get("attrName") or prop.get("propName"))
        if not k:
            k = "size" if looks_like_size(values) else "color"
        src = carrier_by_kind.get(k) or {}
        attr_id = str(src.get("attrId") or prop.get("attrId") or "").strip()
        attr_name = str(src.get("attrName") or prop.get("attrName") or "").strip()
        # 载体里没有 attrId 时，用调用方塞进 carrier_info 的兜底字段
        if not attr_id:
            attr_id = str(clean.get(f"_{k}AttrId") or "").strip()
        if not attr_name:
            attr_name = str(clean.get(f"_{k}AttrName") or "").strip()

        # 规格值：直接用商品自己的规格（相当于给载体新增/删除规格值）。
        #
        # 实测结论（2026-11-04，三种 id 生成方式都验证过）：
        #   · 妙手接受任意非空 attrValueId 作为「新规格值」；
        #   · 所以不需要把商品颜色映射到载体已有的颜色（那反而会失败）；
        #   · 只要 attrId 对（颜色 100000 / 尺码 100007）+ id 非空 + skuMap 升序，
        #     妙手就会按我们提交的规格重建载体的规格表。
        # 名称去重：同维度重名会让妙手报「选项名称重复」。
        seen_labels: set[str] = set()
        entry = {
            "attrId": attr_id,
            "attrName": attr_name,
            "attrValueList": [],
        }
        for av in values:
            label = str(av.get("attrValue") or "").strip()
            if not label:
                continue
            key = label.casefold().replace(" ", "")
            if key in seen_labels:
                continue
            seen_labels.add(key)
            our_id = str(av.get("attrValueId") or "").strip()
            if not our_id:
                # 没有 id 就生成一个稳定的（妙手接受任意非空值）
                import hashlib as _hash

                our_id = _hash.md5(
                    ("%s|%s" % (attr_id, label)).encode("utf-8")).hexdigest()[:10]
            img = (av.get("imgUrl")
                   or av.get("imageUrl")
                   or (av.get("imgUrls") or [None])[0]
                   or "")
            entry["attrValueList"].append({
                "attrValueId": our_id,
                "attrValue": label,
                "imgUrl": str(img or ""),
            })
        if not entry["attrValueList"]:
            notes.append("维度%d规格值为空，跳过" % dim)
            continue
        strict_props.append(entry)
        if not attr_id:
            notes.append("维度%d缺attrId" % dim)

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
                name_to_new[label.casefold().replace(" ", "")] = str(
                    av.get("attrValueId") or "")

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
            # 先把旧 id 换成去重后的新 id（按名称反查）
            mapped = []
            ok = True
            for part in parts:
                if part in id_to_dim:
                    mapped.append(part)
                    continue
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

    notes.append("规格SKU规范化%d条" % len(new_map))
    if dropped:
        notes.append("丢弃无法对齐%d条" % dropped)
    if merged:
        notes.append("合并重复%d条" % merged)
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


def fill_carriers(client: MiaoshouClient, carriers: Iterable[int],
                  payloads: Iterable[Mapping[str, Any]], *, shop_id: int,
                  progress: Callable[[dict[str, Any]], None] | None = None,
                  verify_readback: bool = False,
                  warehouse_id: str | None = None,
                  category_trees: dict[str, Mapping[str, Any] | bool] | None = None,
                  ) -> dict[str, Any]:
    """用成品 payload 覆盖每个载体；每个载体先重读拿自己的 ossMd5。"""
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
    for index, (detail_id, payload) in enumerate(
            zip(carrier_list, payload_list, strict=True)):
        if progress:
            progress({"stage": "fill" if verify_readback else "saving",
                      "detailId": detail_id})
        try:
            snapshot = client.shop_detail(detail_id, shop_id)
            block = snapshot.get("data") or {}
            oss_md5 = block.get("ossMd5")
            if not oss_md5:
                raise CarrierError("载体缺少 ossMd5", detailId=str(detail_id))
            # 每个商品的 cid 可能不同，逐个解析成目标站点可用的叶子类目
            site = str(payload.get("site") or "MY")
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
            version_refresh_used = False
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
                    # ossMd5 once for this explicit conflict only.
                    text = str(exc)
                    is_version_conflict = (
                        "变动" in text or "重新打开" in text or "ossMd5" in text.lower())
                    if is_version_conflict and not version_refresh_used:
                        logger.info("载体版本变动，重读 ossMd5 后重试 detailId=%s", detail_id)
                        time.sleep(1.0)
                        fresh = client.shop_detail(detail_id, shop_id)
                        fresh_md5 = (fresh.get("data") or {}).get("ossMd5")
                        if not fresh_md5:
                            raise
                        snapshot = fresh
                        oss_md5 = fresh_md5
                        version_refresh_used = True
                        continue
                    raise
            if verify_readback:
                # Keep strict readback for workflows that publish immediately.
                mismatched = _readback_mismatches(
                    client, detail_id, shop_id, body, payload)
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

    ready: queue.Queue[tuple[int, int] | None] = queue.Queue(maxsize=1)
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
            collect_carriers(db, client, count=len(payload_list),
                             progress=report, on_carrier=publish_carrier)
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
