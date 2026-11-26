"""B-P0-1 根因收敛与修复边界 —— 全部结论的复跑脚本。

## 本脚本回答三个问题

1. `_is_price_field` 的子串匹配到底误判了哪些 key？哪些真的会进 product payload？
2. 修好它之后，其余校验会不会一起垮？（是「修对校验」还是「关掉校验」）
3. `readback_check.py:408-417` 是不是同一个根因的第二处表现？

## 用法

    cd miaoshou/server && python -X utf8 ../docs/audit/_verify_b_p0_1_keys.py

## 注意

脚本自己把数据目录指到临时目录，**不碰** `%LOCALAPPDATA%\\aohuang-desktop\\app.db`。
只有第 4 节里标注 [只读] 的那段会去读真实库做 key 普查，全程 rollback。
"""
from __future__ import annotations

import ast
import copy
import json
import os
import pathlib
import sys
import tempfile
from collections import Counter, defaultdict
from decimal import ROUND_HALF_UP, Decimal

_DATA_DIR = tempfile.mkdtemp(prefix="audit-b-p01-keys-")
os.environ["AOHUANG_DATA_DIR"] = _DATA_DIR
os.environ.pop("AOHUANG_DATABASE_URL", None)

SERVER = pathlib.Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(SERVER))
os.chdir(SERVER)

from services import pre_save_check as P  # noqa: E402
from services import readback_check as R  # noqa: E402

ORIG_IS_PRICE = P._is_price_field
ORIG_PRICES_UNCHANGED = P.prices_unchanged

RATE = "1.62550086"


# ══════════════════════════════════════════════════════════════════════
# 第 1 节：Q1 —— 子串匹配判 True 的全量 key 普查
# ══════════════════════════════════════════════════════════════════════

SCAN_FILES = [
    "services/warehouse_service.py", "services/collect_box_service.py",
    "services/publish_service.py", "services/carrier_service.py",
    "services/schedule_service.py", "services/product_service.py",
    "services/transform_service.py", "services/common_box_service.py",
    "services/price_template.py", "services/pre_save_check.py",
    "services/readback_check.py", "services/product_standard.py",
    "services/gate_service.py", "services/sync_service.py",
    "services/publish_job_service.py", "services/batch_service.py",
    "services/setting_service.py", "services/category_service.py",
    "api/warehouse.py", "api/collect_box.py", "api/groups.py",
    "api/system.py", "api/sync.py", "api/categories.py",
]

# 真正来自妙手上游、必须继续比的价格字段
UPSTREAM_PRICE = {"price", "priceIncludeVat", "originPrice",
                  "originPriceIncludeVat", "shopPrice", "salePrice",
                  "purchasePrice"}
# 会写进 product payload 的本地记账键
LOCAL_IN_PAYLOAD = {"_warehousePriceRate"}
# 只在 API 响应 / 路由参数 / 模板配置 / 表列名 / 读回诊断里出现的
NEVER_IN_PAYLOAD = {
    "priceAsc", "priceDesc", "price_min", "price_max", "price_currency",
    "purchasePriceMin", "purchasePriceMax", "purchasePriceCny",
    "priceTemplateList", "repriced", "prices", "priceAdjustments",
    "bySalePriceProfit", "continuedUnitPrice", "firstWeightPrice",
    "pretaxPrice", "originPriceUnchanged", "priceBefore", "priceAfter",
    "priceIncludeVatBefore", "priceIncludeVatAfter", "oldPrice", "newPrice",
}


def collect_keys():
    keys: dict[str, set[str]] = defaultdict(set)
    for rel in SCAN_FILES:
        path = pathlib.Path(rel)
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            vals: list = []
            if isinstance(node, ast.Dict):
                vals = [k.value for k in node.keys if isinstance(k, ast.Constant)]
            elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
                vals = [node.slice.value]
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  and node.func.attr in ("get", "pop", "setdefault")
                  and node.args and isinstance(node.args[0], ast.Constant)):
                vals = [node.args[0].value]
            for v in vals:
                if isinstance(v, str):
                    keys[v].add(rel.split("/")[-1])
    return keys


