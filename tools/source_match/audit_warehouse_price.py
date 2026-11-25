"""查库里有多少商品的 price 是旧口径（模板价）存的。

判定：把 originPrice 按新口径算一遍（×1.18 ÷ 汇率），和库里实际的 price 比。
差距大的 → 旧口径存的，需要重跑。
"""
import json
import warnings

warnings.filterwarnings("ignore")

from sqlalchemy import text

from db.session import SessionLocal
from services import exchange_rate

RATE = exchange_rate.fallback_rate("MYR")
try:
    live, _src = exchange_rate.cached_rate(currency="MYR")
    if live:
        RATE = live
except Exception:  # noqa: BLE001
    pass
BUMP = 1.18

db = SessionLocal()

total = db.execute(text(
    "select count(*) from products where deleted_at is null")).scalar()
print(f"成品仓库商品总数: {total}")

rows = db.execute(text("""
    select id, price_min, price_max, payload
    from products where deleted_at is null
""")).fetchall()

buckets = {"已符合新口径": 0, "旧口径（模板价）": 0, "只裸换算（未上挑）": 0,
           "无 originPrice": 0, "其它": 0}
samples = []

for pid, pmin, pmax, payload in rows:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:  # noqa: BLE001
            continue
    sku_map = (payload or {}).get("skuMap") or {}
    first = next((s for s in sku_map.values() if isinstance(s, dict)), None)
    if not first:
        buckets["无 originPrice"] += 1
        continue
    try:
        origin = float(first.get("originPrice") or 0)
        price = float(first.get("price") or 0)
    except (TypeError, ValueError):
        buckets["无 originPrice"] += 1
        continue
    if origin <= 0:
        buckets["无 originPrice"] += 1
        continue

    expect_new = origin * BUMP / float(RATE)      # 新口径
    expect_bare = origin / float(RATE)            # 只换算

    if price <= 0:
        buckets["其它"] += 1
        continue

    if abs(price - expect_new) / expect_new < 0.05:
        buckets["已符合新口径"] += 1
    elif abs(price - expect_bare) / expect_bare < 0.05:
        buckets["只裸换算（未上挑）"] += 1
    elif price > expect_new * 1.3:
        buckets["旧口径（模板价）"] += 1
        if len(samples) < 12:
            samples.append((pid, origin, round(price, 2),
                            round(expect_new, 2),
                            round(price / expect_new, 2)))
    else:
        buckets["其它"] += 1

print()
print("=" * 70)
print("判定结果")
print("=" * 70)
for k, v in buckets.items():
    pct = v / total * 100 if total else 0
    print(f"  {k:<22} {v:>6} 件  ({pct:.1f}%)")

print()
print(f"汇率 1 CNY = {RATE} MYR，上挑系数 {BUMP}")
print("新口径 price = originPrice × 1.18 ÷ 汇率")
print()

if samples:
    print("=" * 70)
    print("旧口径样本（库里 price 明显偏高）")
    print("=" * 70)
    print(f"{'id':>6} {'拿货CNY':>9} {'库里price':>10} {'应为':>9} {'倍数':>6}")
    for pid, o, p, e, r in samples:
        print(f"{pid:>6} {o:>9.2f} {p:>10.2f} {e:>9.2f} {r:>6.2f}×")

db.close()