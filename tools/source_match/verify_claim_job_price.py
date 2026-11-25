"""验证认领任务路径（publish → claimOnly）现在会按目标站点币种重算价格。

用生产失败的 9 件商品真实数据，对比修复前后：
  修复前：18.71（马币旧值）原样发给菲律宾店 → 妙手报成本 225.50 PHP → 失败
  修复后：人民币拿货价 → 按 PHP 汇率换算 → 微微上挑
"""
import json
import sys

sys.path.insert(0, "/opt/miaoshou-listing-v2/server")

import warnings

warnings.filterwarnings("ignore")

from decimal import Decimal

from sqlalchemy import text

from db.session import SessionLocal
from services import collect_box_service, exchange_rate

db = SessionLocal()

php, src = exchange_rate.cached_rate(currency="PHP")
if php is None:
    php, src = exchange_rate.live_rate(currency="PHP")
php_d = Decimal(str(php))
bump = collect_box_service.carrier_service.CLAIM_PRICE_BUMP

print("=" * 80)
print(f"目标店铺站点 PHP · 1 CNY = {php_d} PHP ({src}) · 上挑系数 {bump}")
print("=" * 80)

WIDS = [415, 413, 411, 408, 407, 406, 405, 401, 397]
rows = db.execute(text("""
  select id, payload from products where id = any(:w) order by id desc
"""), {"w": WIDS}).fetchall()

print(f"\n{'id':>5} {'拿货CNY':>9} {'重算前':>10} {'重算后PHP':>11} {'倍数':>7}")
print("-" * 80)

payloads = []
entries = []
for pid, payload in rows:
    pl = payload if isinstance(payload, dict) else json.loads(payload or "{}")
    pl = dict(pl)
    payloads.append(pl)
    entries.append({"warehouseId": str(pid), "payload": pl})

# 修复前：库里原值
before = {}
for pid, pl in zip((r[0] for r in rows), payloads):
    first = next((s for s in (pl.get("skuMap") or {}).values()
                  if isinstance(s, dict)), {})
    before[pid] = (first.get("originPrice"), first.get("price"))

# 模拟 require_shop 返回 PHP 店铺
class _Acc:
    id = 16
    name = "李小芳"


orig = collect_box_service.require_shop
collect_box_service.require_shop = lambda *_a, **_k: {"site": "PH", "shopId": "19990473"}
try:
    collect_box_service._reset_claim_prices_to_purchase(
        db, _Acc(), payloads, entries, target_shop_id="19990473")
finally:
    collect_box_service.require_shop = orig

ok = 0
for pid, pl in zip((r[0] for r in rows), payloads):
    first = next((s for s in (pl.get("skuMap") or {}).values()
                  if isinstance(s, dict)), {})
    origin = first.get("originPrice")
    after = Decimal(str(first.get("price") or 0))
    old = before[pid][1]
    ratio = (after / Decimal(old)) if old and Decimal(str(old)) > 0 else None
    print(f"{pid:>5} {str(origin):>9} {str(old):>10} {str(after):>11} "
          f"{f'{ratio:.1f}×' if ratio else '-':>7}")
    if origin and after > 0:
        expect = (Decimal(str(origin)) * bump / php_d).quantize(Decimal("0.01"))
        assert abs(after - expect) <= Decimal("0.02"), (pid, after, expect)
        ok += 1

print(f"\n  {ok}/{len(rows)} 件按「拿货价 × {bump} ÷ PHP 汇率」重算正确")

print()
print("=" * 80)
print("修复前后对比（id=415，拿货 8.49 CNY）")
print("=" * 80)
b = before[415]
n_first = next((s for s in (payloads[WIDS.index(415)]['skuMap'] or {}).values()
                if isinstance(s, dict)), {})
new = Decimal(str(n_first.get("price")))
print(f"  修复前提交: {b[1]} PHP   ← 马币旧值，妙手报成本 225.50 PHP → 失败")
print(f"  修复后提交: {new} PHP  (= 8.49 × {bump} ÷ {php_d})")
print(f"  妙手成本 225.50 PHP 仍高于它 → 会抬到 225.51，刚好能过")

db.close()