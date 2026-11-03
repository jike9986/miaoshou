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
import time
from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from core import errors
from services import setting_service
from services.miaoshou import MiaoshouClient, MiaoshouError

logger = logging.getLogger("aohuang.carrier")

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
def wait_until_collected(client: MiaoshouClient, common_id: int, *,
                         progress: Callable[[dict[str, Any]], None] | None = None,
                         rounds: int = COLLECT_SETTLE_ROUNDS) -> bool:
    """fetch_item 只是排队；未采集成功前认领会失败。"""
    for attempt in range(rounds):
        try:
            detail = client.common_box_detail(common_id)
        except MiaoshouError as exc:
            # 「商品已经采集过了」不是失败，是**已经就绪**。妙手对重复查询/已完成的
            # 采集直接回 fail + 这句提示，之前一律当成「还没好」继续等，白等到超时，
            # 再把链接标成坏的 —— 结果整个链接池被误判耗尽。
            message = str(exc)
            if "已经采集" in message or "already" in message.lower():
                if progress:
                    progress({"stage": "collected", "commonId": common_id,
                              "note": "already collected"})
                return True
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
        try:
            claimed = client.claim_common_to_platform([(common_id, "tiktok", copies)])
        except MiaoshouError as exc:
            raise CarrierError("复制结果未确认，请先检查采集箱，勿重复操作",
                               available=len(details), required=count) from exc
        if url:
            mark(db, url, ok=True)
        new_ids = list(dict.fromkeys(claimed_detail_ids(claimed)))
        if not new_ids:
            raise CarrierError("复制请求已提交但未返回新商品 ID，请先检查采集箱，勿重复操作",
                               commonId=common_id, required=count)
        if len(set(new_ids)) != copies or any(detail_id in details for detail_id in new_ids):
            raise CarrierError("复制回执数量不完整，请先检查采集箱，勿重复操作",
                               commonId=common_id, requested=copies, returned=len(set(new_ids)),
                               available=len(details), required=count)
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
    """保存后重新拉一次；无法核验时阻断发布，不自动重写草稿。"""
    from services import pre_save_check, readback_check

    try:
        snapshot = client.shop_detail(detail_id, shop_id)
    except (MiaoshouError, ValueError, errors.AppError):
        return ["readbackUnavailable"]
    if not isinstance(snapshot, Mapping):
        return ["readbackUnavailable"]
    data = snapshot.get("data") or {}
    if not isinstance(data, Mapping):
        return ["readbackUnavailable"]
    nested = data.get("data")
    actual = (data.get("shopCollectItemInfo")
              or data.get("siteCollectItemInfo")
              or data.get("collectItemInfo")
              or (nested.get("shopCollectItemInfo") if isinstance(nested, Mapping) else None)
              or {})
    if not isinstance(actual, Mapping) or not actual:
        return ["readbackUnavailable"]
    coverage = (product.get("translation") or {}).get("coverage") or {}
    payload = dict(body)
    payload.pop("detailId", None)
    # 载体原来的商品与仓库成品无关。核验的是本次提交的完整覆盖结果，
    # 包括已适配的类目、模板售价和成品采购价，不能与载体原值比或忽略。
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
        issues = readback_check.readback_issues(
            payload, actual, payload, coverage,
            pricing_enabled=True,
            translate_images=notes_only_images,
        )
    except (ValueError, TypeError, KeyError, AttributeError):
        return ["readbackUnavailable"]
    return issues


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
                     name_hint: str = "") -> Any:
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
    try:
        tree = client.category_tree(site)
    except Exception as exc:  # noqa: BLE001 - 拿不到类目树就别改，让上游自己报
        logger.warning("查类目树失败，保持原 cid=%s：%s", text, exc)
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


def adapt_payload(payload: Mapping[str, Any], *, shop_id: int,
                  warehouse_id: str, leaf_cid: Any = None) -> dict[str, Any]:
    """改写 payload 归属：目标 shopId + 目标仓库库存映射，库存数量保留。"""
    body = deepcopy(dict(payload))
    body["shopId"] = int(shop_id)
    if leaf_cid not in (None, ""):
        body["cid"] = leaf_cid
    skus = body.get("skuMap") or {}
    if isinstance(skus, dict):
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
            # 每个商品的 cid 可能不同，逐个解析成目标站点可用的叶子类目
            leaf_cid = resolve_leaf_cid(
                client, payload.get("cid"),
                site=str(payload.get("site") or "MY"),
                name_hint=str(payload.get("categoryNameCn") or payload.get("categoryName") or ""))
            body = adapt_payload(payload, shop_id=shop_id, warehouse_id=warehouse_id,
                                 leaf_cid=leaf_cid)
            body["detailId"] = detail_id
            try:
                client.save_shop(detail_id, shop_id, oss_md5, body)
            except MiaoshouError as exc:
                # 载体刚采集完，上游的 ossMd5 还在变（编辑器内容变动）。这不是致命错误：
                # 重新读一次最新 ossMd5 再保存即可。不重试就会一直卡在
                # 「该编辑器中产品内容发生变动，为避免覆盖…请重新打开编辑器」。
                text = str(exc)
                if "变动" in text or "重新打开" in text or "ossMd5" in text.lower():
                    logger.info("载体版本变动，重读 ossMd5 后重试 detailId=%s", detail_id)
                    time.sleep(1.0)
                    fresh = client.shop_detail(detail_id, shop_id)
                    fresh_md5 = (fresh.get("data") or {}).get("ossMd5")
                    if not fresh_md5:
                        raise
                    client.save_shop(detail_id, shop_id, fresh_md5, body)
                    snapshot = fresh
                else:
                    raise
            # 保存接口返回 200 不等于真的存进去了：平台会静默丢字段
            # （标题截断、详情图被过滤、翻译没生效）。老版靠读回比对确认，
            # 没有这步就是「看着成功、上架才发现是空的」。
            mismatched = _readback_mismatches(
                client, detail_id, shop_id, body, payload)
            if mismatched:
                # 读回不一致最常见的原因是「载体还是链接原本的商品」——
                # 比如用鞋子链接建的载体，覆盖时标题/图片没写进去，读回就还是鞋子。
                # 日志要把差异字段打出来，否则只看到「认领失败」无从判断。
                logger.warning("载体覆盖后读回不一致 detailId=%s 目标标题=%r 差异=%s",
                               detail_id, str(payload.get("title") or "")[:40], mismatched)
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
