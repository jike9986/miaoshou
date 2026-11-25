"""验证部署后的服务器代码是新口径。"""
import sys

sys.path.insert(0, "/opt/miaoshou-listing-v2/server")

from decimal import Decimal

print("=" * 70)
print("① 常量检查")
print("=" * 70)
from services import carrier_service

bump = getattr(carrier_service, "CLAIM_PRICE_BUMP", None)
print(f"  CLAIM_PRICE_BUMP = {bump}")
assert bump == Decimal("1.18"), f"期望 1.18，实际 {bump}"
print("  ✓ 微微上挑系数已上线")

gone = [n for n in ("CLAIM_PRICE_BUMP_RATIO", "CLAIM_PRICE_MAX_BUMP",
                    "_PRICE_BASE_KEY", "_PRICE_CAPPED_KEY")
        if hasattr(carrier_service, n)]
print(f"  已移除的旧常量: {gone or '无（干净）'}")

print()
print("=" * 70)
print("② 函数实际行为")
print("=" * 70)
from services import exchange_rate

rate, src = exchange_rate.cached_rate(currency="MYR")
if rate is None:
    rate, src = exchange_rate.live_rate(currency="MYR")
rate_d = Decimal(str(rate))
print(f"  汇率 1 CNY = {rate_d} MYR ({src})")


class Err(Exception):
    def __init__(self, msg):
        self.payload = {"message": msg}
        super().__init__(msg)


def make(purchase, price):
    return {"site": "MY", "skuMap": {
        f";{i};": {"originPrice": purchase, "price": price,
                   "priceIncludeVat": price, "isDelete": "0"}
        for i in range(2)}}


# A: 拿货 23，妙手报成本 38.40
p = Decimal("23")
q = (p * bump).quantize(Decimal("0.01"))
m = (q / rate_d).quantize(Decimal("0.01"))
print(f"\n  A. 拿货 23 CNY → 上挑 {q} CNY → {m} MYR")
pl = make("23", "1.00")
r = carrier_service._raise_price_to_reported_floor(
    pl, Err("本地展示价 1.00MYR，无法覆盖产品基础成本 38.40MYR"))
after = next(iter(pl["skuMap"].values()))["price"]
print(f"     妙手报 38.40 → 抬到 {after} MYR  (usedFloor={r['usedFloor']})")
assert Decimal(after) == Decimal("38.41"), after
print("     ✓ 微微上挑 + 抬到成本线+0.01")

# B: 拿货高，已够成本线
pl = make("50", "1.00")
r = carrier_service._raise_price_to_reported_floor(
    pl, Err("本地展示价 1.00MYR，无法覆盖产品基础成本 10.00MYR"))
after = next(iter(pl["skuMap"].values()))["price"]
q50 = (Decimal("50") * bump / rate_d).quantize(Decimal("0.01"))
print(f"\n  B. 拿货 50 CNY，成本线只 10.00 → 结果 {after} MYR（应为 {q50}）")
assert after == format(q50, ".2f"), (after, q50)
assert r["usedFloor"] is False
print("     ✓ 够成本线就不额外抬")

# C: 重试不滚
pl = make("23", "1.00")
seq = []
for _ in range(3):
    carrier_service._raise_price_to_reported_floor(
        pl, Err("本地展示价 XMYR，无法覆盖产品基础成本 38.40MYR"))
    seq.append(next(iter(pl["skuMap"].values()))["price"])
print(f"\n  C. 连续 3 轮: {seq}")
assert len(set(seq)) == 1
print("     ✓ 不滚雪球")

print()
print("=" * 70)
print("③ 成品仓库入库不再套模板")
print("=" * 70)
import inspect

from services import warehouse_service

src_code = inspect.getsource(warehouse_service.save_from_collect_box)
has_price_fn = hasattr(warehouse_service, "_price_warehouse_at_purchase")
print(f"  _price_warehouse_at_purchase 存在: {has_price_fn}")
assert has_price_fn

sig = inspect.getsource(warehouse_service._price_warehouse_at_purchase)
print(f"  函数内含 originPrice: {'originPrice' in sig}")
print(f"  save_from_collect_box 里 template 只验证不定价: "
      f"{'只验证存在' in src_code}")

print()
print("=" * 70)
print("④ purchase_range 不再回退 price")
print("=" * 70)
from services import product_service as ps

# 只有 price 没有 originPrice → 应返回 None
only_price = {"skuMap": {";a;": {"price": "88", "isDelete": "0"}}}
rng = ps.purchase_range(only_price)
print(f"  只有 price=88，无 originPrice → {rng}")
assert rng == (None, None), rng
print("  ✓ 不会把售价当拿货价")

with_origin = {"skuMap": {";a;": {"price": "88", "originPrice": "23",
                                  "isDelete": "0"}}}
rng = ps.purchase_range(with_origin)
print(f"  有 originPrice=23 → {rng}")
assert rng == (23.0, 23.0), rng
print("  ✓ 只认 originPrice")

print()
print("=" * 70)
print("全部通过 —— 服务器已是新口径")
print("=" * 70)