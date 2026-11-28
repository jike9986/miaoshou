import os
import warnings

import psycopg

warnings.filterwarnings("ignore")

u = os.environ["AOHUANG_DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
with psycopg.connect(u) as conn:
    rows = conn.execute("""
        select detail_id, automation_images
        from products
        where deleted_at is null and automation_images->>'running' = 'true'
        order by updated_at desc
    """).fetchall()
    print("running=True 的商品 %d 件：" % len(rows))
    for did, ai in rows:
        print("  %-13s queued=%-5s completed=%-5s runningAt=%s" % (
            did, ai.get("queued"), ai.get("completed"),
            ai.get("runningAt")))