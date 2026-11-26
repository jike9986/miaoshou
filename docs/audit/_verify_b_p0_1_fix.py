"""验证 B-P0-1 的根因修复方案：证明「能修对」而不只是「能绕过」。

## 为什么要单独验证「修法」

审计发现 B-P0-1 的根因是 `pre_save_check._is_price_field` 用子串匹配：

    def _is_price_field(key): return "price" in str(key).casefold()

它把纯本地记账键 `_warehousePriceRate` 判成业务价格字段，于是
「仓库不套模板 → 每次入仓改写价格 → 又不许价格变」三者撞成 422。

但最省事的「修法」是直接删掉价格校验 —— 那是把校验关掉，不是修对。
本脚本证明**存在一种改法：既放行合法的本地记账，又不放松对真实价格的守卫**。

## 两个关键设计

1. **本地字段用前缀约定，不枚举**
   `key.startswith("_")` → 所有 `_` 开头的都是本地元数据，以后新增的自动覆盖。
   这比 `_LOCAL_ONLY_PAYLOAD_FIELDS = (...)` 那种手工枚举更根本。

2. **放行本地记账键，但把「被改写的价格」纳入受约束校验**
   剔除噪声之后，价格确实会被 `_price_warehouse_at_purchase` 改写
   （拿货价按汇率换算成本币）。所以不能简单「不管价格」，
   而是要求：改写后的价格必须**严格等于** originPrice / rate。
   这样既允许正常改写，又拦住任何异常改价。

## 用法

    cd miaoshou/server && python -X utf8 ../docs/audit/_verify_b_p0_1_fix.py
"""
from __future__ import annotations

import copy
import os
import sys
import tempfile
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

_DATA_DIR = tempfile.mkdtemp(prefix="audit-b-p01-fix-")
os.environ["AOHUANG_DATA_DIR"] = _DATA_DIR
os.environ.pop("AOHUANG_DATABASE_URL", None)

SERVER = Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(SERVER))

from core import errors  # noqa: E402
from db.session import SessionLocal, init_db  # noqa: E402
from models.account import Account  # noqa: E402
from services import pre_save_check as P  # noqa: E402
from services import readback_check as R  # noqa: E402
from services import warehouse_service as ws  # noqa: E402


# --- 改动 1：前缀约定，消除枚举 ---------------------------------------
_ORIG_IS_PRICE = P._is_price_field


def _patched_is_price_field(key) -> bool:
    """`_` 开头的一律是本地元数据，不算业务价格字段。

    这一行替代了「维护一个 _LOCAL_ONLY_PAYLOAD_FIELDS 元组」的做法：
    以后任何新加的 `_xxx` 字段都自动被排除，不需要记得去改列表。
    """
    if str(key).startswith("_"):
        return False
    return _ORIG_IS_PRICE(key)


P._is_price_field = _patched_is_price_field


# --- 改动 2：受约束的价格校验（允许记账改写，但不许乱改）--------------
def _price_matches_rate(original: dict, payload: dict) -> bool | None:
    """入仓改写必须严格等于 originPrice / rate。返回 None 表示不适用。

    ## 为什么不能只比对「存在的 SKU」

    第一版实现遍历 `original` 的 SKU、在 `payload` 里找同 key 比对价格，
    结果**删掉整个 skuMap 时循环体一次都不执行，函数返回 True（放行）**。
    这是典型的「反向缺陷」：只为「值变了」设防，忘了「东西没了」。
    本脚本第一轮就抓到了这个漏洞（SKU 被删除 → 期望 False、实际 True）。

    修法：先要求 SKU 集合完全一致，再做逐项比对。
    """
    rate_info = payload.get("_warehousePriceRate")
    rate = rate_info.get("rate") if isinstance(rate_info, dict) else rate_info
    if not rate:
        return None
    rate_d = Decimal(str(rate))
    if rate_d <= 0:
        return None

    before = {k: v for k, v in (original.get("skuMap") or {}).items()
              if isinstance(v, dict) and str(v.get("isDelete", "0")) != "1"}
    after = payload.get("skuMap") or {}

    # ① 有效 SKU 集合必须完全一致（删了、加了都不行）
    after_active = {k for k, v in after.items()
                    if isinstance(v, dict) and str(v.get("isDelete", "0")) != "1"}
    if set(before) != after_active:
        return False

    # ② 逐项比对价格与拿货价
    for key, sku in before.items():
        want = format(
            (Decimal(str(sku.get("originPrice") or "0")) / rate_d).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP), ".2f")
        got = after[key]
        if str(got.get("price")) != want:
            return False
        if str(got.get("priceIncludeVat")) != want:
            return False
        # originPrice 是拿货价，任何情况下都不许变
        if str(got.get("originPrice")) != str(sku.get("originPrice")):
            return False
    return True


