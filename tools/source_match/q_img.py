import os
import warnings

import psycopg

warnings.filterwarnings("ignore")

u = os.environ["AOHUANG_DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
with psycopg.connect(u) as conn:
    rows = conn.execute("""
        select detail_id, automation_images
        from products
        where deleted_at is null and automation_images is not null
          and updated_at > extract(epoch from now()) - 3600
        order by updated_at desc limit 25
    """).fetchall()
    print(f"有图片任务的商品: {len(rows)}")
    q = c = other = 0
    for did, ai in rows:
        ai = ai or {}
        if ai.get("queued"):
            st = "queued"
            q += 1
        elif ai.get("completed"):
            st = "done"
            c += 1
        else:
            st = "other"
            other += 1
        print("  %-13s %-8s req=%s sub=%s fail=%s running=%s reason=%s" % (
            did, st, ai.get("requested"), ai.get("submitted"), ai.get("failed"),
            ai.get("running"), str(ai.get("reason") or "")[:30]))
    print()
    print("汇总: queued=%d completed=%d other=%d" % (q, c, other))
