import sys, os, json, tempfile
sys.path.insert(0, os.getcwd())
os.environ["AOHUANG_DATA_DIR"] = tempfile.mkdtemp()
from db.session import init_db, session_scope
init_db()
from services import schedule_service as ss
from models.schedule import Schedule
from models.account import Account
from services import crypto

out = []
with session_scope() as db:
    acc = Account(name="p", app_key="k", app_secret_enc=crypto.encrypt("s"), shops="[]", created_at=0)
    db.add(acc); db.commit()
    task = Schedule(id="t1", name="probe-task", account_id=acc.id, shop_id="18077147",
                    per_run=3, frequency="daily", enabled=True)
    db.add(task); db.commit()
    for i in range(3):
        summ = {"status": "empty", "reason": "仓库是空的（第%d轮）" % (i+1),
                "requested": 3, "selected": 0, "published": 0, "failed": 0}
        ss.record_log(db, task, summ)
    db.commit()
    r = ss.list_logs(db, task_id="t1")
    out.append({"case":"历史","total":r["total"],"returned":len(r["items"]),
                "newest": r["items"][0]["reason"] if r["items"] else None,
                "statuses": [i["status"] for i in r["items"]]})
    r2 = ss.list_logs(db, task_id="")
    out.append({"case":"全部","total":r2["total"]})
    r3 = ss.list_logs(db, task_id="t1", limit=2)
    out.append({"case":"限量","returned":len(r3["items"]),"limit":r3["limit"]})
open("_meas/log.json","w",encoding="utf-8").write(json.dumps(out, ensure_ascii=False, indent=1))
print("done")
