#!/bin/bash
# 把图搜核对结果写回生产库的货源链接。
# 只处理清单里给的 id → offerId，逐个 UPDATE，最后回读确认。
set -a
. /etc/aohuang-listing-v2.env
set +a
export PYTHONPATH=/opt/miaoshou-listing-v2/server
/opt/miaoshou-listing-v2/venv/bin/python <<'PY'
import json
import warnings

warnings.filterwarnings("ignore")
from sqlalchemy import text

from db.session import SessionLocal

todo = json.load(open("/tmp/to_write.json", encoding="utf-8"))
print(f"待写 {len(todo)} 件")

db = SessionLocal()
ok = fail = 0
missing = []
try:
    for row in todo:
        pid = int(row["id"])
        oid = str(row["oid"])
        url = f"https://detail.1688.com/offer/{oid}.html"
        exists = db.execute(
            text("select 1 from products where id = :p"), {"p": pid}
        ).fetchone()
        if not exists:
            missing.append(pid)
            continue
        db.execute(
            text(
                """
                update products
                set payload = (coalesce(payload::jsonb, '{}'::jsonb)
                               || jsonb_build_object(
                                    'sourceUrl', cast(:src_url as text),
                                    'sourceItemId', cast(:src_oid as text),
                                    'sourceMatchedBy', 'image-search'))::json,
                    revision = coalesce(revision, 0) + 1,
                    updated_at = extract(epoch from now())::double precision
                where id = :pid
                """
            ),
            {"pid": pid, "src_url": url, "src_oid": oid},
        )
        # 规则 §4：自动化结果商品必须同步 draft_verified_revision
        db.execute(
            text(
                """
                update products set draft_verified_revision = revision
                where id = :pid and automation_result is true
                  and draft_verified_revision is not null
                """
            ),
            {"pid": pid},
        )
        ok += 1
    db.commit()

    ids = [int(r["id"]) for r in todo]
    back = db.execute(
        text(
            """
            select count(*) from products
            where id = any(:ids) and coalesce(payload->>'sourceUrl','') <> ''
            """
        ),
        {"ids": ids},
    ).scalar()
    total = db.execute(
        text(
            """
            select count(*) from products
            where deleted_at is null and automation_result is false
              and coalesce(payload->>'sourceUrl','') <> ''
            """
        )
    ).scalar()
    still = db.execute(
        text(
            """
            select count(*) from products
            where deleted_at is null and automation_result is false
              and coalesce(payload->>'sourceUrl','') = ''
              and coalesce(group_name,'') <> all(array['精品','自营海外仓','海外仓定制款'])
            """
        )
    ).scalar()
    print(f"写入 {ok} / 目标 {len(todo)} / 缺 id {len(missing)}")
    print(f"回读：这批里有货源 {back} / {len(ids)}")
    print(f"全局：有货源 {total} 件，仍缺（可见分组）{still} 件")
finally:
    db.close()
PY
