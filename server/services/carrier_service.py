"""载体重建：把成品商品发布到「不同账号/店铺」的必经路径。

detailId 只在账号内唯一，所以跨店铺发布不能引用原商品。流程：
  1. 从链接池取一个 1688 链接，采集进公共采集箱（fetch_item）
  2. 认领进目标店铺的采集箱，一次要 N 份（claimed + serialNumber）
  3. 用成品 payload 覆盖每个载体（save_shop）

第 3 步是关键：标题、图片、SKU、类目、描述、价格全部来自仓库副本，
所以不需要再付一次翻译费。
"""
from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from core import errors
from services import setting_service
from services.miaoshou import MiaoshouClient, MiaoshouError

LINK_POOL_KEY = "carrier.links"
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
def wait_until_collected(client: MiaoshouClient, common_id: int, *,
                         progress: Callable[[dict[str, Any]], None] | None = None,
                         rounds: int = COLLECT_SETTLE_ROUNDS) -> bool:
    """fetch_item 只是排队；未采集成功前认领会失败。"""
    for attempt in range(rounds):
        try:
            detail = client.common_box_detail(common_id)
        except MiaoshouError:
            time.sleep(COLLECT_SETTLE_SECONDS)
            continue
        data = detail.get("data") or {}
        info = data.get("editCommonCollectBoxDetail") or data
        status = str(info.get("status") or "").lower()
        if progress:
            progress({"stage": "waiting", "commonId": common_id,
                      "status": status or "?", "attempt": attempt + 1})
        if status in {"success", "collectsuccess"}:
            return True
        if status in {"fail", "collectfail"}:
            return False
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


def _fresh_box_ids(client: MiaoshouClient, common_id: int | None = None) -> list[int]:
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
        try:
            listing = client.collect_box_list(page_no=1, page_size=page_size)
        except (MiaoshouError, ValueError):
            return []
        rows = (listing.get("data") or {}).get("detailList") or []
        return [row for row in rows if isinstance(row, Mapping)]

    # 主路径：只认挂着这个公共箱 id 的行
    rows = scan(20)
    if common_id is not None:
        for row in rows:
            if str(row.get("commonCollectBoxDetailId")) == str(common_id):
                remember(row.get("collectBoxDetailId") or row.get("detailId"))
    if ids:
        return ids
    # 兜底：整页再扫一次（上游常把 20 条的上限悄悄截断，page_size=50 才给全）
    for row in scan(50):
        row_common = str(row.get("commonCollectBoxDetailId") or "")
        # 行上带了别的公共箱 id 就不是这次认领的产物；没带 id 的行无法区分，一律留下
        if common_id is not None and row_common and row_common != str(common_id):
            continue
        remember(row.get("collectBoxDetailId") or row.get("detailId"))
    return ids


