"""并发 4 件，复现失败。"""
import sys
import threading
import warnings
from concurrent.futures import ThreadPoolExecutor

warnings.filterwarnings("ignore")

from db.session import SessionLocal
from models.account import Account
from services import account_service, readback_check, warehouse_service as ws

SHOP = "19977183"
CONCURRENCY = int(sys.argv[1]) if len(sys.argv) > 1 else 4

TARGETS = ["3563325267", "3563324668", "3563317041", "3562906475"]

lock = threading.Lock()
records = []


def run(did):
    db = SessionLocal()
    try:
        acc = db.query(Account).filter(Account.id == 12).one()
        client = account_service.account_client(acc)
        try:
            res = ws.save_from_collect_box(
                db, acc, client, detail_id=did, shop_id=SHOP,
                language="ms", price_template=None,
                allow_image_review=True, automation_result=True,
                operator_id=64,
                image_limits={"mainImageCount": 9, "skuImageCount": 9,
                              "detailImageCount": 9},
            )
            rec = (did, True, res.get("draftVerified"), None)
        except Exception as ex:  # noqa: BLE001
            rec = (did, False, None, f"{type(ex).__name__}: {str(ex)[:160]}")
        client.close()
        with lock:
            records.append(rec)
        return rec
    finally:
        db.close()


print(f"并发 {CONCURRENCY} 件开始\n")
with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
    list(ex.map(run, TARGETS))

print("\n" + "=" * 72)
ok = bad = 0
for did, success, verified, err in records:
    if success and verified:
        ok += 1
        print(f"OK   {did}  draftVerified=True")
    else:
        bad += 1
        print(f"FAIL {did}  {err or f'draftVerified={verified}'}")
print("=" * 72)
print(f"结果: {ok}/{len(TARGETS)} 通过，{bad} 失败")