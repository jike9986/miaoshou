"""验算：SKU 重量 = 主重量 + N×增量？确定该减多少。"""
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

    print("%-13s %-8s %-9s %-9s %-8s %s" % (
        "detailId", "主重量", "SKU最大", "SKU最小", "推算次数", "所有取值"))
    print("-" * 90)
    n_bad = 0
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
        mx, mn = max(weights), min(weights)
        if mx - main <= 0.25:
            continue
        n_bad += 1
        # 推算加了几次 0.1
        times_est = round((mx - main) / 0.1)
        vals = sorted({round(w, 3) for w in weights})
        print("%-13s %-8s %-9s %-9s %-8s %s" % (
            did, main, mx, mn, times_est,
            vals if len(vals) <= 5 else f"{len(vals)}种"))
    print()
    print("共 %d 件" % n_bad)