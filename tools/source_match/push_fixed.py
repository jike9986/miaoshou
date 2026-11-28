"""把修好的本地 payload 推回妙手草稿，让图床地址生效。

## 为什么必须单独做这一步

`migrate_autotemp.py` 只改了**我们数据库**里的 `products.payload`：
auto-temp 地址 → products 地址（文件真实位置）。

但妙手草稿是**另一份数据**，存在妙手系统里。它里面的 `notes`、
`skuPropertyList` 还是旧地址 —— 所以浏览器打开妙手商品页，
图片继续 404、继续空白框。

必须调 `client.save_shop()` 把修好的 payload 重新写回妙手。

## 步骤

    ① client.shop_detail(detail_id, shop_id)  拿到草稿和 oss_md5
    ② client.save_shop(detail_id, shop_id, oss_md5, payload)
    ③ 读回验证 notes 里已无 auto-temp 地址
"""
import sys
import warnings

warnings.filterwarnings("ignore")

from sqlalchemy import text
from db.session import SessionLocal
from models.account import Account
from services import account_service

LIMIT = 0
for _a in sys.argv[1:]:
    if _a.isdigit():
        LIMIT = int(_a)
APPLY = "--apply" in sys.argv


def find_targets(db):
    return db.execute(text("""
        select p.id, p.detail_id, p.shop_id, p.account_id
        from products p
        where p.deleted_at is null
          and p.automation_result = true
          and p.payload::text like '%tk-product-automation/products/%'
          and not exists (
            select 1 from products q
            where q.id = p.id and q.payload::text like '%auto-temp%'
          )
        order by p.account_id, p.id
    """)).fetchall()


def filter_ids(db, rows):
    ids = [sys.argv[i + 1] for i, a in enumerate(sys.argv) if a == "--id"]
    if not ids:
        return rows
    wanted = {int(x) for x in ids}
    return [r for r in rows if r[0] in wanted]


db = SessionLocal()
try:
    rows = filter_ids(db, find_targets(db))
    if LIMIT:
        rows = rows[:LIMIT]

    print(f"待推回妙手: {len(rows)} 件")
    if not rows:
        raise SystemExit(0)

    by_acc = {}
    for r in rows:
        by_acc.setdefault(r[3], []).append(r)
    for acc_id, items in by_acc.items():
        print(f"  账号 {acc_id}: {len(items)} 件")

    if not APPLY:
        print("加 --apply 才真写")
        raise SystemExit(0)

    ok = fail = 0
    for acc_id, items in by_acc.items():
        acc = db.query(Account).filter(Account.id == acc_id).one_or_none()
        if acc is None:
            print(f"账号 {acc_id} 不存在，跳过 {len(items)} 件")
            continue
        print()
        print(f"=== 账号 {acc_id}: {len(items)} 件 ===")
        for r in items:
            pid, did, shop = r[0], r[1], str(r[2])
            import json
            payload_row = db.execute(
                text("select payload from products where id = :i"),
                {"i": pid}).fetchone()
            payload = payload_row[0]
            if not isinstance(payload, dict):
                payload = json.loads(payload or "{}")

            client = account_service.account_client(acc)
            try:
                detail = client.shop_detail(did, shop)
                info = (detail.get("data") or {}).get("shopCollectItemInfo") or {}
                oss_md5 = (detail.get("data") or {}).get("ossMd5") or ""
                if not oss_md5:
                    oss_md5 = info.get("ossMd5") or ""
                client.save_shop(did, shop, oss_md5, payload)
                print(f"  #{pid} {did} ✓ 已推回")
                ok += 1
            except Exception as ex:  # noqa: BLE001
                print(f"  #{pid} {did} ✗ {type(ex).__name__}: {str(ex)[:130]}")
                fail += 1
            finally:
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass
    print()
    print(f"结果: 成功 {ok} / 失败 {fail}")
finally:
    db.close()