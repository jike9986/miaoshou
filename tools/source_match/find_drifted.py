"""查清哪些商品的 SKU 重量被推高了（对比主重量与 SKU 重量）。"""
import os
import warnings

import psycopg

warnings.filterwarnings("ignore")

u = os.environ["AOHUANG_DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
with psycopg.connect(u) as conn:
    rows = conn.execute("""
        select detail_id, payload
        from products
        where deleted_at is null
          and payload->>'weight' is not null
          and payload->'skuMap' is not null
    """).fetchall()

    bad = []
    for did, payload in rows:
        try:
            main = float(payload.get("weight") or 0)
        except (TypeError, ValueError):
            continue
        skus = payload.get("skuMap") or {}
        weights = []
        for v in skus.values():
            if isinstance(v, dict):
                try:
                    weights.append(float(v.get("weight")))
                except (TypeError, ValueError):
                    pass
        if not weights:
            continue
        mx = max(weights)
        if mx - main > 0.25:
            bad.append((did, main, mx, len(weights)))

    print("SKU 重量明显高于主重量（差 > 0.25kg）的商品：%d 件" % len(bad))
    for did, main, mx, n in sorted(bad, key=lambda x: -(x[2] - x[1]))[:25]:
        print("  %-13s 主=%s  SKU最大=%s  (%d 个SKU)  差=%.1f" % (
            did, main, mx, n, mx - main))