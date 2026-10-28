"""独立审查验证 part3：claimOnly 部分成功（正确 patch 客户端）。"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

REAL = Path(os.environ["LOCALAPPDATA"]) / "aohuang-desktop" / "app.db"
TMP = Path(tempfile.mkdtemp(prefix="audit3-"))
shutil.copy2(REAL, TMP / "app.db")
if (REAL.parent / "secret.key").is_file():
    shutil.copy2(REAL.parent / "secret.key", TMP / "secret.key")
os.environ["AOHUANG_DATA_DIR"] = str(TMP)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))


class FakeClaimClient:
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
    from services import publish_service, setting_service
    from core import errors

    db = SessionLocal()
    setting_service.set_publish_enabled(db, True)
    db.commit()
    rows = db.query(Product).filter(Product.deleted_at.is_(None)).limit(2).all()
    n = len(rows)
    shop = "18077147"

    cases = {
        "A 上游 idMap 全成功(2 条)": {
            "data": {"successNum": 2, "failNum": 0,
                     "platformCollectBoxDetailIdMap": {"18077147": {"1": "7001", "2": "7002"}}}},
        "B 上游只成功 1 条(idMap 1 条)": {
            "data": {"successNum": 1, "failNum": 1,
                     "platformCollectBoxDetailIdMap": {"18077147": {"1": "7001"}},
                     "errorMap": {"2": "店铺不可用"}}},
        "C 上游全失败(successNum=0 + errorMap)": {
            "data": {"successNum": 0, "failNum": 2,
                     "errorMap": {"1": "店铺不可用", "2": "店铺不可用"}}},
        "D 上游 successNum=1 但没有 idMap": {
            "data": {"successNum": 1, "failNum": 1}},
        "E 上游空 data": {"data": {}},
        "F 上游 success:false": {"success": False, "message": "参数错误"},
    }

    orig = publish_service.account_client
    try:
        for label, resp in cases.items():
            fake = FakeClaimClient(resp)
            publish_service.account_client = lambda a, c=fake: c
            payload = {
                "confirm": True, "shopId": shop, "claimOnly": True,
                "items": [{"detailId": r.detail_id, "shopId": shop} for r in rows],
            }
            try:
                result = publish_service.publish(db, payload)
                path = ("路径 claimOnly" if result.get("mode") else "?")
                print(f"  {label}: 请求 {n} 件 -> claimed={result['claimed']} "
                      f"({len(result['claimed'])} 条) failures={result['failures']}")
            except errors.AppError as exc:
                print(f"  {label}: AppError {exc.code} {exc.message[:70]}")
            except Exception as exc:  # noqa: BLE001
                print(f"  {label}: {type(exc).__name__} {exc}")
            finally:
                db.rollback()
    finally:
        publish_service.account_client = orig
        db.rollback()
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