def section_q1() -> None:
    print("=" * 78)
    print("Q1  `_is_price_field` 判 True 的全量 key 普查")
    print("=" * 78)
    keys = collect_keys()
    hit = {k: v for k, v in keys.items() if ORIG_IS_PRICE(k)}
    print(f"扫描 {len(SCAN_FILES)} 个文件 / 候选字面量 {len(keys)} / "
          f"判 True {len(hit)}\n")

    def bucket(k: str) -> str:
        if k in UPSTREAM_PRICE:
            return "A 真实上游价格字段（必须继续比）"
        if k in LOCAL_IN_PAYLOAD:
            return "B 本地记账键，确实写进了 payload"
        if k in NEVER_IN_PAYLOAD:
            return "C 永不进 payload（误判但当前无实际后果）"
        return "D 未归类"

    groups: dict[str, list] = defaultdict(list)
    for k in sorted(hit):
        groups[bucket(k)].append((k, sorted(hit[k])))
    for g in sorted(groups):
        print(f"### {g}  （{len(groups[g])} 个）")
        for k, files in groups[g]:
            print(f"  {k:26} {', '.join(files)}")
        print()

    print("### 真实 payload 里的实际命中（`_price_fields` 收集到的路径）")
    real = {
        "site": "MY", "cid": "603751", "title": "T", "weight": 0.6,
        "packageLength": 33, "packageWidth": 22, "packageHeight": 12,
        "imgUrls": ["https://a/b.jpg"], "notes": '<img src="https://a/d.jpg">',
        "sizeChart": "https://aohuang888.com/s.png",
        "skuPropertyList": [{"attrName": "Warna",
                             "attrValueList": [{"attrValue": "Black"}]}],
        "skuMap": {";1;2;": {"price": "14.15", "priceIncludeVat": "14.15",
                             "originPrice": "23", "isDelete": "0", "stock": "9"}},
        "_warehousePriceRate": {"site": "MY", "currency": "MYR", "rate": RATE},
        "_costFloorAdjustments": [{"oldPrice": "14.15", "newPrice": "38.40",
                                   "costFloor": "38.40", "usedFloor": True}],
        "_sourceWeight": "0.6",
    }
    for path in sorted(P._price_fields(real)):
        leaf = path.split(".")[-1].split("[")[0]
        kind = "真实价格" if leaf in UPSTREAM_PRICE else "本地记账 ★误判"
        print(f"  {path:44} {kind}")
    print("\n  → 真实会命中「本地记账」的只有两处：")
    print("      _warehousePriceRate             ← 顶层新建的键，直接判为变更")
    print("      _costFloorAdjustments[0].oldPrice / .newPrice  ← 嵌套里含 price")
    print()


# ══════════════════════════════════════════════════════════════════════
# 第 2 节：修法 —— 前缀约定 + 三态受约束校验
# ══════════════════════════════════════════════════════════════════════


def fixed_is_price_field(key) -> bool:
    """`_` 开头 = 本地元数据，不是业务字段。前缀约定替代枚举。"""
    if str(key).startswith("_"):
        return False
    return ORIG_IS_PRICE(key)


def constrained_verdict(original, payload):
    """价格改写是否**恰好等于** originPrice / rate。三态返回。

    True  → 完全符合受约束改写，放行
    False → 确定不符合（值不对 / SKU 增减），直接拒绝，不再问别人
    None  → 没有 `_warehousePriceRate`，这条路径不适用
    """
    info = payload.get("_warehousePriceRate")
    rate = info.get("rate") if isinstance(info, dict) else info
    if not rate:
        return None
    try:
        rd = Decimal(str(rate))
    except Exception:
        return None
    if rd <= 0:
        return None

    before = {k: v for k, v in (original.get("skuMap") or {}).items()
              if isinstance(v, dict) and str(v.get("isDelete", "0")) != "1"}
    after = payload.get("skuMap") or {}
    after_active = {k for k, v in after.items()
                    if isinstance(v, dict) and str(v.get("isDelete", "0")) != "1"}
    if set(before) != after_active:
        return False
    for key, sku in before.items():
        want = format((Decimal(str(sku.get("originPrice") or "0")) / rd).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP), ".2f")
        got = after[key]
        if str(got.get("price")) != want:
            return False
        if str(got.get("priceIncludeVat")) != want:
            return False
        if str(got.get("originPrice")) != str(sku.get("originPrice")):
            return False
    return True


def fixed_prices_unchanged(original, payload):
    verdict = constrained_verdict(original, payload)
    if verdict is None:
        return ORIG_PRICES_UNCHANGED(original, payload)
    return verdict


# ══════════════════════════════════════════════════════════════════════
# 第 3 节：Q2 —— 改前/改后对照表，证明「修对」而不是「关掉」
# ══════════════════════════════════════════════════════════════════════

