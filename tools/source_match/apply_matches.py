# -*- coding: utf-8 -*-
"""把核对结果写回生产库的货源链接。

安全设计：
  · 只写 verdict=auto 的（pHash ≤10 且直方图 ≤0.45，肉眼抽验过是同款）
  · review / none 一律不碰，留给人处理
  · 默认 dry-run，必须显式 --write 才落库
  · 写前打印清单让你过目；写完回读校验

用法：
    # 看会写哪些
    python tools/source_match/apply_matches.py

    # 真写
    python tools/source_match/apply_matches.py --write
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = (HERE / "../../server").resolve()
VERIFIED = HERE / ".verified.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verified", default=str(VERIFIED))
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--include-review", action="store_true",
                    help="把 review 一档也写进去（不推荐）")
    args = ap.parse_args()

    data = json.loads(Path(args.verified).read_text(encoding="utf-8"))
    wanted = {"auto"} | ({"review"} if args.include_review else set())
    todo = [r for r in data if r.get("verdict") in wanted and r.get("chosen")]
    print(f"核对结果 {len(data)} 件，其中可写 {len(todo)} 件 "
          f"（{'auto+review' if args.include_review else '仅 auto'}）")
    if not todo:
        return 0

    for r in todo:
        print(f"  #{r['id']:>4} -> {r['chosen']}  {str(r['title'])[:40]}")

    if not args.write:
        print("\n（dry-run，未写库。加 --write 落库）")
        return 0

    sys.path.insert(0, str(SERVER))
    import warnings

    warnings.filterwarnings("ignore")
    from sqlalchemy import text

    from db.session import SessionLocal

    db = SessionLocal()
    ok = fail = 0
    try:
        for r in todo:
            oid = str(r["chosen"])
            url = f"https://detail.1688.com/offer/{oid}.html"
            try:
                db.execute(text("""
                    update products
                    set payload = jsonb_set(
                          jsonb_set(
                            jsonb_set(coalesce(payload,'{}'::jsonb),
                                      '{sourceUrl}', to_jsonb(:url::text), true),
                            '{sourceItemId}', to_jsonb(:oid::text), true),
                          '{sourceMatchedBy}', to_jsonb('image-search'::text), true),
                        revision = coalesce(revision, 0) + 1,
                        updated_at = now()
                    where id = :pid
                """), {"pid": r["id"], "url": url, "oid": oid})
                # 规则 §4：自动化结果商品同步 draft_verified_revision
                db.execute(text("""
                    update products set draft_verified_revision = revision
                    where id = :pid and automation_result is true
                      and draft_verified_revision is not null
                """), {"pid": r["id"]})
                ok += 1
            except Exception as exc:  # noqa: BLE001
                print(f"  #{r['id']} 失败 {type(exc).__name__}: {str(exc)[:80]}")
                fail += 1
        db.commit()

        # 回读校验
        ids = [r["id"] for r in todo]
        back = db.execute(text("""
            select id, payload->>'sourceUrl'
            from products where id = any(:ids)
        """), {"ids": ids}).fetchall()
        got = sum(1 for _id, u in back if u)
        print(f"\n写入成功 {ok} / 失败 {fail}")
        print(f"回读确认有货源 {got} / {len(ids)}")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())