_ORIG_PRICES_UNCHANGED = P.prices_unchanged


def _patched_prices_unchanged(original, payload):
    """受约束校验优先；只有它「不适用」（None）时才回落到原始校验。

    ## 为什么不能把 False 也回落到原始校验

    第一版写的是：
        if verdict is True: return True
        return _ORIG_PRICES_UNCHANGED(original, payload)     # ← False 也走到这里

    于是「删掉整张 skuMap」被判 False 之后，原始校验又看了一眼
    （空 skuMap 没有价格字段可比 → 返回 True）→ 最终放行。
    **一个明确的「不一致」被另一个更弱的校验推翻了。**

    正确语义是三态：
        True  → 一致，放行
        False → 确定不一致，**直接拒绝，不再问别人**
        None  → 这条路径不适用（没有记账键），交给原始校验
    """
    verdict = _price_matches_rate(original, payload)
    if verdict is None:
        return _ORIG_PRICES_UNCHANGED(original, payload)
    return verdict


P.prices_unchanged = _patched_prices_unchanged


def _draft(price: str, origin: str = "23") -> dict:
    return {
        "title": "Running Shoes Breathable Sports Sneakers For Men",
        "cid": "603751", "weight": 0.6,
        "packageLength": 33, "packageWidth": 22, "packageHeight": 12,
        "imgUrls": ["https://cbu01.alicdn.com/1.jpg"],
        "notes": '<p><img src="https://cbu01.alicdn.com/d.jpg" /></p>',
        "skuPropertyList": [
            {"attrName": "Warna", "attrId": "100000",
             "attrValueList": [{"attrValueId": "1", "attrValue": "Black",
                                "imgUrl": "https://cbu01.alicdn.com/c.jpg"}]},
            {"attrName": "Saiz", "attrId": "100007",
             "attrValueList": [{"attrValueId": "2", "attrValue": "41"}]},
        ],
        "sizeChart": "https://aohuang888.com/s.png",
        "site": "MY", "language": "ms", "isCodOpen": "1",
        "skuMap": {";1;2;": {
            "price": price, "priceIncludeVat": price, "originPrice": origin,
            "isDelete": "0", "stock": "9", "weight": "0.6", "itemNum": "A1"}},
    }


class FakeClient:
    """够用的妙手假客户端：第一次读草稿，之后读回写进去的内容。

    `category_metadata` 必须返回可用的结构，否则 save_from_collect_box
    会在类目校验阶段就 502 退出，根本走不到价格校验 —— 那样测不出
    B-P0-1 到底修没修好。
    """
    draft = None
    saved = None
    reads = 0
    tamper = None

    def shop_detail(self, detail_id, shop_id):
        FakeClient.reads += 1
        body = copy.deepcopy(
            FakeClient.saved if (FakeClient.reads > 1 and FakeClient.saved)
            else FakeClient.draft)
        if FakeClient.tamper:
            FakeClient.tamper(body)
        return {"data": {"ossMd5": "m", "editModel": "shop",
                         "shopCollectItemInfo": body}}

    def save_shop(self, detail_id, shop_id, payload, md5):
        FakeClient.saved = copy.deepcopy(payload)

    def close(self):
        pass

    def category_metadata(self, *args, **kwargs):
        """返回草稿里已有的那两个属性，让规格校验能通过。"""
        return {
            "data": {
                "attrList": [
                    {"attrId": "100000", "attrName": "Warna",
                     "attrValueList": [{"attrValueId": "1", "attrValue": "Black"}]},
                    {"attrId": "100007", "attrName": "Saiz",
                     "attrValueList": [{"attrValueId": "2", "attrValue": "41"}]},
                ]
            }
        }