BASE = {
    "site": "MY", "cid": "603751", "title": "Running Shoes", "weight": 0.6,
    "packageLength": 33, "packageWidth": 22, "packageHeight": 12,
    "imgUrls": ["https://a/b1.jpg"],
    "notes": '<img src="https://a/d1.jpg">',
    "sizeChart": "https://aohuang888.com/s.png",
    "skuPropertyList": [{"attrName": "Saiz", "attrId": "100007",
                         "attrValueList": [{"attrValueId": "2", "attrValue": "41"}]}],
    "skuMap": {";1;2;": {"price": "38.41", "priceIncludeVat": "38.41",
                         "originPrice": "23", "isDelete": "0", "stock": "9",
                         "weight": "0.6", "itemNum": "A1"}},
}
EXPECTED = format((Decimal("23") / Decimal(RATE)).quantize(Decimal("0.01"),
                                                          rounding=ROUND_HALF_UP),
                  ".2f")


def priced(origin="23", price=None, **extra):
    """照抄 warehouse_service._price_warehouse_at_purchase 的算法。"""
    body = copy.deepcopy(BASE)
    local = price if price is not None else format(
        (Decimal(origin) / Decimal(RATE)).quantize(Decimal("0.01"),
                                                  rounding=ROUND_HALF_UP), ".2f")
    sku = body["skuMap"][";1;2;"]
    sku["price"] = local
    sku["priceIncludeVat"] = local
    sku["originPrice"] = origin
    body["_warehousePriceRate"] = {"site": "MY", "currency": "MYR",
                                   "rate": RATE, "rateSource": "有效缓存"}
    body.update(extra)
    return body


def case_rows():
    rows = []

    def add(label, original, payload, want_block):
        rows.append((label, original, payload, want_block))

    # —— 应放行：正常入仓的受约束改写 ——
    add("A1 正常入仓：草稿 38.41 → 改写成 " + EXPECTED, BASE, priced(), False)
    add("A2 正常入仓 + 成本线抬价记录", BASE,
        priced(_costFloorAdjustments=[{"oldPrice": EXPECTED, "newPrice": "38.40",
                                       "costFloor": "38.40", "usedFloor": True}]),
        False)
    add("A3 草稿价已等于改写结果（重复入仓）", priced(price=EXPECTED), priced(), False)

    # —— 必须拦住：真变化 ——
    p = priced()
    p["skuMap"][";1;2;"]["price"] = "99.99"
    add("B1 手改售价 99.99（不再等于 origin/rate）", BASE, p, True)

    p = priced()
    p["skuMap"][";1;2;"]["price"] = "14.16"
    add("B2 手改 1 分（14.15 → 14.16）", BASE, p, True)

    p = priced()
    p["skuMap"][";1;2;"]["priceIncludeVat"] = "16.00"
    add("B3 只改 priceIncludeVat", BASE, p, True)

    p = priced()
    p["skuMap"][";1;2;"]["originPrice"] = "19"
    add("B4 改采购价 originPrice 23 → 19", BASE, p, True)

    p = priced()
    p["skuMap"][";9;9;"] = {"price": EXPECTED, "priceIncludeVat": EXPECTED,
                            "originPrice": "23", "isDelete": "0"}
    add("B5 凭空多一个 SKU", BASE, p, True)

    p = priced()
    p.pop("_warehousePriceRate")
    add("B6 没有记账键的改价（受约束校验不该生效）", BASE, p, True)

    return rows


def run_table(title, patch_is_price: bool, patch_unchanged: bool) -> bool:
    P._is_price_field = fixed_is_price_field if patch_is_price else ORIG_IS_PRICE
    P._price_fields.__globals__["_is_price_field"] = P._is_price_field
    P.prices_unchanged = fixed_prices_unchanged if patch_unchanged \
        else ORIG_PRICES_UNCHANGED
    print(title)
    all_ok = True
    for label, original, payload, want in case_rows():
        blocked = not P.prices_unchanged(original, payload)
        ok = blocked == want
        if not ok:
            all_ok = False
        print(f"  {'OK ' if ok else '!! '}{label:44} "
              f"拦={'是' if blocked else '否':2} 期望={'拦' if want else '放'}")
    print(f"  → {'全部符合期望' if all_ok else '有失守项，见 !! 行'}\n")
    return all_ok


