"""第 3 个问题：已完成的商品，处理别的商品后完成标志又消失了。

排查方向：
  ① 采集箱列表里「已完成」标志是从什么数据算出来的？
  ② 处理别的商品时，会不会把已完成的记录改掉？
"""
import os

import psycopg

url = os.environ["AOHUANG_DATABASE_URL"].replace(
    "postgresql+psycopg://", "postgresql://")

with psycopg.connect(url) as conn:
    print("=" * 76)
    print("① products 里今天处理的商品，automation_result / revision 状态")
    print("=" * 76)
    rows = conn.execute("""
        select id, detail_id, account_id, shop_id, automation_result,
               revision, draft_verified_revision, status, operator_id,
               to_char(to_timestamp(updated_at) at time zone 'Asia/Shanghai',
                       'MM-DD HH24:MI:SS')
        from products
        where deleted_at is null
          and updated_at > extract(epoch from now()) - 86400
        order by updated_at desc
        limit 40
    """).fetchall()
    print(f"  今天更新过 {len(rows)} 件")
    ok = sum(1 for r in rows if r[4] and r[5] == r[6])
    print(f"  其中 auto=true 且 verified==revision: {ok} 件")

    print()
    print("=" * 76)
    print("② 采集箱里「已完成」是靠什么判断的 —— 看 collect_box_service")
    print("=" * 76)
    # 直接查哪些 detail_id 有 products 记录（那才是「已处理」的物理依据）
    box_ids = conn.execute("""
        select detail_id, count(*) from products
        where deleted_at is null and account_id = 12
        group by 1 having count(*) > 0
        order by 1
    """).fetchall()
    print(f"  账号 12 在库商品 {len(box_ids)} 个 detailId")

    print()
    print("=" * 76)
    print("③ 同一 detail_id 出现多行（重复处理会覆盖还是新增）")
    print("=" * 76)
    dup = conn.execute("""
        select detail_id, count(*) from products
        where deleted_at is null
        group by 1 having count(*) > 2
        order by 2 desc limit 10
    """).fetchall()
    for d, n in dup:
        print(f"  {d}: {n} 行")

    print()
    print("=" * 76)
    print("④ draft_verified_revision 为 NULL 的（会显示未完成）")
    print("=" * 76)
    n = conn.execute("""
        select count(*) from products
        where deleted_at is null and automation_result = true
          and draft_verified_revision is null
    """).fetchone()[0]
    print(f"  automation_result=true 但 verified 为 NULL: {n} 件")
    for r in conn.execute("""
        select id, detail_id, revision, automation_images->>'failed',
               automation_images->>'reason',
               to_char(to_timestamp(updated_at) at time zone 'Asia/Shanghai',
                       'MM-DD HH24:MI:SS')
        from products
        where deleted_at is null and automation_result = true
          and draft_verified_revision is null
        order by updated_at desc limit 10
    """).fetchall():
        print(f"  #{r[0]:<4} {r[1]:<12} rev={r[2]} failed={r[3]} {r[5]}")
        if r[4]:
            print(f"        {str(r[4])[:90]}")