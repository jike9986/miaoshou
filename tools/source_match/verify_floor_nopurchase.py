"""验证 originPrice 缺失时，妙手报成本线仍能兜底抬价（不漏件）。

场景：老 payload 只有 price、没有 originPrice。妙手报成本线时，
如果直接 continue 跳过，这件商品永远认领不了。
"""
import sys

sys.path.insert(0, "/opt/miaoshou-listing-v2/server")

import warnings

warnings.filterwarnings("ignore")

from decimal import Decimal

from services import carrier_service
from services.miaoshou import MiaoshouError


def err(msg):
    return MiaoshouError(msg, payload={"message": msg})


print("=" * 76)
print("① 无 originPrice，妙手报成本线 → 应抬到成本线+0.01")
print("=" * 76)
msg = ("产品设置的本地展示价9.66MYR，无法覆盖产品基础成本"
       "(跨境物流运费+关税+增值税)：15.00MYR")
payload = {"skuMap": {"sku-a": {"price": "9.66"}}}
r = carrier_service._raise_price_to_reported_floor(payload, err(msg))
price = payload["skuMap"]["sku-a"]["price"]
print(f"  9.66 → {price}  (成本线 15.00)")
assert r is not None, "没抬价，这件会一直失败"
assert Decimal(price) == Decimal("15.01"), price
print("  ✓ 抬到 15.01")

print()
print("=" * 76)
print("② 无 originPrice 也无成本线 → 不动，不猜")
print("=" * 76)
payload2 = {"skuMap": {"sku-a": {"price": "9.66"}}}
r2 = carrier_service._raise_price_to_reported_floor(
    payload2, err("产品设置的本地展示价9.66MYR，无法覆盖产品基础成本"))
print(f"  结果: {r2}，价格 {payload2['skuMap']['sku-a']['price']}")
assert r2 is None and payload2["skuMap"]["sku-a"]["price"] == "9.66"
print("  ✓ 保持不变")

print()
print("=" * 76)
print("③ 无 originPrice，成本线低于当前价 → 不降")
print("=" * 76)
payload3 = {"skuMap": {"sku-a": {"price": "20.00"}}}
r3 = carrier_service._raise_price_to_reported_floor(
    payload3, err("产品设置的本地展示价20.00MYR，无法覆盖产品基础成本"
                  "(跨境物流运费+关税+增值税)：15.00MYR"))
print(f"  结果: {r3}，价格 {payload3['skuMap']['sku-a']['price']}")
assert r3 is None and payload3["skuMap"]["sku-a"]["price"] == "20.00"
print("  ✓ 不降")

print()
print("=" * 76)
print("④ 有 originPrice 走原路径（微微上挑）")
print("=" * 76)
payload4 = {"site": "MY", "skuMap": {
    ";a;": {"originPrice": "23", "price": "12.00"}}}
r4 = carrier_service._raise_price_to_reported_floor(
    payload4, err("产品设置的本地展示价12.00MYR，无法覆盖产品基础成本"
                  "(跨境物流运费+关税+增值税)：38.40MYR"))
p4 = payload4["skuMap"][";a;"]["price"]
print(f"  23 CNY 拿货 → 上挑 27.14 CNY → {p4} MYR，成本线 38.40")
assert Decimal(p4) == Decimal("38.41"), p4
print("  ✓ 抬到 38.41（成本线+0.01）")

print()
print("=" * 76)
print("⑤ 无 originPrice 时多轮重试不滚价")
print("=" * 76)
payload5 = {"skuMap": {"sku-a": {"price": "9.66"}}}
prev = None
for i in range(4):
    r5 = carrier_service._raise_price_to_reported_floor(
        payload5, err("产品设置的本地展示价9.66MYR，无法覆盖产品基础成本"
                      "(跨境物流运费+关税+增值税)：15.00MYR"))
    cur = payload5["skuMap"]["sku-a"]["price"]
    print(f"  第 {i+1} 轮: {cur}")
    if prev is not None:
        assert cur == prev, f"滚价了: {prev} → {cur}"
    prev = cur
print("  ✓ 稳定在 15.01，不滚价")

print()
print("全部通过")