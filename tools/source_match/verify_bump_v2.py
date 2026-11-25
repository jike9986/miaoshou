"""验证：认领只做「微微上挑」，妙手还嫌低就抬到成本线+0.01，刚好不报错。"""
import sys

sys.path.insert(0, "server")

from decimal import Decimal

from services import exchange_rate
from services.carrier_service import CLAIM_PRICE_BUMP, _raise_price_to_reported_floor

currency = exchange_rate.currency_for_site("MY")
rate, src = exchange_rate.cached_rate(currency=currency)
if rate is None:
    rate, src = exchange_rate.live_rate(currency=currency)
rate_d = Decimal(str(rate))


class Err(Exception):
    def __init__(self, msg):
        self.payload = {"message": msg}
        super().__init__(msg)


def make(purchase, price):
    return {"site": "MY", "skuMap": {
        f";{i};": {"originPrice": purchase, "price": price,
                   "priceIncludeVat": price, "isDelete": "0"}
        for i in range(2)}}


print("=" * 74)
print(f"CLAIM_PRICE_BUMP = {CLAIM_PRICE_BUMP}   汇率 1 CNY = {rate_d} MYR")
print("=" * 74)

print("\n【情形 1】平常情况：只微微上挑，妙手不报错")
print("-" * 74)
p = Decimal("23")
q = (p * CLAIM_PRICE_BUMP).quantize(Decimal("0.01"))
m = (q / rate_d).quantize(Decimal("0.01"))
print(f"  拿货 23.00 CNY → 上挑 {q} CNY → {m} MYR")
print(f"  直接提交这个值。妙手接受就不报错，流程结束。")

print("\n【情形 2】妙手报「基础成本 38.40」→ 抬到 38.41，刚好不报错")
print("-" * 74)
pl = make("23", str(m))
r = _raise_price_to_reported_floor(
    pl, Err(f"本地展示价 {m}MYR，无法覆盖产品基础成本 38.40MYR"))
after = next(iter(pl["skuMap"].values()))["price"]
print(f"  提交 {m} MYR → 妙手拒")
print(f"  抬到 {after} MYR  (= 成本线 38.40 + 0.01)")
print(f"  usedFloor={r['usedFloor']}  → 再提交就能过")
assert Decimal(after) == Decimal("38.41"), after

print("\n【情形 3】拿货价高、本就够成本线的商品，不会被抬")
print("-" * 74)
pl = make("50", "1.00")
r = _raise_price_to_reported_floor(
    pl, Err("本地展示价 1.00MYR，无法覆盖产品基础成本 10.00MYR"))
after = next(iter(pl["skuMap"].values()))["price"]
q50 = (Decimal("50") * CLAIM_PRICE_BUMP / rate_d).quantize(Decimal("0.01"))
print(f"  拿货 50 CNY，上挑后 {q50} MYR，已高于成本线 10.00")
print(f"  结果 {after} MYR，usedFloor={r['usedFloor']}")
assert after == format(q50, ".2f") and r["usedFloor"] is False

print("\n【情形 4】反复重试不会滚价")
print("-" * 74)
pl = make("23", str(m))
seq = []
for i in range(3):
    _raise_price_to_reported_floor(
        pl, Err(f"本地展示价 XMYR，无法覆盖产品基础成本 38.40MYR"))
    seq.append(next(iter(pl["skuMap"].values()))["price"])
print(f"  连续 3 轮：{seq}")
assert len(set(seq)) == 1, seq
print(f"  恒定在 {seq[0]}（每次都从拿货价重算，成本线也只抬到 +0.01）")

print("\n【情形 5】非成本错误 → 不动价格")
pl = make("23", str(m))
r = _raise_price_to_reported_floor(pl, Err("商品信息有变动，请重新打开"))
print(f"  {r}，价格保持 {next(iter(pl['skuMap'].values()))['price']}")
assert r is None

print()
print("=" * 74)
print("结论")
print("=" * 74)
print(f"· 平时：拿货价 × 1.18（23 → {q} CNY / {m} MYR）")
print(f"· 妙手嫌低：抬到它报的成本线 + 0.01（38.40 → 38.41），刚好不报错")
print(f"· 不会再出现「2.7 倍天价」，但也不会因为差价失败")
print(f"· 重试恒定，不滚雪球")