import sys, os, json, tempfile
sys.path.insert(0, os.getcwd())
os.environ["AOHUANG_DATA_DIR"] = tempfile.mkdtemp()
from db.session import init_db, session_scope
from services import carrier_service as cs

init_db()
out = []
with session_scope() as db:
    n = cs.add_links(db, ["123456789", "https://detail.1688.com/offer/987654321.html"])
    db.commit()
    links = cs.list_links(db)["links"]
    out.append({"case":"裸id与链接","added":n,"urls":[l["url"] for l in links]})

    n2 = cs.add_links(db, [f"https://detail.1688.com/offer/{100000+i}.html" for i in range(10)])
    db.commit()
    out.append({"case":"池上限","added":n2,"total":len(cs.list_links(db)["links"]),"limit":cs.POOL_SIZE})

    taken = []
    for _ in range(3):
        item = cs.take_link(db); db.commit()
        taken.append(item["url"] if item else None)
    out.append({"case":"轮换","taken":taken,"distinct":len(set(taken))})

    first = taken[0]
    cs.mark(db, first, ok=True); db.commit()
    row = [l for l in cs.list_links(db)["links"] if l["url"] == first][0]
    out.append({"case":"成功归零","failed":row["failed"],"ok":row["ok"]})

    cs.mark(db, first, ok=False); db.commit()
    row2 = [l for l in cs.list_links(db)["links"] if l["url"] == first][0]
    usable = [l["url"] for l in cs.list_links(db)["links"] if int(l.get("failed") or 0) < cs.MAX_FAILURES]
    out.append({"case":"失败弃用","failed":row2["failed"],"stillUsable":first in usable})

open("_meas/pool.json","w",encoding="utf-8").write(json.dumps(out, ensure_ascii=False, indent=1))
print("done")
