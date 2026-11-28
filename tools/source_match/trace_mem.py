"""完整处理 1 件商品，用 tracemalloc 抓内存分配来源。"""
import gc
import tracemalloc
import warnings

warnings.filterwarnings("ignore")


def rss_mb() -> float:
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except Exception:  # noqa: BLE001
        pass
    return 0.0


print(f"基线 RSS = {rss_mb():.0f} MB")
tracemalloc.start(15)

from db.session import SessionLocal
from models.account import Account
from services import account_service, warehouse_service as ws

db = SessionLocal()
acc = db.query(Account).filter(Account.id == 12).one()
client = account_service.account_client(acc)

print("\n>>> 处理 3562895768 <<<")
try:
    res = ws.save_from_collect_box(
        db, acc, client, detail_id="3562895768", shop_id="19977183",
        language="ms", price_template=None,
        allow_image_review=True, automation_result=True,
        operator_id=64,
        image_limits={"main": 9, "sku": 9, "detail": 9},
    )
    print(f"  draftVerified={res.get('draftVerified')}")
except Exception as ex:  # noqa: BLE001
    print(f"  失败: {type(ex).__name__}: {str(ex)[:200]}")
finally:
    client.close()

gc.collect()
cur, peak = tracemalloc.get_traced_memory()
print(f"\n处理完 RSS = {rss_mb():.0f} MB")
print(f"tracemalloc 当前 = {cur/1024/1024:.1f} MB, 峰值 = {peak/1024/1024:.1f} MB")

print("\n=== 内存占用 Top 10 ===")
snap = tracemalloc.take_snapshot()
for stat in snap.statistics("lineno")[:10]:
    print(f"  {stat.size/1024/1024:>7.1f} MB  {stat.traceback.format()[-1][:110]}")

tracemalloc.stop()
db.close()