def main() -> None:
    print(f"数据目录（临时）: {_DATA_DIR}\n")

    print("=== 修复方案的判定逻辑直接验证（不跑整条保存链路）===\n")
    print("为什么直接测判定函数而不是跑 save_from_collect_box：")
    print("  整条链路要过类目/规格/图片/翻译/读回等多道校验，任何一道的")
    print("  假客户端不完美都会先 502 退出，测不到价格校验本身。")
    print("  而 B-P0-1 的根因就是这一个判定函数，直接测它更准。\n")

    rate = "1.6255"
    origin = "23"

    def payload_after(origin_price: str, price: str, **extra) -> dict:
        body = {
            "skuMap": {";1;2;": {
                "price": price,
                "priceIncludeVat": price,
                "originPrice": origin_price,
                "isDelete": "0", "stock": "9",
            }},
            "_warehousePriceRate": {"rate": rate, "site": "MY"},
        }
        body.update(extra)
        return body

    def original_of(origin_price: str = origin, price: str = "23.00") -> dict:
        return {
            "skuMap": {";1;2;": {
                "price": price,
                "priceIncludeVat": price,
                "originPrice": origin_price,
                "isDelete": "0", "stock": "9",
            }},
        }

    expected = format(
        (Decimal(origin) / Decimal(rate)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP), ".2f")
    print(f"基准：originPrice={origin} CNY, rate={rate} → 期望本币价 {expected}\n")

    cases = [
        ("正常入仓改写（价格=origin/rate）",
         original_of(price="23.00"), payload_after(origin, expected), True),
        ("价格被改高",
         original_of(price="23.00"), payload_after(origin, "99999.00"), False),
        ("价格被改低",
         original_of(price="23.00"), payload_after(origin, "1.00"), False),
        ("originPrice 被篡改",
         original_of(price="23.00"), payload_after("0.01", expected), False),
        ("SKU 被删除（整张 skuMap 清空）",
         original_of(price="23.00"),
         {"skuMap": {}, "_warehousePriceRate": {"rate": rate}}, False),
        ("没有记账键（非入仓路径，走原校验）",
         original_of(price="23.00"), {"skuMap": {";1;2;": {"price": "1.00"}}}, None),
    ]

    print(f"{'场景':38} {'期望':8} {'实际':8} 判定")
    print("-" * 70)
    all_ok = True
    for label, original, payload, want in cases:
        # 判定的是「最终是否放行」，也就是 pre_save_check 真正消费的那个值
        got = bool(_patched_prices_unchanged(original, payload))
        if want is None:
            print(f"{label:38} {'-':8} {str(got):8} （走原路径）")
            continue
        ok = got == want
        if not ok:
            all_ok = False
        print(f"{label:38} {str(want):8} {str(got):8} {'✓' if ok else '✗'}")

    print("\n=== 对照：修复前（子串匹配）会怎样 ===")
    P.prices_unchanged = _ORIG_PRICES_UNCHANGED
    P._is_price_field = _ORIG_IS_PRICE
    before = _ORIG_PRICES_UNCHANGED(
        original_of(price="23.00"),
        payload_after(origin, expected))
    print(f"  正常入仓改写 → prices_unchanged = {before}  "
          f"{'（正确）' if before else '★ 被误判为价格变更 → 422'}")

    print("\n=== 结论 ===")
    if all_ok:
        print("  ✓ 修复方案成立：正常改写放行，改价/改拿货价/删 SKU 全部拦住。")
        print("    这不是「关掉校验」，是「精确区分本地元数据与业务字段」。")
        print("\n  改动共两处，都在 pre_save_check.py：")
        print("    1. _is_price_field 加 `key.startswith('_')` 短路（前缀约定替代枚举）")
        print("    2. prices_unchanged 前置「受汇率约束的价格校验」")
    else:
        print("  ✗ 修复方案有漏洞，见上表 ✗ 行")


if __name__ == "__main__":
    main()
