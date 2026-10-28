"""独立审查用的验证脚本（不改真实数据：用 AOHUANG_DATA_DIR 指向临时副本）。"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

REAL = Path(os.environ["LOCALAPPDATA"]) / "aohuang-desktop" / "app.db"
TMP = Path(tempfile.mkdtemp(prefix="audit-"))
shutil.copy2(REAL, TMP / "app.db")
for name in ("secret.key",):
    src = REAL.parent / name
    if src.is_file():
        shutil.copy2(src, TMP / name)
os.environ["AOHUANG_DATA_DIR"] = str(TMP)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

OUT = []


def report(tag, ok, detail=""):
    OUT.append((tag, ok, detail))
    print(f"[{'OK ' if ok else 'BAD'}] {tag}: {detail}")


def main() -> int:
    from fastapi.testclient import TestClient
    import main as app_main
    from core.config import get_settings
    from db.session import SessionLocal
    from models.product import Product
    from models.schedule import Schedule
    from models.publish_log import PublishLog
    from services import publish_service, schedule_service, warehouse_service, carrier_service

    token = get_settings().local_secret()
    client = TestClient(app_main.app, raise_server_exceptions=False)
    H = {"Authorization": f"Bearer {token}"}

    print("data_dir =", os.environ["AOHUANG_DATA_DIR"])

    # ============ P0-4 / P0-2: published list -> int ============
    print("\n--- P0-4/P0-2 ---")
    print("_count(['a','b']) =", schedule_service._count(["a", "b"]))
    print("_count(['a']) =", schedule_service._count(["a"]))
    print("_count('3') =", schedule_service._count("3"))
    # old bug shape
    try:
        int({"published": ["1", "2"]}.get("published") or 0)
        print("int(list) unexpectedly ok")
    except TypeError as exc:
        print("int(list) still raises TypeError (old bug shape):", exc)
    print("run_due source uses _count:", "_count(outcome" in Path(
        Path(app_main.__file__).parent / "services" / "schedule_service.py").read_text(
        encoding="utf-8"))

    # ============ P0-5: _pair_entries with partial failure ============
    print("\n--- P0-5 ---")
    entries = [{"detailId": "111"}, {"detailId": "222"}]
    # only 2nd carrier succeeded -> saved=[901], savedSources=[1]
    paired = publish_service._pair_entries(entries, ["901"], [], saved_sources=[1])
    report("P0-5 只第2个成功时按 savedSources 配对",
           paired == [(entries[1], "901")], str(paired))
    # 3 entries, middle failed: saved=[901,903], savedSources=[0,2]
    e3 = [{"detailId": "a"}, {"detailId": "b"}, {"detailId": "c"}]
    paired3 = publish_service._pair_entries(e3, ["901", "903"], [], saved_sources=[0, 2])
    report("P0-5 三件中间失败配对", [p[1] for p in paired3] == ["901", "903"]
           and [p[0]["detailId"] for p in paired3] == ["a", "c"], str([(p[0]["detailId"], p[1]) for p in paired3]))
    # no savedSources, mismatched length -> refuse
    report("P0-5 无配对信息不硬配", publish_service._pair_entries(entries, ["900"], []) == [], "")
    # direct mode all success, no savedSources
    report("P0-5 direct 模式全成功按序配对",
           publish_service._pair_entries(entries, ["111", "222"], []) ==
           [(entries[0], "111"), (entries[1], "222")], "")

    # ============ P0-6: _mark_published only successful ============
    print("\n--- P0-6 ---")
    db = SessionLocal()
    rows = db.query(Product).filter(Product.deleted_at.is_(None)).limit(2).all()
    if len(rows) < 2:
        report("P0-6 无样本", False, "need 2 products")
    else:
        ok_row, fail_row = rows[0], rows[1]
        acc = publish_service.require_shop  # noqa: F841
        from services.account_service import require_account
        account = require_account(db, None)
        entries_ok = [{"detailId": ok_row.detail_id, "product": ok_row}]
        entries_all = [{"detailId": ok_row.detail_id, "product": ok_row},
                       {"detailId": fail_row.detail_id, "product": fail_row}]
        before = (ok_row.status, fail_row.status)
        publish_service._mark_published(db, entries_ok, "18077147", account)
        db.rollback()
        db.expire_all()
        print("after _mark_published(success only):",
              ok_row.status, fail_row.status, "before:", before)
        report("P0-6 只标记成功件后失败件仍非 published",
               fail_row.status != "published", f"fail_row.status={fail_row.status}")
        db.close()

    # ============ reproduce the publish() code path around _mark_published ============
    print("\n--- P0-6 代码路径核对 ---")
    src = (Path(app_main.__file__).parent / "services" / "publish_service.py").read_text(
        encoding="utf-8")
    idx = src.find("if published and not claim_only:")
    print(src[idx:idx + 400])
    db = SessionLocal()
    again = db.query(Product).filter(Product.deleted_at.is_(None)).limit(2).all()
    for r in again:
        print("  product", r.id, r.status, "claimed=", r.claimed)
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
