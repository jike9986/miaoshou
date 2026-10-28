import sys, os, json, tempfile
sys.path.insert(0, os.getcwd())
os.environ["AOHUANG_DATA_DIR"] = tempfile.mkdtemp()
from db.session import init_db, session_scope
init_db()
from services import schedule_service as ss, setting_service, crypto
from models.schedule import Schedule
from models.account import Account

with session_scope() as db:
    acc = Account(name="probe", app_key="k", app_secret_enc=crypto.encrypt("s"),
                  shops=json.dumps([{"shopId":"18077147","shopName":"LuoMan"}]),
                  created_at=0)
    db.add(acc); db.commit()
    aid = acc.id
    task = Schedule(id="t1", name="t", account_id=aid, shop_id="18077147",
                    per_run=5, frequency="daily", enabled=True)
    db.add(task); db.commit()
    setting_service.set_read_only(db, True); db.commit()
    r = ss.execute(db, task)
    print("read-only ->", r["status"], "|", r["reason"])
    setting_service.set_read_only(db, False); db.commit()
    r2 = ss.execute(db, task)
    print("writable  -> status:", r2["status"], "| reason:", str(r2.get("reason",""))[:50])
