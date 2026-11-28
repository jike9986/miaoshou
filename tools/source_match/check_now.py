"""核对当前状态：自动化结果可见数 / 已入仓数 / 最近改动。"""
import os

import psycopg

url = os.environ["AOHUANG_DATABASE_URL"].replace(
    "postgresql+psycopg://", "postgresql://")

COND = """
  exists (
    select 1 from automation_run_items i
    join automation_runs r on r.id = i.run_id
    where i.process_status = 'succeeded' and i.product_id = p.id
      and r.account_id = p.account_id and i.detail_id = p.detail_id
      and i.source_shop_id = p.shop_id
      and p.draft_verified_revision = p.revision
      and not exists (select 1 from image_jobs j
                      where j.product_id = p.id and j.status <> 'ready'))
"""

with psycopg.connect(url) as conn:
    visible = conn.execute(f"""
        select count(*) from products p
        where p.deleted_at is null and p.automation_result = true and {COND}
    """).fetchone()[0]
    print("=" * 70)
    print(f"自动化结果页可见: {visible} 件")
    print("=" * 70)
    for acc, shop, n in conn.execute(f"""
        select p.account_id, p.shop_id, count(*) from products p
        where p.deleted_at is null and p.automation_result = true and {COND}
        group by 1,2 order by 3 desc
    """).fetchall():
        print(f"  acc={acc} shop={shop}: {n} 件")

    print()
    total_auto = conn.execute("""
        select count(*) from products
        where deleted_at is null and automation_result = true
    """).fetchone()[0]
    admitted = conn.execute("""
        select count(*) from products
        where deleted_at is null and automation_result = false
    """).fetchone()[0]
    print(f"automation_result=true 合计: {total_auto} 件"
          f"（可见 {visible} / 不可见 {total_auto - visible}）")
    print(f"已入成品仓库(automation_result=false): {admitted} 件")

    print()
    print("=" * 70)
    print("最近 30 分钟有变动的商品")
    print("=" * 70)
    rows = conn.execute("""
        select id, detail_id, account_id, shop_id, automation_result,
               revision, draft_verified_revision, status,
               to_char(to_timestamp(updated_at) at time zone 'Asia/Shanghai',
                       'MM-DD HH24:MI:SS')
        from products
        where deleted_at is null
          and updated_at > extract(epoch from now()) - 1800
        order by updated_at desc limit 20
    """).fetchall()
    if not rows:
        print("  （无）")
    for r in rows:
        ok = "✓可见" if r[4] and r[5] == r[6] else ""
        print(f"  #{r[0]:<5} {r[1]:<12} acc={r[2]} shop={r[3]} auto={r[4]} "
              f"rev={r[5]} ver={r[6]} {r[7]} {r[8]} {ok}")

    print()
    print("=" * 70)
    print("已入仓的（automation_result=false）最近 10 件")
    print("=" * 70)
    for r in conn.execute("""
        select id, detail_id, account_id, shop_id, status,
               to_char(to_timestamp(updated_at) at time zone 'Asia/Shanghai',
                       'MM-DD HH24:MI:SS')
        from products
        where deleted_at is null and automation_result = false
        order by updated_at desc limit 10
    """).fetchall():
        print(f"  #{r[0]:<5} {r[1]:<12} acc={r[2]} shop={r[3]} {r[4]} {r[5]}")
