"""用库里已有的商品验证：重量上限 2.0 是否生效、失败原因分布。"""
import warnings

import psycopg

warnings.filterwarnings("ignore")

import os

u = os.environ["AOHUANG_DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
with psycopg.connect(u) as conn:
    print("=== 上游原重分布（payload->>'weight'）===")
    rows = conn.execute("""
        select
          case
            when (payload->>'weight')::float < 1.5 then 'a. <1.5kg'
            when (payload->>'weight')::float < 2.0 then 'b. 1.5-2.0kg'
            when (payload->>'weight')::float < 3.0 then 'c. 2.0-3.0kg'
            else 'd. >=3.0kg'
          end as bucket,
          count(*)
        from products
        where deleted_at is null
          and payload->>'weight' is not null
          and (payload->>'weight') ~ '^[0-9.]+$'
        group by 1 order by 1
    """).fetchall()
    for r in rows:
        print("  %-14s %d 件" % r)

    print()
    print("=== 1.5-2.0kg 之间的商品（原来会被拦，现在能过）===")
    rows = conn.execute("""
        select detail_id, payload->>'weight', left(title, 30)
        from products
        where deleted_at is null
          and payload->>'weight' is not null
          and (payload->>'weight') ~ '^[0-9.]+$'
          and (payload->>'weight')::float >= 1.5
          and (payload->>'weight')::float < 2.0
        order by (payload->>'weight')::float desc limit 15
    """).fetchall()
    for r in rows:
        print("  %-13s %-6s %s" % (r[0], r[1], r[2]))

    print()
    print("=== 超 2.0kg 仍会被拦的 ===")
    n = conn.execute("""
        select count(*) from products
        where deleted_at is null
          and payload->>'weight' is not null
          and (payload->>'weight') ~ '^[0-9.]+$'
          and (payload->>'weight')::float >= 2.0
    """).fetchone()[0]
    print("  %d 件" % n)