def section_q2() -> None:
    print("=" * 78)
    print("Q2  改前 / 改后对照表 —— 修好 B-P0-1 会不会把别的校验一起弄塌")
    print("=" * 78)
    print(f"基准：originPrice=23 CNY，rate={RATE} → 受约束改写应得 {EXPECTED}\n")
    run_table("--- 改前（现状：子串匹配）---", False, False)
    run_table("--- 只改 _is_price_field（前缀约定），不动价格校验 ---", True, False)
    ok = run_table("--- 前缀约定 + 三态受约束校验 ---", True, True)
    print(f"结论：{'两点都改之后，9 个用例全部符合期望' if ok else '仍有失守'}")
    print("      也就是说这是「把误判的本地元数据摘出去 + 把受约束的改写表达出来」，")
    print("      不是「不再比价格」。B1–B6 六种真变化一个都没漏。\n")

    P._is_price_field = ORIG_IS_PRICE
    P._price_fields.__globals__["_is_price_field"] = ORIG_IS_PRICE
    P.prices_unchanged = ORIG_PRICES_UNCHANGED


# ══════════════════════════════════════════════════════════════════════
# 第 4 节：其余 12 条规则是否仍然生效
# ══════════════════════════════════════════════════════════════════════


def section_rules() -> None:
    print("=" * 78)
    print("Q2 附  修好价格这一条之后，`pre_save_issues` 其余 13 条规则是否还在拦")
    print("=" * 78)
    P._is_price_field = fixed_is_price_field
    P._price_fields.__globals__["_is_price_field"] = fixed_is_price_field
    P.prices_unchanged = fixed_prices_unchanged

    good = priced()
    good.pop("_warehousePriceRate", None)
    good["skuMap"][";1;2;"]["price"] = "14.15"
    good["skuMap"][";1;2;"]["priceIncludeVat"] = "14.15"

    def call(payload, original=None, **kw):
        opts = dict(main_limit=9, detail_limit=30,
                    package_dimensions={"packageLength": 33, "packageWidth": 22,
                                        "packageHeight": 12},
                    size_chart_url="https://aohuang888.com/s.png",
                    process_images_enabled=True, image_only=False,
                    pricing_enabled=False, title_review_words=[],
                    image_review_pending=None, requirements=None)
        opts.update(kw)
        return P.pre_save_issues(original or good, payload, {}, **opts)

    rows = []
    rows.append(("基线：合规 payload", call(copy.deepcopy(good))))

    p = copy.deepcopy(good); p.pop("imgUrls")
    rows.append(("规则 5  主图全删", call(p)))
    p = copy.deepcopy(good); p["imgUrls"] = [f"https://a/x{i}.jpg" for i in range(12)]
    rows.append(("规则 5  主图 12 张超上限", call(p)))
    p = copy.deepcopy(good); p["notes"] = "<p>纯文字，没有图</p>"
    rows.append(("规则 7  详情区混入文字", call(p)))
    p = copy.deepcopy(good)
    p["notes"] = "".join(f'<img src="https://a/d{i}.jpg">' for i in range(31))
    rows.append(("规则 6  详情图 31 张超平台硬上限", call(p)))
    p = copy.deepcopy(good); p["packageLength"] = 40
    rows.append(("规则 3  包裹尺寸与设置不一致", call(p)))
    p = copy.deepcopy(good); p["sizeChart"] = "https://aohuang888.com/other.png"
    rows.append(("规则 4  尺码图不是选中的那张", call(p)))
    p = copy.deepcopy(good); p["imgUrls"] = ["https://a/CHANGED.jpg"]
    rows.append(("规则 8  关掉图片处理却改了主图",
                 call(p, process_images_enabled=False)))
    p = copy.deepcopy(good); p["skuMap"][";1;2;"]["originPrice"] = "19"
    rows.append(("规则 10 开了定价还动 originPrice", call(p, pricing_enabled=True)))
    p = copy.deepcopy(good)
    rows.append(("规则 13 标题命中需复核词",
                 call(p, title_review_words=["包邮", "爆款"])))
    p = copy.deepcopy(good)
    rows.append(("规则 14 主图 OCR 待复核",
                 call(p, image_review_pending={"blocked": True, "message": "厂家图"})))
    p = copy.deepcopy(good); p["cid"] = ""
    rows.append(("规则 2  类目 cid 缺失", call(p)))
    p = copy.deepcopy(good)
    rows.append(("规则 11 必填属性未填",
                 call(p, requirements={"packageDimensionRequired": False,
                                       "requiredAttributes": ["材质", "产地"]})))
    p = copy.deepcopy(good)
    p["packageLength"] = ""
    rows.append(("规则 12 类目要求包裹尺寸但缺项",
                 call(p, requirements={"packageDimensionRequired": True,
                                       "requiredAttributes": []})))
    rows.append(("规则 1  译文不完整",
                 P.pre_save_issues(good, copy.deepcopy(good), {"complete": False},
                                   main_limit=9, detail_limit=30,
                                   package_dimensions={"packageLength": 33,
                                                       "packageWidth": 22,
                                                       "packageHeight": 12},
                                   size_chart_url="https://aohuang888.com/s.png")))

    fired = 0
    for label, got in rows:
        is_baseline = label.startswith("基线")
        if not is_baseline:
            fired += bool(got)
        print(f"  {label:36} -> {'放行' if not got else '拦住 ' + str(got)}")
    print(f"\n  → 14 条里共 {len(rows) - 1} 条被逐一触发，命中 {fired} 条；基线放行。")
    print("    也就是说：修的是「哪两个键算价格字段」和「哪些改写是受约束的」，")
    print("    图片 / 尺寸 / 尺码图 / 标题 / OCR / 类目 / 译文 全都没动。\n")

    P._is_price_field = ORIG_IS_PRICE
    P._price_fields.__globals__["_is_price_field"] = ORIG_IS_PRICE
    P.prices_unchanged = ORIG_PRICES_UNCHANGED


