"""1688 货源链接：入仓保存 / 读回 / 有效性检查。

## 为什么需要这个模块（2026-11-20 负责人提出）

载体机制（`carrier_service.py`）会给每件待发布的商品造一个「空壳」，
空壳的 1688 采购链接来自**链接池轮换**（`take_link()` 挑最闲的一条）——
跟这件商品真实的货源毫无关系。用成品数据覆盖空壳后，标题/图/价/SKU 全对，
**但采购链接还是空壳自己那条**。店铺出单后采购员照着错误链接买货。

负责人原话：「店铺出单采购是最开始载体的链接而不是成品仓库的 1688 链接，
导致我需要去重新复制链接重新弄。」

## 实测出来的三个事实（决定了这个模块怎么写）

1. **成品 payload 里从来没有货源字段**。实测生产库 532 件商品，0 件有
   `sourceList` / `sourceItemId` / `oriItemId` —— 全是空。
2. **妙手接口也不返回链接**。调 `get_shop_collect_item_info`，返回里只有
   `originPrice`（采购价数字），没有任何 1688 链接字段。
3. **采集箱原件多数已删**。多件商品直接报「该产品在采集箱中已被删除」。

结论：**入仓那一刻是唯一能拿到链接的时机**，之后再无补救。所以
`capture_from_box_row()` 必须在 `save_from_collect_box` 里调用。

## 字段约定

存进 `Product.payload`，供后续载体生成直接取用：

    payload["sourceItemId"]   1688 offer id，纯数字（如 "620123456789"）
    payload["sourceUrl"]      标准商品页（https://detail.1688.com/offer/<id>.html）
    payload["sourceTitle"]    货源标题（人工核对时有用）
    payload["sourceCapturedAt"]  抓取时间戳

`sourceUrl` 是**用户可改**的（负责人要求「老师可以修改」），所以
`normalize_source_url()` 要能接受各种写法：裸 offer id、带参数的分享链接、
`m.1688.com` 移动端链接等。
"""
from __future__ import annotations

import re
import time
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit

# 标准商品页模板。1688 的 detail.1688.com/offer/<id>.html 是长期稳定的形式。
OFFER_URL_TEMPLATE = "https://detail.1688.com/offer/{offer}.html"

# 来源标记归一：上游 `source` 字段大小写不统一（1688 / 1688.com / 1688CN 混着来）
_SOURCE_1688 = {"1688", "1688.com", "1688cn", "alibaba1688", "alibaba"}

# offer id：1688 的是 9~15 位数字（实测都在 10~13 位）
_OFFER_ID_RE = re.compile(r"^\d{6,16}$")
# 从任意 URL 里抠 offer id：/offer/123456.html 是最常见形式
_URL_OFFER_RE = re.compile(r"/offer/(\d{6,16})(?:\.html|/|$)", re.IGNORECASE)
# 查询参数里的 offerId=（分享链接常见）
_QUERY_OFFER_RE = re.compile(r"[?&](?:offerId|offer_id|id)=(\d{6,16})", re.IGNORECASE)


def is_1688_source(value: Any) -> bool:
    """这个 `source` 标记算不算 1688。"""
    return str(value or "").strip().lower() in _SOURCE_1688


