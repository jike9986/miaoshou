"""查那 8 件「有 run item + auto=true」的，succeeded 为什么是 0。"""
import os

import psycopg

url = os.environ["AOHUANG_DATABASE_URL"].replace(
    "postgresql+psycopg://", "postgresql://")

with psycopg.connect(url) as conn:
    rows = conn.execute("""
        select p.id, p.detail_id, p.shop_id, p.account_id,
               p.revision, p.draft_verified_revision,
               i.process_status, i.product_id, i.source_shop_id,
               r.account_id as run_acc, i.process_reason
        from products p
        join automation_run_items i on i.detail_id = p.detail_id
        join automation_runs r on r.id = i.run_id
        where p.deleted_at is null and p.automation_result = true
        order by p.id desc
    """).fetchall()

    print(f"有 run item 且 auto=true: {len(rows)} 条")
    print()
    for r in rows:
        (pid, did, shop, acc, rev, ver, st, ipid, ishop, racc,
         reason) = r
        problems = []
        if st != "succeeded":
            problems.append(f"process_status={st}")
        if ipid is None:
            problems.append("item.product_id 为空")
        elif ipid != pid:
            problems.append(f"item.product_id={ipid} != {pid}")
        if ver != rev:
            problems.append(f"verified {ver} != revision {rev}")
        if racc != acc:
            problems.append(f"run.account={racc} != {acc}")
        if str(ishop) != str(shop):
            problems.append(f"item.shop={ishop} != {shop}")
        mark = "✓ 应显示" if not problems else "✗"
        print(f"  {mark} #{pid} {did}  status={st}")
        if problems:
            for p in problems:
                print(f"        {p}")
            if reason:
                print(f"        reason={str(reason)[:100]}")

    print()
    print("=" * 72)
    print("97 件没有 run item 的商品，最新 5 件")
    print("=" * 72)
    for r in conn.execute("""
        select p.id, p.detail_id, p.revision, p.draft_verified_revision
        from products p
        where p.deleted_at is null and p.automation_result = true
          and not exists (select 1 from automation_run_items i
                          where i.detail_id = p.detail_id)
        order by p.id desc limit 5
    """).fetchall():
        print(f"  #{r[0]} {r[1]} rev={r[2]} verified={r[3]}")