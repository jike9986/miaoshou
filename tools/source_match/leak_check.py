"""处理一件后，抓 1075MB 里到底有什么（RSS 不释放的来源）。"""
import gc
import warnings

warnings.filterwarnings("ignore")


def rss() -> float:
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except Exception:  # noqa: BLE001
        pass
    return 0.0


def mark(t):
    gc.collect()
    print(f"  {t:<38} {rss():>8.0f} MB")


mark("基线")
from db.session import SessionLocal
from models.account import Account
from services import account_service, warehouse_service as ws

db = SessionLocal()
acc = db.query(Account).filter(Account.id == 12).one()
client = account_service.account_client(acc)
mark("导入 + 连接")

res = ws.save_from_collect_box(
    db, acc, client, detail_id="3562895408", shop_id="19977183",
    language="ms", price_template=None, allow_image_review=True,
    automation_result=True, operator_id=64,
    image_limits={"main": 9, "sku": 9, "detail": 9},
)
client.close()
mark(f"处理 1 件（verified={res.get('draftVerified')}）")

# 看看能不能靠 gc 收回来
for i in range(3):
    gc.collect()
    mark(f"gc 第 {i+1} 次")

# 看线程数
import threading
print(f"\n  线程数: {threading.active_count()}")
for t in threading.enumerate():
    print(f"    - {t.name}  daemon={t.daemon}")

db.close()
mark("关库")