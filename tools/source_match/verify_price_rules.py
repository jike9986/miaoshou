"""验证三条规则（负责人 2026-11-25）：

  1. 成品仓库里的价格只能是拿货价，不能套模板；
  2. 只有发布才套模板；
  3. 认领到采集箱价格低就往上提一点，不套模板。

用真实生产数据（#470：拿货 25 CNY，重 1.1kg）跑，看每一步落在什么数上。
"""
import sys

sys.path.insert(0, "server")

from decimal import Decimal

print("=" * 74)
print("① 成品仓库入库：_price_warehouse_at_purchase（不套模板）")
print("=" * 74)

from services.warehouse_service import _price_warehouse_at_purchase

payload = {
    "site": "MY",
    "skuMap": {
        ";a;": {"originPrice": "25", "price": "46.0", "priceIncludeVat": "46.0",
                "isDelete": "0"},
        ";b;": {"originPrice": "25", "price": "46.0", "priceIncludeVat": "46.0",
                "isDelete": "0"},
        ";c;": {"originPrice": "", "price": "46.0", "isDelete": "0"},
    },
}
priced = _price_warehouse_at_purchase(payload)
skus = priced["skuMap"]
a = skus[";a;"]
c = skus[";c;"]
rate_info = priced.get("_warehousePriceRate") or {}

print(f"  汇率信息: {rate_info}")
print(f"  SKU a: 拿货 25 CNY → price={a['price']} "
      f"priceIncludeVat={a['priceIncludeVat']}")
print(f"  SKU c: 拿货价缺失     → price={c['price']}（原样保留，不猜）")
expect = float(Decimal("25") / Decimal(rate_info.get("rate", "1.6255")))
print(f"  期望: 25 / {rate_info.get('rate')} = {expect:.2f}")
assert abs(float(a["price"]) - round(expect, 2)) < 0.02, a["price"]
print("  ✓ 仓库价 = 拿货价换算值，原来那个 46.0 的模板价没了")

print()
print("  仓库卡片显示的拿货价（purchase_range）不受影响：")
from services.product_service import purchase_range

pmin, pmax = purchase_range(priced)
print(f"    price_min={pmin} price_max={pmax}  ← 仍是 originPrice 的 25")

print()
print("=" * 74)
print("② 发布时才套模板：price_template.apply_template")
print("=" * 74)
from db.session import SessionLocal
from services import price_template, setting_service

db = SessionLocal()
try:
    tpls = setting_service.price_templates(db, operator_id=None)
    my_raw = next((r for n, r in tpls.items()
                   if str((r or {}).get("site", "")).upper() == "MY"), None)
    if my_raw:
        my = price_template.normalize_template(my_raw)
        result, detail = price_template.apply_template(
            {"site": "MY", "weight": "1.1", **payload}, my)
        first = next(iter((result.get("skuMap") or {}).values()))
        print(f"  马来模板（利润 {my.get('profitPercent')}%）:")
        print(f"    发布时算出 price = {first.get('price')}")
        print(f"    仓库里存的是     = {a['price']}  ← 不含模板")
        assert float(first["price"]) > float(a["price"]), "模板价应高于拿货价"
        print("  ✓ 模板只作用于发布，仓库里没有模板价")
    else:
        print("  (本机没有 MY 模板，跳过)")
finally:
    db.close()

print()
print("=" * 74)
print("③ 认领提价：拿货价 + 最多 15%，硬上限 60%")
print("=" * 74)
from services.carrier_service import (CLAIM_PRICE_BUMP_RATIO,
                                      CLAIM_PRICE_MAX_BUMP,
                                      _raise_price_to_reported_floor)


class FakeError(Exception):
    def __init__(self, msg):
        self.payload = {"message": msg}
        super().__init__(msg)


print(f"  RATIO={CLAIM_PRICE_BUMP_RATIO}  MAX={CLAIM_PRICE_MAX_BUMP}\n")

# 场景 A：妙手报的全成本是拿货价的 2.7 倍（生产实测）
pay = {"skuMap": {f";s{i};": {"price": "15.38", "priceIncludeVat": "15.38",
                              "isDelete": "0"} for i in range(3)}}
r = _raise_price_to_reported_floor(
    pay, FakeError("本地展示价 15.38MYR，无法覆盖产品基础成本 38.40MYR"))
after = next(iter(pay["skuMap"].values()))["price"]
print(f"  A. 拿货价 15.38，妙手要 38.40")
print(f"     → {after}  (上限 {15.38 * 1.6:.2f})")
assert float(after) <= 15.38 * 1.6 + 0.01, after
print(f"     旧行为: 38.41 ✗   现在: {after} ✓\n")

# 场景 B：已经高于成本线，不该动
pay = {"skuMap": {";s;": {"price": "50.00", "priceIncludeVat": "50.00",
                          "isDelete": "0"}}}
r = _raise_price_to_reported_floor(
    pay, FakeError("本地展示价 50.00MYR，无法覆盖产品基础成本 48.00MYR"))
print(f"  B. 价格 50.00 已高于成本 48.00 → 返回 {r}，价格保持 "
      f"{next(iter(pay['skuMap'].values()))['price']}")
assert r is None
print("     ✓ 不误加价（这是别的字段对不上，抬价治不了）\n")

# 场景 C：非成本错误
pay = {"skuMap": {";s;": {"price": "15.38", "isDelete": "0"}}}
r = _raise_price_to_reported_floor(pay, FakeError("商品信息有变动，请重新打开"))
assert r is None
print(f"  C. 非成本错误 → 不处理 ✓\n")

# 场景 D：反复加价不会滚雪球
pay = {"skuMap": {";s;": {"price": "15.38", "priceIncludeVat": "15.38",
                          "isDelete": "0"}}}
seq = []
for _ in range(4):
    _raise_price_to_reported_floor(
        pay, FakeError("本地展示价 X MYR，无法覆盖产品基础成本 900.00MYR"))
    seq.append(next(iter(pay["skuMap"].values()))["price"])
print(f"  D. 连续 4 次被拒后的价格序列: {seq}")
assert float(seq[-1]) <= 15.38 * 1.6 + 0.01, seq
print(f"     旧行为会滚到 15.38×1.15^4={15.38 * 1.15 ** 4:.2f}，"
      f"12 轮能到 {15.38 * 1.15 ** 12:.1f}")
print("     ✓ 封顶在上限，不会无限涨")

print()
print("=" * 74)
print("结论")
print("=" * 74)
print("① 成品仓库：price = 拿货价换算值，不套模板")
print("② 发布：才套模板")
print("③ 认领：拿货价 + 最多 15%，封顶 60%，绝不用妙手全成本口径")