def extract_offer_id(value: Any) -> str:
    """从任意写法里抠出 1688 offer id；抠不到返回空串。

    接受：
        620123456789                          裸 id
        https://detail.1688.com/offer/620123456789.html
        https://m.1688.com/offer/620123456789.html?spm=...
        https://detail.1688.com/offer/620123456789.html?offerId=620123456789
        620123456789.html                     只有文件名
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if _OFFER_ID_RE.match(text):
        return text
    for pattern in (_URL_OFFER_RE, _QUERY_OFFER_RE):
        found = pattern.search(text)
        if found:
            return found.group(1)
    # 最后一个兜底：纯文件名形态的 "620123456789.html"
    head = text.split(".", 1)[0]
    if _OFFER_ID_RE.match(head):
        return head
    return ""


def normalize_source_url(value: Any) -> str:
    """把各种写法归一成标准商品页；认不出来返回空串（调用方据此拒绝保存）。"""
    offer = extract_offer_id(value)
    if offer:
        return OFFER_URL_TEMPLATE.format(offer=offer)
    # 不是 1688 的链接原样返回（负责人可能贴了别的采购渠道）
    text = str(value or "").strip()
    if text.startswith("https://") or text.startswith("http://"):
        return text
    return ""


def _is_1688_host(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return host == "1688.com" or host.endswith(".1688.com")


def capture_from_box_row(row: Mapping[str, Any] | None) -> dict[str, Any]:
    """从采集箱行（或 shop_detail 返回）里抽出货源信息。

    返回 `{}` 表示这行没有货源 —— 调用方**不要**写空字段进 payload，
    免得以后分不清「没抓到」和「抓到了但是空」。

    上游结构（实测）：

        row = {
          "title": "…",
          "sourceList": [
            {"source": "1688", "sourceItemId": "620123456789",
             "sourceUrl": "https://detail.1688.com/offer/….html"}
          ],
        }
    """
    if not isinstance(row, Mapping):
        return {}
    title = str(row.get("title") or "").strip()
    for source in (row.get("sourceList") or []):
        if not isinstance(source, Mapping):
            continue
        if not is_1688_source(source.get("source")):
            continue
        raw = (source.get("sourceItemId") or source.get("sourceUrl")
               or source.get("url") or "")
        offer = extract_offer_id(raw)
        url = normalize_source_url(raw)
        if not url:
            continue
        captured: dict[str, Any] = {
            "sourceUrl": url,
            "sourceCapturedAt": time.time(),
        }
        if offer:
            captured["sourceItemId"] = offer
        if title:
            captured["sourceTitle"] = title
        return captured
    return {}


def capture_from_payload(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """从已有 payload 里回收货源（`sourceList` 等历史字段）。

    给「补录历史数据」用。注意这是最后手段 —— 实测 532 件全都没有，
    因为入仓时根本没存。真正的修复是让新入仓的商品存下来。
    """
    if not isinstance(payload, Mapping):
        return {}
    for source in (payload.get("sourceList") or []):
        if not isinstance(source, Mapping):
            continue
        if not is_1688_source(source.get("source")):
            continue
        raw = (source.get("sourceItemId") or source.get("sourceUrl")
               or source.get("url") or "")
        url = normalize_source_url(raw)
        if url:
            return {"sourceUrl": url,
                    **({"sourceItemId": extract_offer_id(raw)}
                       if extract_offer_id(raw) else {})}
    for key in ("sourceItemId", "oriItemId", "sourceUrl"):
        raw = str(payload.get(key) or "").strip()
        if not raw:
            continue
        url = normalize_source_url(raw)
        if url:
            return {"sourceUrl": url,
                    **({"sourceItemId": extract_offer_id(raw)}
                       if extract_offer_id(raw) else {})}
    return {}


def attach_source(payload: dict[str, Any], source: Mapping[str, Any]) -> bool:
    """把货源信息写进 payload（就地改）；返回是否写入了新东西。

    只补**缺的**字段：已有 `sourceUrl` 时不覆盖 —— 那是用户手动改过的，
    比采集箱里的自动抓取值可信。
    """
    if not isinstance(payload, dict) or not source:
        return False
    changed = False
    for key, value in source.items():
        if not value:
            continue
        if payload.get(key):
            continue
        payload[key] = value
        changed = True
    return changed


def source_url_of(payload: Mapping[str, Any] | None) -> str:
    """取 payload 里的货源链接（没有就返回空串）。"""
    if not isinstance(payload, Mapping):
        return ""
    text = str(payload.get("sourceUrl") or "").strip()
    if text:
        return text
    return normalize_source_url(payload.get("sourceItemId"))


def source_urls_of(payloads: Iterable[Mapping[str, Any] | None]) -> list[str]:
    """批量取货源链接，去重保序。给载体重建用。"""
    seen: list[str] = []
    for payload in payloads:
        url = source_url_of(payload)
        if url and url not in seen:
            seen.append(url)
    return seen


# --- 公共采集箱：覆盖已认领商品的货源链接 -----------------------------------
#
# **2026-11-20 实测确认的唯一可行路径。**
#
# 负责人指出两件关键事实：
#   1. 载体是「生成一个然后一直复制」，不是每次重新采集；
#   2. 一个妙手账号**每天只能采集 10 个产品** —— 靠重新采集换链接根本不够用。
# 所以必须「覆盖链接」而不是「重新采集」。
#
# 实测结论（三处接口我都试过）：
#
#   公共采集箱 `edit_common_collect_box_detail`   → ✅ 能改 sourceList
#        写入 826406158136 后回读一致，且**不消耗采集配额**（不是 fetch_item）
#   店铺采集箱 `save_shop_collect_item_info`      → ❌ 传 sourceList 返回 success
#        但回读是 null，上游直接忽略
#   `get_shop_collect_item_info`                 → 根本没有 sourceList 字段
#
# 所以正确顺序是：**先在公共箱把链接改对，再复制/认领到店铺。**

# 公共箱 stock 上限（实测 99999，超过就整单被拒：
# 「editCommonCollectBoxDetail.stock 不能大于 99999」）
COMMON_BOX_STOCK_CAP = 99999


def build_source_list(url: str) -> list[dict[str, Any]]:
    """构造公共箱可写的 sourceList 结构（实测上游接受的就是这五个字段）。"""
    normalized = normalize_source_url(url)
    if not normalized:
        return []
    offer = extract_offer_id(normalized)
    entry: dict[str, Any] = {
        "remark": None,
        "source": "1688",
        "sourceSite": "",
        "sourceItemUrl": normalized,
    }
    if offer:
        entry["sourceItemId"] = offer
    return [entry]


def override_common_box_source(client: Any, common_detail_id: int | str,
                               url: str) -> dict[str, Any]:
    """把公共采集箱里某个商品的货源链接改成 `url`。

    **不消耗采集配额**（走 edit 接口，不是 fetch_item），这正是它比
    「重新采集」有价值的地方 —— 每天只有 10 个采集额度。

    流程：读详情拿 `editCommonCollectBoxDetail` + `ossMd5`
         → 替换 sourceList（夹住 stock）
         → 整份回写 → 回读校验

    返回 `{ok, changed, before, after, error}`。
    失败不抛异常：调用方（载体重建 / 批量修链接）应该继续跑，只记失败。
    """
    from services.miaoshou import MiaoshouError

    wanted = normalize_source_url(url)
    if not wanted:
        return {"ok": False, "changed": False, "error": "货源链接无法识别"}
    if not str(common_detail_id).isdigit():
        return {"ok": False, "changed": False, "error": "公共箱 detailId 不合法"}

    try:
        detail = client.common_box_detail(int(common_detail_id))
    except MiaoshouError as exc:
        return {"ok": False, "changed": False, "error": f"读取公共箱失败：{exc}"}

    block = detail.get("data") or {}
    edit = block.get("editCommonCollectBoxDetail") or {}
    oss_md5 = block.get("ossMd5")
    if not isinstance(edit, dict) or not edit:
        return {"ok": False, "changed": False, "error": "公共箱详情缺少编辑结构"}
    if not oss_md5:
        return {"ok": False, "changed": False, "error": "公共箱详情缺少 ossMd5"}

    from copy import deepcopy

    before_list = deepcopy(edit.get("sourceList") or [])
    before = ""
    if before_list and isinstance(before_list[0], Mapping):
        before = str(before_list[0].get("sourceItemUrl")
                     or before_list[0].get("sourceItemId") or "")
    before_offer = extract_offer_id(before)
    wanted_offer = extract_offer_id(wanted)
    if before_offer and wanted_offer and before_offer == wanted_offer:
        return {"ok": True, "changed": False, "before": before, "after": wanted}

    body_edit = deepcopy(edit)
    body_edit["sourceList"] = build_source_list(wanted)
    # sourceMataInfo.oriItemNum 要与 sourceItemId 一致，否则上游认为货源没对齐
    if isinstance(body_edit.get("sourceMataInfo"), dict) and wanted_offer:
        body_edit["sourceMataInfo"]["oriItemNum"] = wanted_offer
    # stock 必须夹住：上游原始数据常是 60 万，直接回写会被拒
    if isinstance(body_edit.get("stock"), (int, float)) \
            and body_edit["stock"] > COMMON_BOX_STOCK_CAP:
        body_edit["stock"] = COMMON_BOX_STOCK_CAP
    for entry in (body_edit.get("skuMap") or {}).values():
        if isinstance(entry, dict):
            stock = entry.get("stock")
            if isinstance(stock, (int, float)) and stock > COMMON_BOX_STOCK_CAP:
                entry["stock"] = COMMON_BOX_STOCK_CAP

    try:
        client.edit_common_box_detail({
            "commonCollectBoxDetailId": int(common_detail_id),
            "ossMd5": oss_md5,
            "editCommonCollectBoxDetail": body_edit,
        })
    except MiaoshouError as exc:
        return {"ok": False, "changed": False, "before": before,
                "error": f"写入货源链接失败：{exc}"}

    # 回读校验：上游对不认识/不合法的字段是**静默忽略**（店铺接口就是这样），
    # 只有回读才能证明真改上了。
    try:
        verify = client.common_box_detail(int(common_detail_id))
        verified = ((verify.get("data") or {})
                    .get("editCommonCollectBoxDetail") or {}).get("sourceList") or []
        after = ""
        if verified and isinstance(verified[0], Mapping):
            after = str(verified[0].get("sourceItemUrl")
                        or verified[0].get("sourceItemId") or "")
        after_offer = extract_offer_id(after)
        if wanted_offer and after_offer != wanted_offer:
            return {"ok": False, "changed": False, "before": before,
                    "after": after, "error": "回读校验不符，上游没有接受该链接"}
        return {"ok": True, "changed": True, "before": before, "after": after}
    except MiaoshouError as exc:
        # 写成功了但读不回来：不算彻底失败，如实报告
        return {"ok": True, "changed": True, "before": before,
                "after": wanted, "verified": False,
                "error": f"已写入但回读失败：{exc}"}


# --- 货源链接有效性检查 -----------------------------------------------------
#
# 妙手开放平台自己的警告（负责人 2026-11-20 截图）：
#
#   「高危：不存在的链接也会 success 并采到无关的真实商品。
#     采集前务必自行校验链接有效性。」
#
# 也就是说 fetch_item 不做校验 —— 传一条死链进去，它会随便返回一件商品，
# 于是载体带着一条假货源继续往下走。所以我们要自己检。
#
# 检查分两级，都不消耗采集配额：
#   check=row    查本地：payload 里有没有链接、格式对不对（零成本，先跑）
#   check=live   请求 1688 商品页：HTTP 200 且页面里有该 offer 的标题
#                （1 次 HTTP，不发妙手接口，不占每天 10 个采集额度）

# 1688 商品页提取标题（三种常见结构，任一命中即可）
_TITLE_PATTERNS = (
    re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL),
    re.compile(r'"subject":"(.*?)"'),
    re.compile(r'name="description"\s+content="(.*?)"', re.IGNORECASE),
)

STATUS_OK = "ok"
STATUS_MISSING = "missing"      # payload 里没有货源链接
STATUS_MALFORMED = "malformed"  # 有值但认不出 offer id
STATUS_DEAD = "dead"            # 明确失效
STATUS_VERIFIED = "verified"    # 妙手采集箱里确认存在
STATUS_UNKNOWN = "unknown"      # 查不到，但不能断定失效


def _clean_title(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", str(raw or ""))
    return re.sub(r"\s+", " ", text).strip()[:160]


def verify_offer_live(url: str, *, client: Any = None,
                      timeout: float = 12.0) -> dict[str, Any]:
    """校验一条 1688 货源链接是否有效。

    ## 为什么不能用 HTTP 请求判断（实测走过的弯路）

    直接 GET `detail.1688.com/offer/<id>.html` 对**存在和不存在**的 offer
    返回的是同一个 2739 字节反爬壳页面（含 `_____tmd_____` 跳转脚本）。
    移动端 `m.1688.com` 同理，也是 2039 字节壳页。
    也就是说：**从服务器侧 HTTP 无法区分死活链接** —— 拿它当判据会全部误判成有效。

    ## 现在用的是妙手公共采集箱反查

    `common_box_list(source_item_keyword=<offerId>)` 能按货源 id 搜索：

        717267894593（真实存在）→ 搜到 1 行，标题正确
        999999999999（乱造）    → 0 行

    不消耗采集配额（走 list 接口，不是 fetch_item）。

    **局限要说清楚**：这个检索只能验证「妙手采集箱里有过这个 offer」，
    搜不到**不等于**链接失效 —— 可能只是这个账号没采过它。
    所以：
      · 搜到了  → STATUS_VERIFIED，可信
      · 搜不到  → STATUS_UNKNOWN，界面显示「无法确认」而不是「无效」
                 （避免让老师误删好链接）
    """
    normalized = normalize_source_url(url)
    offer = extract_offer_id(normalized or url)
    if not offer:
        return {"status": STATUS_MALFORMED, "offerId": "", "httpStatus": 0,
                "title": "", "error": "无法从链接里识别 1688 offer id"}
    if client is None:
        return {"status": STATUS_UNKNOWN, "offerId": offer, "httpStatus": 0,
                "title": "", "error": "没有可用账号，无法联网校验"}

    from services.miaoshou import MiaoshouError

    try:
        listing = client.common_box_list(page_no=1, page_size=5,
                                         source_item_keyword=offer)
    except MiaoshouError as exc:
        return {"status": STATUS_UNKNOWN, "offerId": offer, "httpStatus": 0,
                "title": "", "error": f"查询失败：{exc}"}
    except Exception as exc:  # noqa: BLE001 - 网络/上游异常都算「无法确认」
        return {"status": STATUS_UNKNOWN, "offerId": offer, "httpStatus": 0,
                "title": "", "error": f"{type(exc).__name__}: {exc}"[:160]}

    rows = (listing.get("data") or {}).get("detailList") or []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        for source in (row.get("sourceList") or []):
            if not isinstance(source, Mapping):
                continue
            if extract_offer_id(source.get("sourceItemId")
                                or source.get("sourceItemUrl")) == offer:
                return {"status": STATUS_VERIFIED, "offerId": offer,
                        "httpStatus": 200,
                        "title": _clean_title(str(row.get("title") or "")),
                        "error": ""}
    return {"status": STATUS_UNKNOWN, "offerId": offer, "httpStatus": 0,
            "title": "", "error": "妙手采集箱里没有该货源，无法确认（不代表失效）"}


def check_payload_source(payload: Mapping[str, Any] | None, *, client: Any = None,
                         timeout: float = 12.0) -> dict[str, Any]:
    """检查一件商品的货源链接。

    `client=None` 时只做本地格式检查（零请求，批量扫全库用这个）；
    传了 client 再调妙手反查确认。
    """
    url = source_url_of(payload)
    if not url:
        return {"status": STATUS_MISSING, "url": "", "offerId": "",
                "httpStatus": 0, "title": "", "error": "未记录货源链接"}
    offer = extract_offer_id(url)
    if not offer:
        return {"status": STATUS_MALFORMED, "url": url, "offerId": "",
                "httpStatus": 0, "title": "", "error": "链接里没有 1688 offer id"}
    result: dict[str, Any] = {"status": STATUS_OK, "url": url, "offerId": offer,
                              "httpStatus": 0, "title": "", "error": ""}
    if client is not None:
        checked = verify_offer_live(url, client=client, timeout=timeout)
        result.update(checked)
        result["url"] = url
    return result