def collect_carriers(db: Session, client: MiaoshouClient, *, count: int,
                     progress: Callable[[dict[str, Any]], None] | None = None,
                     max_rounds: int = 8) -> list[int]:
    """产出 count 个新草稿并返回 detailId。链接失败即退役并换下一个。"""
    if count < 1:
        raise CarrierError("需要至少 1 个载体", required=0)
    details: list[int] = []
    rounds = 0
    while len(details) < count and rounds < max_rounds:
        rounds += 1
        link = take_link(db)
        if link is None:
            raise CarrierError(
                "链接池里没有可用的 1688 链接了。请先按来源链接采集一件商品，"
                "或把采集箱里的商品链接加入链接池",
                available=len(details), required=count,
            )
        remaining = count - len(details)
        url = str(link.get("url") or "")
        offer = offer_id(url)
        common_id = find_in_common_box(client, offer) if offer else None
        if common_id:
            if progress:
                progress({"stage": "reuse", "link": url, "commonId": common_id})
        else:
            if progress:
                progress({"stage": "collect", "link": url, "need": remaining})
            try:
                fetched = client.fetch_items([url])
            except MiaoshouError as exc:
                mark(db, url, ok=False)
                if progress:
                    progress({"stage": "link_failed", "link": url,
                              "error": str(exc)[:120]})
                continue
            mapping = (fetched.get("data") or {}).get("sourceItemIdAndDetailIdMap") or {}
            ids = [int(value) for value in mapping.values() if str(value).isdigit()]
            if not ids:
                mark(db, url, ok=False)
                if progress:
                    progress({"stage": "link_failed", "link": url,
                              "error": "采集未返回商品"})
                continue
            common_id = ids[0]
            if not wait_until_collected(client, common_id, progress=progress):
                mark(db, url, ok=False)
                if progress:
                    progress({"stage": "collect_failed", "link": url})
                continue

        copies = min(remaining, 100)
        if progress:
            progress({"stage": "claim", "commonId": common_id, "copies": copies})
        try:
            claimed = client.claim_common_to_platform([(common_id, "tiktok", copies)])
        except MiaoshouError as exc:
            mark(db, url, ok=False)
            if progress:
                progress({"stage": "claim_failed", "error": str(exc)[:120]})
            continue
        mark(db, url, ok=True)
        new_ids = claimed_detail_ids(claimed)
        if not new_ids:
            # 认领已成功（费用已产生）却解析不出 detailId，先回头读采集箱捞草稿，
            # 否则这批载体既拿不到又白扣一次认领
            new_ids = _fresh_box_ids(client, common_id)
            if new_ids and progress:
                progress({"stage": "fresh_box", "commonId": common_id,
                          "found": len(new_ids)})
        if not new_ids:
            raise CarrierError("认领成功但没有拿到载体的 detailId",
                               commonId=common_id, required=count)
        for detail_id in new_ids:
            if len(details) >= count:
                break
            if detail_id not in details:
                details.append(detail_id)
    if len(details) < count:
        raise CarrierError(f"只创建了 {len(details)} 个载体，需要 {count} 个",
                           available=len(details), required=count)
    return details


def _readback_mismatches(client: MiaoshouClient, detail_id: int, shop_id: int,
                         body: Mapping[str, Any], product: Mapping[str, Any],
                         original_block: Mapping[str, Any]) -> list[str]:
    """保存后重新拉一次，返回对不上的字段（空列表＝确实写进去了）。

    只读不断言：读不回来时不做判断，交给上层按成功处理——
    上游偶发超时不该把一次成功的保存判成失败再重试一遍（会重复扣费/覆盖）。
    """
    from services import pre_save_check, readback_check

    try:
        snapshot = client.shop_detail(detail_id, shop_id)
    except (MiaoshouError, ValueError, errors.AppError):
        return []
    data = snapshot.get("data") or {}
    actual = (data.get("shopCollectItemInfo")
              or data.get("siteCollectItemInfo")
              or data.get("collectItemInfo")
              or (data.get("data") or {}).get("shopCollectItemInfo")
              or {})
    if not isinstance(actual, Mapping) or not actual:
        return []
    original = (original_block.get("shopCollectItemInfo")
                or original_block.get("siteCollectItemInfo")
                or {})
    coverage = (product.get("translation") or {}).get("coverage") or {}
    payload = dict(body)
    payload.pop("detailId", None)
    original_map = original if isinstance(original, Mapping) else {}
    # 有没有按模板定过价：原价和现价对不上就是定过价。
    # 定过价时 `price` 本来就该和原图不同，只有 originPrice 被改掉才算错。
    pricing_enabled = _priced(original_map, payload)
    # translate_images 只在「notes 已裁成纯详情图串」时才开。
    # 开了就要求 actual["notes"] 恰好等于 detail_images_only(notes)，
    # 而这里送上去的 body 是 adapt_payload 后的完整 notes（含文字描述），
    # 重算串必然对不上 → 每个载体都被判「读回不一致」→ saved 空 →
    # 跨店铺发布直接抛 CARRIER_UNAVAILABLE「载体全部写入失败」。
    # 老版 fill_carriers 保存后干脆不做读回，成败只看 save_shop 抛不抛。
    notes_only_images = (
        pre_save_check.detail_images_only(readback_check.extract_detail_image_urls(
            payload.get("notes"))) == payload.get("notes")
        if str(payload.get("notes") or "").strip() else False
    )
    try:
        return readback_check.readback_issues(
            payload, actual, original_map, coverage,
            pricing_enabled=pricing_enabled,
            translate_images=notes_only_images,
        )
    except (ValueError, TypeError, KeyError, AttributeError):
        # 比对器自己不熟这个 shape 时不要造假失败
        return []


