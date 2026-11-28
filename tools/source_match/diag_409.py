"""查补录失败的真实原因：把 finish_process 的每个判断单独算一遍。"""
import os

import psycopg

url = os.environ["AOHUANG_DATABASE_URL"].replace(
    "postgresql+psycopg://", "postgresql://")

PIDS = [359, 374, 376]

with psycopg.connect(url) as conn:
    for pid in PIDS:
        p = conn.execute("""
            select id, detail_id, shop_id, account_id, revision,
                   draft_verified_revision, updated_at,
                   automation_result,
                   coalesce(automation_images::text,'{}')
            from products where id = %s
        """, (pid,)).fetchone()
        print("=" * 72)
        print(f"#{p[0]} {p[1]}")
        print("=" * 72)
        print(f"  account={p[3]} shop={p[2]} rev={p[4]} verified={p[5]}")
        print(f"  automation_result={p[7]}")
        print(f"  product.updated_at={p[6]}")
        print(f"  automation_images={p[8][:120]}")

        items = conn.execute("""
            select id, process_status, product_id, updated_at, source_shop_id,
                   detail_id
            from automation_run_items
            where detail_id = %s order by updated_at desc
        """, (p[1],)).fetchall()
        print(f"  run items: {len(items)} 条")
        for it in items[:3]:
            print(f"    id={it[0]} status={it[1]} product_id={it[2]}")
            print(f"      item.updated_at={it[3]}")
            print(f"      item.detail={it[5]} shop={it[4]}")
            print(f"      product.updated_at < item.updated_at ? "
                  f"{p[6] < it[3]}  ← True 就是被这条拦住的")

        # 严格条件逐一
        print(f"  ① product 存在: True")
        print(f"  ② updated_at 顺序: product={p[6]} item={items[0][3] if items else 'n/a'}")
        print(f"  ③ verified == revision: {p[5]} vs {p[4]} "
              f"→ {p[5] == p[4]}")
        print()
