"""修正 `_sourceWeights` 的基准值。

复位时我按「主重量已经含过增量」把基准写成 `主重量 - 0.1`，
于是 `prepare_weight` 每次算出 `(主重量-0.1) + 0.1 = 主重量`，
增量被抵消了 —— 结果稳定但比新商品少 0.1。

新商品（首次处理）的行为是：`主重量 + 0.1`。这里对齐它。

基准 = 真正上游原重 = 主重量（主重量有 `_sourceWeight` 保护，一直没被推高）。
"""
import json
import os
import warnings
from copy import deepcopy

import psycopg

warnings.filterwarnings("ignore")

INCREMENT = 0.1

u = os.environ["AOHUANG_DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
with psycopg.connect(u) as conn:
    rows = conn.execute("""
        select id, detail_id, payload
        from products
        where deleted_at is null
          and payload->'_sourceWeights' is not null
    """).fetchall()

    fixed = 0
    for pid, did, payload in rows:
        try:
            main = float(payload.get("weight") or 0)
        except (TypeError, ValueError):
            continue
        saved = payload.get("_sourceWeights") or {}
        base = saved.get("weight")
        try:
            base = float(base) if base not in (None, "") else None
        except (TypeError, ValueError):
            base = None
        if base is None or abs(base - main) < 1e-9:
            continue

        new = deepcopy(payload)
        new["_sourceWeights"] = {k: round(main, 6) for k in saved}
        conn.execute(
            "update products set payload = %s, revision = revision + 1, "
            "draft_verified_revision = case when draft_verified_revision "
            "is not null then revision + 1 else null end where id = %s",
            (json.dumps(new, ensure_ascii=False), pid))
        print("%-13s 主=%s  基准 %s -> %s" % (did, main, base, round(main, 6)))
        fixed += 1

    conn.commit()
    print(f"\n已修正 {fixed} 件")