def _priced(original: Mapping[str, Any], payload: Mapping[str, Any]) -> bool:
    """售价与原价不同，说明这份 payload 是按定价模板算过的。"""
    def number(value: Any) -> float | None:
        try:
            return float(str(value).strip())
        except (TypeError, ValueError):
            return None

    before = number(original.get("originPrice"))
    after = number(payload.get("price"))
    if before is None or after is None:
        return False
    return abs(before - after) > 1e-9


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


def adapt_payload(payload: Mapping[str, Any], *, shop_id: int,
                  warehouse_id: str) -> dict[str, Any]:
    """改写 payload 归属：目标 shopId + 目标仓库库存映射，库存数量保留。"""
    body = deepcopy(dict(payload))
    body["shopId"] = int(shop_id)
    skus = body.get("skuMap") or {}
    if isinstance(skus, dict):
        for sku in skus.values():
            if not isinstance(sku, dict):
                continue
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
                  progress: Callable[[dict[str, Any]], None] | None = None
                  ) -> dict[str, Any]:
    """用成品 payload 覆盖每个载体；每个载体先重读拿自己的 ossMd5。"""
    carrier_list = list(carriers)
    payload_list = list(payloads)
    if len(carrier_list) != len(payload_list):
        raise CarrierError("载体数量与商品数量不一致",
                           carriers=len(carrier_list), payloads=len(payload_list))
    warehouse_id = target_warehouse_id(client, shop_id)
    if progress:
        progress({"stage": "warehouse", "shopId": shop_id, "warehouseId": warehouse_id})
    saved: list[int] = []
    saved_sources: list[int] = []
    failures: list[str] = []
    for index, (detail_id, payload) in enumerate(
            zip(carrier_list, payload_list, strict=True)):
        if progress:
            progress({"stage": "fill", "detailId": detail_id})
        try:
            snapshot = client.shop_detail(detail_id, shop_id)
            block = snapshot.get("data") or {}
            oss_md5 = block.get("ossMd5")
            if not oss_md5:
                raise CarrierError("载体缺少 ossMd5", detailId=str(detail_id))
            body = adapt_payload(payload, shop_id=shop_id, warehouse_id=warehouse_id)
            body["detailId"] = detail_id
            client.save_shop(detail_id, shop_id, oss_md5, body)
            # 保存接口返回 200 不等于真的存进去了：平台会静默丢字段
            # （标题截断、详情图被过滤、翻译没生效）。老版靠读回比对确认，
            # 没有这步就是「看着成功、上架才发现是空的」。
            mismatched = _readback_mismatches(
                client, detail_id, shop_id, body, payload, snapshot.get("data") or {})
            if mismatched:
                failures.append(
                    f"{detail_id}: 保存后读回不一致（{', '.join(mismatched)}）")
                continue
            saved.append(detail_id)
            saved_sources.append(index)
        except (MiaoshouError, ValueError, errors.AppError) as exc:
            failures.append(f"{detail_id}: {str(exc)[:90]}")
        time.sleep(0.25)
    return {"saved": saved, "savedSources": saved_sources, "failures": failures,
            "warehouseId": warehouse_id}