# ══════════════════════════════════════════════════════════════════════
# 第 5 节：Q3 —— readback_check 是不是同一个根因的第二处表现
# ══════════════════════════════════════════════════════════════════════


def section_q3() -> None:
    print("=" * 78)
    print("Q3  `readback_check.py:408-417` 有没有同样的子串匹配")
    print("=" * 78)
    src = (SERVER / "services/readback_check.py").read_text(encoding="utf-8")
    has_substring = '"price" in ' in src or '"price" in str' in src
    print(f"readback_check.py 里出现 `\"price\" in ...`：{has_substring}")
    print(f"  _PRICE_FIELDS        = {R._PRICE_FIELDS}")
    print(f"  _ORIGIN_PRICE_FIELDS = {R._ORIGIN_PRICE_FIELDS}")
    print("  → 它是**显式元组**，不是子串匹配。所以 Q3 的答案是「不是同一种写法」，")
    print("    但它是同一个根因的**另一面**：靠手工枚举认字段。")
    print()

    print("### 表现二之一：枚举不全 → 任何新 *price* 键被静默忽略（宽松侧）")
    original = {"site": "MY",
                "skuMap": {";1;": {"price": "38.41", "originPrice": "23"}},
                "promoPrice": "1.00"}
    actual = {"site": "MY",
              "skuMap": {";1;": {"price": "38.41", "originPrice": "23"}},
              "promoPrice": "9999.00"}
    print(f"  新增 promoPrice 且读回被改成 9999：")
    print(f"    pre_save_check.prices_unchanged = "
          f"{P.prices_unchanged(original, actual)}   ← 子串匹配能发现")
    print(f"    readback._prices_unchanged     = "
          f"{R._prices_unchanged(original, actual)}    ← 枚举里没有这个名字，看不见")
    print(f"    readback._prices_match         = "
          f"{R._prices_match(original, actual)}    ← 同上")
    print()

    print("### 表现二之二：`:412` 的 elif 只认 `_costFloorAdjustments` 一个键名（严格侧）")
    upstream = {"site": "MY", "title": "T",
                "skuMap": {";1;": {"price": "38.41", "priceIncludeVat": "38.41",
                                   "originPrice": "23"}}}
    submitted = {"site": "MY", "title": "T",
                 "_warehousePriceRate": {"site": "MY", "rate": RATE},
                 "skuMap": {";1;": {"price": "14.15", "priceIncludeVat": "14.15",
                                    "originPrice": "23"}}}
    print("  提交 14.15（= 23/1.6255 的受约束改写）、读回 14.15、上游原价 38.41：")
    print(f"    带 _warehousePriceRate        -> "
          f"{R.readback_issues(submitted, submitted, upstream, {})}")
    swapped = dict(submitted)
    swapped.pop("_warehousePriceRate")
    swapped["_costFloorAdjustments"] = [{"oldPrice": "38.41", "newPrice": "14.15"}]
    print(f"    把同一个键换成 _costFloorAdjustments -> "
          f"{R.readback_issues(swapped, submitted, upstream, {})}")
    print("  → 只差一个键名，判定从「不一致」变成「一致」。")
    print("    所以修 B-P0-1 时必须把 `:412` 的条件从「认一个键名」改成")
    print("    「认 _ 前缀的本地记账键」，否则入仓放行了、读回还在拦。")
    print()

    print("### 第三处：读回里的 originPrice 保护也依赖 pricing_enabled")
    print("  `:409-411` 把 pricing_preserves_origin_price 挂在 `if pricing_enabled` 下面，")
    print("  而仓库入仓恒 pricing_enabled=False（B-P0-1 证据 4），于是走 `:417` 的 else，")
    print("  采购价保护整段落空。实测：")
    tampered = copy.deepcopy(submitted)
    tampered["skuMap"][";1;"]["originPrice"] = "19"
    print(f"    readback_issues(提交件, 读回把 originPrice 改成 19) = "
          f"{R.readback_issues(submitted, tampered, upstream, {})}")
    print(f"    → 与「读回完全正确」时的结果一模一样："
          f"{R.readback_issues(submitted, copy.deepcopy(submitted), upstream, {})}")
    print("      也就是说 originPrice 在仓库入仓这条路上根本没有读回保护。")
    print()


