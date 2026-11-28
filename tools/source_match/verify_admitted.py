"""确认：刚才补录的 78 件是否已全部正常进入成品仓库。

用户说「我加入一些东西进入仓库了」，所以 automation_result 由 true 变 false
是「加入成品仓库」的正常结果（admit_automation_results 里 product.automation_result = False）。
这里核对：
  · 已入仓的 291 件里，哪些是刚补录的 78 件
  · 它们是否在成品仓库列表里可见（active_query + automation_result=False）
  · 剩余 27 件 automation_result=true 的是不是 B/D 桶
"""
import os

import psycopg

url = os.environ["AOHUANG_DATABASE_URL"].replace(
    "postgresql+psycopg://", "postgresql://")

with psycopg.connect(url) as conn:
    print("=" * 72)
    print("刚才补录的 3 个批次里的商品，现在什么状态")
    print("=" * 72)
    rows = conn.execute("""
        select r.id, p.id, p.detail_id, p.account_id, p.shop_id,
               p.automation_result, p.revision, p.draft_verified_revision,
               i.process_status, p.status
        from automation_run_items i
        join automation_runs r on r.id = i.run_id
        join products p on p.id = i.product_id
        where r.id like 'arun_efc26f93%'
           or r.id like 'arun_cbb0f6fa%'
           or r.id like 'arun_89ab2c1e%'
        order by p.account_id, p.id
    """).fetchall()
    print(f"  共 {len(rows)} 件")
    stats = {}
    for (rid, pid, did, acc, shop, auto, rev, ver, st, pstatus) in rows:
        key = (auto, st)
        stats[key] = stats.get(key, 0) + 1
    for (auto, st), n in sorted(stats.items(), key=lambda x: -x[1]):
        print(f"    automation_result={auto} process_status={st}: {n} 件")

    print()
    print("=" * 72)
    print("成品仓库列表可见性（active_query: 未删除 + automation_result=false）")
    print("=" * 72)
    n = conn.execute("""
        select count(*) from products
        where deleted_at is null and automation_result = false
    """).fetchone()[0]
    print(f"  成品仓库可见: {n} 件")

    print()
    print("  补录那 78 件在里面的:")
    n2 = conn.execute("""
        select count(*) from automation_run_items i
        join automation_runs r on r.id = i.run_id
        join products p on p.id = i.product_id
        where (r.id like 'arun_efc26f93%' or r.id like 'arun_cbb0f6fa%'
               or r.id like 'arun_89ab2c1e%')
          and p.deleted_at is null and p.automation_result = false
    """).fetchone()[0]
    print(f"    {n2} 件")

    print()
    print("=" * 72)
    print("剩余 automation_result=true 的 27 件，归类")
    print("=" * 72)
    left = conn.execute("""
        select p.id, p.detail_id, p.account_id, p.shop_id,
               p.revision, p.draft_verified_revision,
               (select string_agg(distinct i.process_status, ',')
                from automation_run_items i where i.detail_id = p.detail_id)
                 as item_status,
               coalesce(p.automation_images->>'reason', '') as img_reason
        from products p
        where p.deleted_at is null and p.automation_result = true
        order by p.account_id, p.id
    """).fetchall()
    for r in left:
        pid, did, acc, shop, rev, ver, st, reason = r
        if st is None:
            kind = "B 无 run item + 核验失败"
        elif "succeeded" in (st or ""):
            kind = "有成功 item 但仍不可见"
        else:
            kind = f"D item 状态={st}"
        print(f"  #{pid:<5} {did:<12} acc={acc} rev={rev} ver={ver} {kind}")
        if reason:
            print(f"       图片原因: {reason[:70]}")