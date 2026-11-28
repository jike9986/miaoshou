"""复位被推高的 SKU 重量：把每个 SKU 的重量改回主重量 + 0.1。

## 依据

`prepare_weight()` 给重量加增量时，主重量和每个 SKU 各加一次 `increment`，
所以正常状态下 **SKU 重量 == 主重量**（同一个基准 +0.1，主重量那边写的是
「主重量+0.1」，SKU 写的也是「SKU基准+0.1」，而 SKU 基准本来就等于主重量）。

被推高的商品：主重量有 `_sourceWeight` 保护，一直没涨；SKU 没有保护，
每次重跑 +0.1，于是 `SKU = 主重量 + (N-1)*0.1`。

实测 25 件全部符合这个规律（1.2 → 1.5/1.6/1.7/1.8/1.89/1.98，
分别对应 3/4/5/6/7/8 次）。

## 复位规则

对每件：
  · 取当前主重量 `payload.weight`（它是对的，有 _sourceWeight 保护）；
  · 把所有 SKU 的 weight 设成 `主重量`；
  · 写 `_sourceWeights['weight'] = 主重量 - increment`、
    `_sourceWeights['sku:{key}:weight'] = 主重量 - increment`，
    这样首次重跑会算成「主重量 - 0.1 + 0.1 = 主重量」，不动。
  · 记 `weightRepaired`，便于审计。

**只改本地库**，妙手上的脏重量要等下次处理时随草稿写回一起覆盖。
"""
import argparse
import json
import os
import warnings
from copy import deepcopy

import psycopg

warnings.filterwarnings("ignore")

INCREMENT = 0.1


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的写库（默认只试跑）")
    ap.add_argument("--min-diff", type=float, default=0.25, help="只修差多少以上的")
    args = ap.parse_args()

    u = os.environ["AOHUANG_DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(u) as conn:
        rows = conn.execute("""
            select id, detail_id, payload, revision, draft_verified_revision
            from products
            where deleted_at is null
              and payload->>'weight' is not null
              and payload->'skuMap' is not null
        """).fetchall()

        targets = []
        skipped_mixed: list[tuple] = []
        for pid, did, payload, rev, verified in rows:
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
            if not weights or max(weights) - main <= args.min_diff:
                continue
            # 只有「所有 SKU 重量完全相同、且都比主重量高」才是被推高的脏数据。
            #
            # 上游本来就有按尺码差异化重量的商品（实测 3533606391 有 72 种取值，
            # 3533601800/3533603400 阶梯递增），那些是真实数据，不能压平。
            # 被推高的特征是：每次重跑给每个 SKU 加同一个增量，所以它们的值
            # 仍然完全相同 —— 只是整体高出一截。
            if max(weights) - min(weights) > 0.001:
                skipped_mixed.append((did, main, min(weights), max(weights), len(weights)))
                continue
            targets.append((pid, did, payload, rev, verified, main))

        print(f"待修 {len(targets)} 件  (apply={args.apply})\n")
        fixed = 0
        for pid, did, payload, rev, verified, main in targets:
            old = payload
            new = deepcopy(payload)
            base = round(main - INCREMENT, 6)
            source_weights = {"weight": base}
            for key, sku in (new.get("skuMap") or {}).items():
                if not isinstance(sku, dict):
                    continue
                prev = sku.get("weight")
                sku["weight"] = main
                source_weights[f"sku:{key}:weight"] = base
            new["_sourceWeights"] = source_weights
            new["weightRepaired"] = {"from": old.get("weight"), "to": main,
                                     "reason": "sku_weight_drift"}
            sample_key = next(iter(new.get("skuMap") or {}), "")
            print("%-13s 主=%s  %s -> %s  (%d 个SKU)" % (
                did, main,
                (old.get("skuMap") or {}).get(sample_key, {}).get("weight"),
                (new.get("skuMap") or {}).get(sample_key, {}).get("weight"),
                len(new.get("skuMap") or {})))
            if args.apply:
                conn.execute(
                    "update products set payload = %s, revision = revision + 1, "
                    "draft_verified_revision = case when draft_verified_revision "
                    "is not null then revision + 1 else null end where id = %s",
                    (json.dumps(new, ensure_ascii=False), pid))
            fixed += 1

        if skipped_mixed:
            print()
            print(f"跳过 {len(skipped_mixed)} 件（SKU 之间重量本来就不同，是真实尺码差异）：")
            for did, main, mn, mx, n in skipped_mixed:
                print("  %-13s 主=%s  SKU范围=%s~%s (%d个)" % (did, main, mn, mx, n))

        if args.apply:
            conn.commit()
            print(f"\n已提交 {fixed} 件")
        else:
            print(f"\n试跑完成（未写库）。加 --apply 才真的改。")


if __name__ == "__main__":
    main()