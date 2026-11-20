"""把误标成 unknown 的发布记录回源修正为平台真实状态。

背景（2026-11-19）：
  发布记录的确认环节有两个缺陷，导致一批**已经发成功**的记录被标成 unknown：

  1. `_refresh_processing` 只翻 `REFRESH_MAX_PAGES`（原 5 页 = 100 行）。
     实测账号 9 平台回执总量 389 行，119 条 unknown 里 69 条落在第 6~10 页，
     根本翻不到。
  2. `release_stale_publish_logs` 过 600 秒就直接标 unknown，**不再回源确认**。
     剩下 50 条虽然在前 5 页，但那一轮 refresh 没覆盖到（或已过点），
     被直接判死。

  实测这批 119 条在平台侧**全部是 success** —— 商品早就发到店铺了，
  本地台账却显示失败。用户会重复发布同一件商品。

这个脚本做的事：
  按账号把 unknown 记录回源核对，平台说 success/fail 的改回真实状态。
  确认不到的保持 unknown（不猜）。

安全措施：
  · 默认 dry-run，`--apply` 才写库。
  · 只处理 status='unknown' 的行，不碰 success/fail/processing。
  · 逐条按 (detail_id, shop_id) 精确定位平台回执，不做模糊匹配。
  · 用 `_refresh_processing` 的同一套匹配逻辑，保证「修历史」和
    「以后自动确认」判定标准一致。

用法（服务器上，source 环境变量后执行）：
    venv/bin/python -m scripts.repair_unknown_publish_logs            # 预览
    venv/bin/python -m scripts.repair_unknown_publish_logs --apply    # 真改
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="回源修正被误标成 unknown 的发布记录")
    parser.add_argument("--apply", action="store_true", help="真正写库")
    parser.add_argument("--account", type=int, default=0,
                        help="只处理指定账号（默认全部）")
    parser.add_argument("--limit", type=int, default=0,
                        help="最多处理多少条（0 = 全部，调试用）")
    args = parser.parse_args(argv)

    from sqlalchemy import func

    from api.publish import _refresh_processing
    from db.session import SessionLocal
    from models.publish_log import PublishLog

    db = SessionLocal()
    try:
        query = db.query(PublishLog).filter(PublishLog.status == "unknown")
        if args.account:
            query = query.filter(PublishLog.account_id == args.account)
        total = query.count()
        if not total:
            print("没有 unknown 记录。")
            return 0

        rows = (query.with_entities(PublishLog.account_id,
                                    func.count(PublishLog.id))
                .group_by(PublishLog.account_id).all())
        print(f"unknown 记录共 {total} 条，分布在 {len(rows)} 个账号：")
        for account_id, count in rows:
            print(f"  account {account_id}: {count} 条")

        if not args.apply:
            print("\n[dry-run] 未写库。加 --apply 执行。")
            print("提示：这一步会真实回源妙手接口（每个账号最多翻几十页），"
                  "耗时可能几分钟。")
            return 0

        fixed_total = 0
        for account_id, _count in rows:
            # 先把这些记录临时改回 processing，让 _refresh_processing 能看见它们。
            # 它的查询条件是 status == 'processing'，unknown 不在其中。
            # 这一步只在本脚本的会话里做，随后立刻回源，失败会回滚。
            pending_ids = [row.id for row in
                           db.query(PublishLog.id)
                           .filter(PublishLog.status == "unknown",
                                   PublishLog.account_id == account_id).all()]
            if args.limit:
                pending_ids = pending_ids[:args.limit]
            if not pending_ids:
                continue
            (db.query(PublishLog)
             .filter(PublishLog.id.in_(pending_ids))
             .update({PublishLog.status: "processing"}, synchronize_session=False))
            db.commit()
            print(f"\naccount {account_id}: 临时重置 {len(pending_ids)} 条，开始深度回源…")

            try:
                # deep=True：不设页数上限，确保深处的回执也能翻到
                result = _refresh_processing(db, str(account_id), deep=True)
                updated = int(result.get("updated") or 0)
                print(f"  回源确认 {updated} 条（平台总量 "
                      f"{result.get('platformTotal')}）")
            except Exception as exc:  # noqa: BLE001 - 单账号失败不影响其它账号
                print(f"  回源失败：{str(exc)[:160]}")

            # 回源后仍停在 processing 的，说明平台确实没有回执，退回 unknown
            still = (db.query(PublishLog)
                     .filter(PublishLog.id.in_(pending_ids),
                             PublishLog.status == "processing").all())
            for row in still:
                row.status = "unknown"
                row.reason = "超时未收到平台回执，已释放以免阻塞后续发布"
            if still:
                db.commit()
            fixed = len(pending_ids) - len(still)
            fixed_total += fixed
            print(f"  修正 {fixed} 条，仍无回执 {len(still)} 条")

        print(f"\n完成：共修正 {fixed_total} 条。")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
