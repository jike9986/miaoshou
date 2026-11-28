"""这些 automation_result=true 但没有 run item 的商品，是谁、什么时候造的。

products 没有 created_at，只有 updated_at（epoch double）。用 updated_at 分日。
判断依据：如果时间集中在几次批处理脚本运行的时间窗，就是脚本跳过了
第①②④步（建批次/开始/回写），属于数据缺口，不是代码 bug。
"""
import os
from datetime import datetime, timezone

import psycopg

url = os.environ["AOHUANG_DATABASE_URL"].replace(
    "postgresql+psycopg://", "postgresql://")


def ts(v):
    if v is None:
        return None
    return datetime.fromtimestamp(float(v), timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


MISSING = """
    p.deleted_at is null and p.automation_result = true
    and not exists (select 1 from automation_run_items i where i.detail_id = p.detail_id)
"""

with psycopg.connect(url) as conn:
    print("=" * 78)
    print("① 无 run item 的商品：按 updated_at 日期分布")
    print("=" * 78)
    for d, n in conn.execute(f"""
        select to_char(to_timestamp(p.updated_at) at time zone 'Asia/Shanghai', 'YYYY-MM-DD') as d,
               count(*)
        from products p
        where {MISSING}
        group by 1 order by 1
    """).fetchall():
        print(f"  {d}  {n:>4} 件")

    print()
    print("=" * 78)
    print("② 按操作员")
    print("=" * 78)
    for oid, n in conn.execute(f"""
        select p.operator_id, count(*) from products p
        where {MISSING} group by 1 order by 2 desc
    """).fetchall():
        print(f"  operator_id={oid}  {n:>4} 件")

    print()
    print("=" * 78)
    print("③ 按账号/店铺")
    print("=" * 78)
    for acc, shop, n in conn.execute(f"""
        select p.account_id, p.shop_id, count(*) from products p
        where {MISSING} group by 1,2 order by 3 desc
    """).fetchall():
        print(f"  account={acc} shop={shop}  {n:>4} 件")

    print()
    print("=" * 78)
    print("④ automation_runs 全部批次")
    print("=" * 78)
    for rid, acc, shop, st, ca, oid, n in conn.execute("""
        select r.id, r.account_id, r.shop_id, r.status, r.created_at,
               r.owner_operator_id,
               (select count(*) from automation_run_items i where i.run_id = r.id)
        from automation_runs r order by r.created_at
    """).fetchall():
        print(f"  run={rid} acc={acc} shop={shop} {st:<10} {ts(ca)} op={oid} items={n}")

    print()
    print("=" * 78)
    print("⑤ succeeded 的 run item 对应的商品还在不在")
    print("=" * 78)
    rows = conn.execute("""
        select i.id, i.run_id, i.detail_id, i.product_id, i.source_shop_id,
               p.id is not null as has_product,
               p.draft_verified_revision, p.revision, p.automation_result,
               p.deleted_at
        from automation_run_items i
        left join products p on p.id = i.product_id
        where i.process_status = 'succeeded'
        order by i.id
    """).fetchall()
    print(f"  {'item':>5} {'run':>6} {'detail':<12} {'pid':>5} {'prod?':<6} "
          f"{'verified':>9} {'rev':>4} {'auto':<6} deleted")
    for (iid, rid, did, pid, shop, hasp, ver, rev, auto, del_at) in rows:
        print(f"  {iid:>5} {str(rid):>6} {str(did):<12} {str(pid):>5} {str(hasp):<6} "
              f"{str(ver):>9} {str(rev):>4} {str(auto):<6} {del_at}")

    print()
    print("=" * 78)
    print("⑥ 关键交叉：有没有商品「有 succeeded item 但不满足其它条件」")
    print("=" * 78)
    for (n,) in conn.execute("""
        select count(*) from products p
        where p.deleted_at is null and p.automation_result = true
          and exists (select 1 from automation_run_items i where i.detail_id = p.detail_id)
          and not exists (
            select 1 from automation_run_items i
            join automation_runs r on r.id = i.run_id
            where i.detail_id = p.detail_id
              and i.process_status = 'succeeded'
              and r.account_id = p.account_id
              and i.source_shop_id = p.shop_id
              and p.draft_verified_revision = p.revision
          )
    """).fetchall():
        print(f"  有 item 但无一成立: {n} 件")