# ══════════════════════════════════════════════════════════════════════
# 第 6 节：只读普查真实库（确认影响面，不改任何数据）
# ══════════════════════════════════════════════════════════════════════


def section_db() -> None:
    print("=" * 78)
    print("影响面普查 [只读]：真实库里的 payload 到底被这个误判影响了多少")
    print("=" * 78)
    db = os.path.join(os.environ.get("LOCALAPPDATA", ""), "aohuang-desktop", "app.db")
    if not os.path.exists(db):
        print(f"  跳过：找不到 {db}")
        return
    import sqlite3

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        cur = con.cursor()
        total = cur.execute("select count(*) from products").fetchone()[0]
        rows = cur.execute("select id, payload from products").fetchall()
        parsed = 0
        top: Counter = Counter()
        sku_keys: Counter = Counter()
        with_sku = 0
        for _pid, raw in rows:
            if raw is None:
                continue
            try:
                p = json.loads(raw) if isinstance(raw, str) else raw
            except Exception:
                continue
            if not isinstance(p, dict) or not p:
                continue
            parsed += 1
            for k in p:
                top[k] += 1
            sm = p.get("skuMap")
            if isinstance(sm, dict) and sm:
                with_sku += 1
                for v in sm.values():
                    if isinstance(v, dict):
                        for k in v:
                            sku_keys[k] += 1
        print(f"  products 共 {total} 件；可解析 payload {parsed} 件（含 skuMap {with_sku}）")
        print(f"  payload 顶层 key：{dict(top)}")
        print(f"  skuMap 内每个 SKU 的 key：{dict(sku_keys)}")
        top_price = [k for k in top if ORIG_IS_PRICE(k)]
        sku_price = [k for k in sku_keys if ORIG_IS_PRICE(k)]
        print(f"  → 顶层命中 `_is_price_field` 的 key：{top_price or '（无）'}")
        print(f"  → skuMap 命中 `_is_price_field` 的 key：{sku_price}")
        for key in ("_warehousePriceRate", "_costFloorAdjustments",
                    "originPrice", "priceIncludeVat"):
            n = cur.execute("select count(*) from products where payload like ?",
                            (f"%{key}%",)).fetchone()[0]
            print(f"  payload 含 {key:24} = {n} 件")
        print()
        print("  读法：真实库里 `_warehousePriceRate` 一件都没有 —— 不是因为它不会写，")
        print("  是因为写入这行代码的上一行（价格改写）之后紧接着就被 422 拦在落库之前。")
        print("  这也说明这个 bug 是「先拦住，所以还没污染」：修好之后这个键才会开始进库，")
        print("  所以修复必须同时把它排除出「价格字段」和「发往上游的字段」，")
        print("  否则修好当天就会开始往妙手推这个本地键。")
        con.rollback()
    finally:
        con.close()
    print()


def main() -> None:
    print(f"数据目录（临时）: {_DATA_DIR}\n")
    section_q1()
    section_q2()
    section_rules()
    section_q3()
    section_db()
    print("=" * 78)
    print("一句话：根因是「用子串匹配判断业务字段」，不是「pricing_enabled 恒 False」。")
    print("        修法是「前缀约定 + 受约束改写」，不是「不比价格」。")
    print("=" * 78)


if __name__ == "__main__":
    main()