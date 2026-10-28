"""读回校验回归：判定必须与老版 product_workflow._readback_check 一致。

每条用例都是「老版会怎么判 → 新版必须同样判」，
放宽会漏掉真实的保存失败，收紧会把成功判成失败（重试一次就多扣一次钱）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import readback_check as rb

PASS = 0
FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"[OK ] {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name}: got={got!r} want={want!r}")


BASE = {
    "title": "Yoga Pants Women",
    "cid": "2001",
    "weight": "0.3",
    "packageLength": "30",
    "packageWidth": "20",
    "packageHeight": "5",
    "sizeChart": "https://x/size.png",
    "imgUrls": ["https://x/a.jpg", "https://x/b.jpg"],
    "notes": '<img src="https://x/d1.jpg"><img src="https://x/d2.jpg">',
    "originPrice": "10",
    "price": "10",
    "skuMap": {"s1": {"price": "10", "originPrice": "10", "weight": "0.3"}},
    "skuPropertyList": [
        {"attrName": "颜色", "attrValueList": [
            {"attrValue": "粉", "imageUrl": "https://x/sku1.jpg"}]}
    ],
}
COVERAGE_EMPTY = {"requestedFields": {}}


def run(actual, payload=None, original=None, coverage=None, **kw):
    return rb.readback_issues(
        payload or BASE, actual, original or BASE,
        coverage or COVERAGE_EMPTY, **kw)


# --- 逐项必须能抓到 ---------------------------------------------------------
check("完全一致 → 无问题", run(dict(BASE)), [])

check("标题被截断", run({**BASE, "title": "Yoga"}), ["title"])
check("类目没写进", run({**BASE, "cid": "9999"}), ["categoryCid"])
check("重量丢了", run({**BASE, "weight": "0"}), ["weight"])
check("详情图被过滤",
      run({**BASE, "notes": '<img src="https://x/d1.jpg">'}), ["detailImages"])
check("主图被替换",
      run({**BASE, "imgUrls": ["https://x/z.jpg"]}), ["mainImages"])
check("尺码图丢了", run({**BASE, "sizeChart": ""}), ["sizeChart"])
check("包裹尺寸缺一项", run({**BASE, "packageHeight": None}), ["packageDimensions"])

# 详情图顺序变了也算不一致——顺序决定详情页排版
check("详情图顺序变了",
      run({**BASE, "notes": '<img src="https://x/d2.jpg"><img src="https://x/d1.jpg">'}),
      ["detailImages"])

# --- 中文残留必须抓到（翻译没生效） -----------------------------------------
COV_TITLE = {"requestedFields": {"title": "translated"}}
# 读回是中文时，title 的「等于 payload」和「不含中文」两条都过不了——老版同样两条都报
check("翻译了但读回还是中文",
      run({**BASE, "title": "瑜伽裤女"}, coverage=COV_TITLE), ["title", "translations"])
check("翻译生效 → 通过",
      run({**BASE, "title": BASE["title"]}, coverage=COV_TITLE), [])

# 只检查翻译过的字段：没标 translated 的字段里留中文是正常的
# 只检查标了 translated 的字段：别的字段留中文不报（这里没动任何字段）
check("没标翻译的字段留中文不算错",
      run(dict(BASE), coverage=COV_TITLE), [])

# --- productAttributes 按集合比，不按位置 --------------------------------
ATTRS_PAYLOAD = {
    **BASE,
    "productAttributes": [
        {"attributeId": "a1", "attributeValues": [{"attributeValue": "EVA"}]},
        {"attributeId": "a2", "attributeValues": [{"attributeValue": "Pink"}]},
    ],
}
ATTRS_COV = {"requestedFields": {"productAttributes": "translated"}}

check("属性顺序被打乱 → 通过（上游本来就会重排）",
      run({**BASE, "productAttributes": [
          {"attributeId": "a2", "attributeValues": [{"attributeValue": "Pink"}]},
          {"attributeId": "a1", "attributeValues": [{"attributeValue": "EVA"}]},
      ]}, payload=ATTRS_PAYLOAD, coverage=ATTRS_COV), [])

check("属性值被改 → 抓到",
      run({**BASE, "productAttributes": [
          {"attributeId": "a1", "attributeValues": [{"attributeValue": "PU"}]},
      ]}, payload=ATTRS_PAYLOAD, coverage=ATTRS_COV), ["translations"])

check("属性读回还是中文 → 抓到",
      run({**BASE, "productAttributes": [
          {"attributeId": "a1", "attributeValues": [{"attributeValue": "粉"}]},
      ]}, payload=ATTRS_PAYLOAD, coverage=ATTRS_COV), ["translations"])

# --- 价格：开定价 vs 不开 ---------------------------------------------------
ORIG = {**BASE, "price": "10", "originPrice": "10",
        "skuMap": {"s1": {"price": "10", "originPrice": "10", "weight": "0.3"}}}
PRICED_PAYLOAD = {**ORIG, "price": "16.5",
                  "skuMap": {"s1": {"price": "16.5", "originPrice": "10", "weight": "0.3"}}}

# 不开定价时价格被改 = 错
check("没开定价但价格被改",
      run({**ORIG, "price": "16.5"}, payload=ORIG, original=ORIG), ["prices"])

# 开定价时 price 变了是预期，但 originPrice 必须原样
check("开定价且采购价保持 → 通过",
      run({**BASE, "price": "16.5",
           "skuMap": {"s1": {"price": "16.5", "originPrice": "10", "weight": "0.3"}}},
          payload=PRICED_PAYLOAD, original=ORIG, pricing_enabled=True), [])

check("开定价但采购价被改 → 抓到",
      sorted(run({**BASE, "price": "16.5", "originPrice": "12",
                  "skuMap": {"s1": {"price": "16.5", "originPrice": "12", "weight": "0.3"}}},
                 payload=PRICED_PAYLOAD, original=ORIG, pricing_enabled=True)),
      ["originPriceUnchanged", "prices"])

# --- SKU 图片 ---------------------------------------------------------------
check("SKU 图片被换",
      run({**BASE, "skuPropertyList": [
          {"attrName": "颜色", "attrValueList": [
              {"attrValue": "粉", "imageUrl": "https://x/other.jpg"}]}]}),
      ["skuImages"])

# --- 工具函数 ---------------------------------------------------------------
check("contains_cjk 中文", rb.contains_cjk("瑜伽"), True)
check("contains_cjk 英文", rb.contains_cjk("Yoga"), False)
check("contains_cjk 嵌套", rb.contains_cjk({"a": ["x", "粉"]}), True)
check("extract_img 顺序",
      rb.extract_detail_image_urls('<img src="a"><img src="b">'), ["a", "b"])
check("extract_img 转义",
      rb.extract_detail_image_urls('<img src="a?x=1&amp;y=2">'), ["a?x=1&y=2"])
check("get_path 数组",
      rb.get_path({"a": [{"b": 1}]}, "a[0].b"), 1)
check("get_path 缺失", rb.get_path({"a": 1}, "a.b.c"), None)
check("mismatch 中文标题",
      rb.translation_readback_matches(
          {"title": "Yoga"}, {"title": "Yoga"}, {"requestedFields": {"title": "translated"}}),
      True)

print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
sys.exit(1 if FAIL else 0)
