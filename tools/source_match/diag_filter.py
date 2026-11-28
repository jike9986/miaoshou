"""105 件 automation_result=true 里，到底哪一条条件把它们全筛掉了。"""
import os

import psycopg

url = os.environ["AOHUANG_DATABASE_URL"].replace(
    "postgresql+psycopg://", "postgresql://")

CHECKS = [
    ("automation_result=true", """
        select count(*) from products
        where deleted_at is null and automation_result = true
    """),
    ("+有 run item 记录", """
        select count(*) from products p
        where p.deleted_at is null and p.automation_result = true
          and exists (select 1 from automation_run_items i
                      where i.detail_id = p.detail_id)
    """),
    ("+process_status=succeeded", """
        select count(*) from products p
        where p.deleted_at is null and p.automation_result = true
          and exists (select 1 from automation_run_items i
                      where i.detail_id = p.detail_id
                        and i.process_status = 'succeeded')
    """),
    ("+draft_verified==revision", """
        select count(*) from products p
        where p.deleted_at is null and p.automation_result = true
          and p.draft_verified_revision = p.revision
    """),
    ("+无未ready图片任务", """
        select count(*) from products p
        where p.deleted_at is null and p.automation_result = true
          and not exists (select 1 from image_jobs j
                          where j.product_id = p.id and j.status != 'ready')
    """),
    ("+run.account==prod.account", """
        select count(*) from products p
        where p.deleted_at is null and p.automation_result = true
          and exists (select 1 from automation_run_items i
                      join automation_runs r on r.id = i.run_id
                      where i.detail_id = p.detail_id
                        and r.account_id = p.account_id)
    """),
    ("+item.shop==prod.shop", """
        select count(*) from products p
        where p.deleted_at is null and p.automation_result = true
          and exists (select 1 from automation_run_items i
                      where i.detail_id = p.detail_id
                        and i.source_shop_id = p.shop_id)
    """),
]

with psycopg.connect(url) as conn:
    print("=" * 74)
    print("逐条累加，看哪一步归零")
    print("=" * 74)
    for label, sql in CHECKS:
        n = conn.execute(sql).fetchone()[0]
        print(f"  {label:<30} {n:>4} 件")
        if n == 0:
            print(f"    ↑↑↑ 从这里归零")

    print()
    print("=" * 74)
    print("run items 实际状态分布")
    print("=" * 74)
    for row in conn.execute("""
        select process_status, count(*) from automation_run_items
        group by process_status order by count(*) desc
    """).fetchall():
        print(f"  {row[0]:<16} {row[1]}")

    print()
    print("=" * 74)
    print("automation_run_items 有哪些列")
    print("=" * 74)
    cols = conn.execute("""
        select column_name from information_schema.columns
        where table_name = 'automation_run_items' order by ordinal_position
    """).fetchall()
    print("  " + ", ".join(r[0] for r in cols))

    print()
    print("=" * 74)
    print("item 与 product 能不能对上（按 detail_id）")
    print("=" * 74)
    r = conn.execute("""
        select count(*) from automation_run_items i
        where not exists (select 1 from products p
                          where p.deleted_at is null and p.detail_id = i.detail_id)
    """).fetchone()[0]
    print(f"  run items 里找不到对应商品的: {r} 条")

    r2 = conn.execute("""
        select count(*) from products p
        where p.deleted_at is null and p.automation_result = true
          and not exists (select 1 from automation_run_items i
                          where i.detail_id = p.detail_id)
    """).fetchone()[0]
    print(f"  automation_result=true 但没有 run item 的: {r2} 件")

    print()
    print("=" * 74)
    print("product_id 关联是否为空")
    print("=" * 74)
    r3 = conn.execute("""
        select count(*) from automation_run_items where product_id is null
    """).fetchone()[0]
    r4 = conn.execute("select count(*) from automation_run_items").fetchone()[0]
    print(f"  process_status=succeeded 但 product_id 为空的: {r3} / {r4}")