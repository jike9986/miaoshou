"""把「auto=true 但无 run item」的 94 件按核验状态拆分。

backfill_runs 走 API 补记录，而 finish_process 有一道门禁：

    product.draft_verified_revision != product.revision  →  409
    「商品没有与当前版本匹配的妙手草稿读回核验」

（automation_run_service.py:563-568；images_in_progress 为真时放宽，
但这些商品没有 automation_images，走的是严格分支。）

所以必须先把这批按 verified 状态分开：只有 verified == revision 的能直接补，
verified 为 NULL 或落后的要先按 CLAUDE.md 规则 4 同步。
"""
import os

import psycopg

url = os.environ["AOHUANG_DATABASE_URL"].replace(
    "postgresql+psycopg://", "postgresql://")

MISSING = """
    p.deleted_at is null and p.automation_result = true
    and not exists (select 1 from automation_run_items i where i.detail_id = p.detail_id)
"""

with psycopg.connect(url) as conn:
    rows = conn.execute(f"""
        select p.id, p.detail_id, p.shop_id, p.account_id,
               p.revision, p.draft_verified_revision,
               p.images is not null as has_images,
               p.automation_images is not null as has_auto_images
        from products p
        where {MISSING}
        order by p.account_id, p.shop_id, p.id
    """).fetchall()

    ok, null_verified, stale_verified = [], [], []
    for pid, did, shop, acc, rev, ver, himg, hauto in rows:
        if ver is None:
            null_verified.append((pid, did, shop, acc, rev, ver))
        elif ver != rev:
            stale_verified.append((pid, did, shop, acc, rev, ver))
        else:
            ok.append((pid, did, shop, acc, rev, ver))

    print("=" * 78)
    print(f"auto=true 且无 run item 合计: {len(rows)} 件")
    print("=" * 78)
    print(f"  ✓ verified == revision（可直接补 run 记录）  {len(ok):>3} 件")
    print(f"  ✗ verified is NULL（要先同步）              {len(null_verified):>3} 件")
    print(f"  ✗ verified != revision（要先同步）          {len(stale_verified):>3} 件")

    def dump(title, items):
        print()
        print("=" * 78)
        print(title)
        print("=" * 78)
        by = {}
        for pid, did, shop, acc, rev, ver in items:
            by.setdefault((acc, shop), []).append((pid, did, rev, ver))
        for (acc, shop), lst in sorted(by.items()):
            print(f"  account={acc} shop={shop}: {len(lst)} 件")
        print()
        for pid, did, shop, acc, rev, ver in items:
            print(f"    #{pid:<5} detail={did:<12} acc={acc} shop={shop} "
                  f"rev={rev} verified={ver}")

    dump("✓ 可直接补 run 记录", ok)
    if null_verified:
        dump("✗ verified is NULL —— 需先 draft_verified_revision = revision",
             null_verified)
    if stale_verified:
        dump("✗ verified != revision —— 需先 draft_verified_revision = revision",
             stale_verified)

    # 图片任务门禁（条件③）
    print()
    print("=" * 78)
    print("图片任务门禁（③ status != ready 会挡住）")
    print("=" * 78)
    bad = conn.execute("""
        select count(distinct p.id)
        from products p
        join image_jobs j on j.product_id = p.id
        where p.deleted_at is null and p.automation_result = true
          and j.status != 'ready'
          and not exists (select 1 from automation_run_items i
                          where i.detail_id = p.detail_id)
    """).fetchone()[0]
    print(f"  无 run item 且有未 ready 图片任务的商品: {bad} 件")
