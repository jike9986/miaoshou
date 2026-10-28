"""独立审查验证 part2：claimOnly 部分成功、summarize 计数、PATCH null、语言校验。"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

REAL = Path(os.environ["LOCALAPPDATA"]) / "aohuang-desktop" / "app.db"
TMP = Path(tempfile.mkdtemp(prefix="audit2-"))
shutil.copy2(REAL, TMP / "app.db")
src_key = REAL.parent / "secret.key"
if src_key.is_file():
    shutil.copy2(src_key, TMP / "secret.key")
os.environ["AOHUANG_DATA_DIR"] = str(TMP)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))


def report(tag, ok, detail=""):
    print(f"[{'OK ' if ok else 'BAD'}] {tag}: {detail}")


class FakeClaimClient:
    """模拟妙手 claim_to_shop 的各种返回。"""

    def __init__(self, response):
        self.response = response
        self.calls = []

    def claim_to_shop(self, detail_ids, shop_ids):
        self.calls.append((list(detail_ids), list(shop_ids)))
        return self.response

    def close(self):
        pass


def main() -> int:
    from db.session import SessionLocal
    from models.product import Product
    from services import publish_service, schedule_service, setting_service, carrier_service
    from services import account_service
    from core import errors

    print("data_dir =", os.environ["AOHUANG_DATA_DIR"])
    db = SessionLocal()
    setting_service.set_publish_enabled(db, True)
    db.commit()

    acct = account_service.require_account(db, None)
    shop = "18077147"

    # 用两件真实商品构造 entries（只读，最后 rollback）
    rows = db.query(Product).filter(Product.deleted_at.is_(None)).limit(2).all()
    print("样本商品:", [(r.id, r.detail_id, r.shop_id) for r in rows])

    print("\n=== P1-2 claimOnly 部分成功解析 ===")
    cases = {
        "上游完整 idMap（全部成功）": {
            "data": {"platformCollectBoxDetailIdMap": {"18077147": {"1": "7001", "2": "7002"}}}},
        "上游只返回 1 个（部分成功）": {
            "data": {"platformCollectBoxDetailIdMap": {"18077147": {"1": "7001"}}}},
        "上游无 idMap 只有 errorMap（失败）": {
            "data": {"successNum": 0, "failNum": 2,
                     "errorMap": {"1": "店铺不可用", "2": "店铺不可用"}}},
        "上游空 data": {"data": {}},
        "上游 success:false 无 data": {"success": False, "message": "参数错误"},
    }
    orig_client = account_service.account_client
    orig_resolve = account_service.resolve_account
    try:
        for label, resp in cases.items():
            fake = FakeClaimClient(resp)
            account_service.account_client = lambda a, c=fake: c
            detail_ids = [int(r.detail_id) for r in rows]
            payload = {
                "confirm": True, "shopId": shop, "claimOnly": True,
                "items": [{"detailId": r.detail_id, "shopId": shop} for r in rows],
            }
            try:
                result = publish_service.publish(db, payload)
                db.rollback()
                print(f"  {label}: claimed={result['claimed']} "
                      f"(请求 {len(detail_ids)} 件)")
            except errors.AppError as exc:
                db.rollback()
                print(f"  {label}: AppError {exc.code} {exc.message[:60]}")
        print("  claimed_detail_ids 对 errorMap 的解析:",
              carrier_service.claimed_detail_ids(
                  {"data": {"successNum": 0, "failNum": 2, "errorMap": {"1": "x"}}}))
    finally:
        account_service.account_client = orig_client
        account_service.resolve_account = orig_resolve
        db.rollback()

    print("\n=== P1-4 summarize 部分成功失败数 ===")
    for kw in [
        dict(requested=5, selected=3, published=2, failures=1),
        dict(requested=5, selected=3, published=2, failures=2),
        dict(requested=5, selected=3, published=3, failures=1),
        dict(requested=10, selected=3, published=3, failures=0),
        dict(requested=5, selected=5, published=0, failures=0),
        dict(requested=5, selected=0, published=0, failures=0),
        dict(requested=5, selected=3, published=2, failures=7),
    ]:
        s = schedule_service.summarize(task_id="t", **kw)
        print(f"  {kw} -> status={s['status']} published={s['published']} "
              f"failed={s['failed']} reason={s['reason'][:50]}")

    print("\n=== P1-3 语言校验（店铺无 site） ===")
    print("  DB accounts.shops:", acct.shops, "account.site:", acct.site)
    shops = account_service.cached_shops(acct)
    print("  cached_shops:", shops)
    warn = publish_service.check_language("zh", str(shops[0].get("site") or ""),
                                          allow_mismatch=False)
    print("  check_language('zh', site=%r) ->" % shops[0].get("site"), warn)
    report("P1-3 站点未知时给显式 warning 而不是静默放过", warn is not None, str(warn))
    try:
        publish_service.check_language("zh", "MY", allow_mismatch=False)
        print("  check_language('zh','MY') -> 未拦截（BAD）")
    except errors.AppError as exc:
        print(f"  check_language('zh','MY') -> {exc.code} {exc.message[:70]}")

    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
