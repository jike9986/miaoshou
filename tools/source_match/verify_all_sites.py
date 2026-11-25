"""验证：所有站点的成本线报错都能被识别并抬价。

现场报错原文（菲律宾，真实）：
  妙手接口失败 [fail]：产品设置的本地展示价95.89PHP，无法覆盖产品基础成本
  (跨境物流运费+关税+增值税)：225.50PHP，售卖可能导致亏损
"""
import sys

sys.path.insert(0, "/opt/miaoshou-listing-v2/server")

import warnings

warnings.filterwarnings("ignore")

from decimal import Decimal

from services import carrier_service

print("=" * 86)
print("① 正则匹配各站点报错")
print("=" * 86)

CASES = [
    ("PH", "产品设置的本地展示价95.89PHP，无法覆盖产品基础成本"
           "(跨境物流运费+关税+增值税)：225.50PHP，售卖可能导致亏损", "225.50"),
    ("MY", "产品设置的本地展示价16.70MYR，无法覆盖产品基础成本"
           "(跨境物流运费+关税+增值税)：38.40MYR，售卖可能导致亏损", "38.40"),
    ("TH", "产品设置的本地展示价58.20THB，无法覆盖产品基础成本"
           "(跨境物流运费+关税+增值税)：310.00THB，售卖可能导致亏损", "310.00"),
    ("VN", "产品设置的本地展示价45000VND，无法覆盖产品基础成本"
           "(跨境物流运费+关税+增值税)：128000VND，售卖可能导致亏损", "128000"),
    ("SG", "产品设置的本地展示价3.20SGD，无法覆盖产品基础成本"
           "(跨境物流运费+关税+增值税)：8.50SGD，售卖可能导致亏损", "8.50"),
]

allok = True
for site, msg, expect in CASES:
    m = carrier_service._COST_FLOOR_VALUE.search(msg)
    got = m.group(1) if m else None
    ok = got == expect
    allok &= ok
    print(f"  {site}: 期望 {expect:>8}  实际 {str(got):>8}  {'✓' if ok else '✗ 不匹配'}")

assert allok, "有站点没匹配上"

print()
print("=" * 86)
print("② 用真实菲律宾数据跑完整流程")
print("=" * 86)


class Err(Exception):
    def __init__(self, msg):
        self.payload = {"message": msg}
        super().__init__(msg)


REAL_MSG = ("妙手接口失败 [fail]：产品设置的本地展示价95.89PHP，"
            "无法覆盖产品基础成本(跨境物流运费+关税+增值税)：225.50PHP，"
            "售卖可能导致亏损")

pl = {"site": "PH", "skuMap": {
    ";a;": {"originPrice": "8.49", "price": "95.89",
            "priceIncludeVat": "95.89", "isDelete": "0"},
    ";b;": {"originPrice": "8.49", "price": "95.89",
            "priceIncludeVat": "95.89", "isDelete": "0"},
}}
print("  提交前 price = 95.89 PHP（拿货 8.49 CNY × 1.18 ÷ 0.1045）")
r = carrier_service._raise_price_to_reported_floor(pl, Err(REAL_MSG))
after = next(iter(pl["skuMap"].values()))["price"]
print(f"  妙手报成本 225.50 PHP")
if r is None:
    print("  ✗ 仍返回 None —— 没修好")
    sys.exit(1)
print(f"  抬价结果 = {after} PHP  (usedFloor={r['usedFloor']})")
assert Decimal(after) == Decimal("225.51"), after
print(f"  ✓ 抬到成本线 + 0.01，再提交就能过")
print(f"    实际会调用两次 save_shop：先 95.89 被拒 → 再 225.51 通过")

print()
print("=" * 86)
print("③ 各站点成本线 → 抬价后能否过")
print("=" * 86)

from services import exchange_rate

for site, cur, origin, floor_php in (
    ("PH", "PHP", "8.49", "225.50"),
    ("PH", "PHP", "20.89", "225.50"),
    ("PH", "PHP", "25.00", "495.50"),
    ("PH", "PHP", "19.00", "540.50"),
):
    rate, _s = exchange_rate.cached_rate(currency=cur)
    if rate is None:
        rate, _s = exchange_rate.live_rate(currency=cur)
    rate_d = Decimal(str(rate))
    quoted = (Decimal(origin) * carrier_service.CLAIM_PRICE_BUMP / rate_d
              ).quantize(Decimal("0.01"))
    floor = Decimal(floor_php)
    final = max(quoted, floor + Decimal("0.01"))
    print(f"  拿货 {origin:>6} CNY  上挑 {quoted:>8} PHP  "
          f"成本线 {floor:>8}  →  最终 {final:>8} PHP")

print()
print("=" * 86)
print("结论")
print("=" * 86)
print("· 根因：成本线正则写死 MYR，只有马来店能识别，其他国家全失效")
print("· 现在：币种用 [A-Z]{3} 通配，MY/PH/TH/VN/SG 都能识别")
print("· 菲律宾那 9 件：415 会被抬到 225.51，408 到 495.51，依此类推")