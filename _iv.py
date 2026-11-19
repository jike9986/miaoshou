import sys, time
sys.path.insert(0, "/opt/miaoshou-listing-v2/server")
from core.config import get_settings
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from services.account_service import account_client, require_account
from services import carrier_service, warehouse_cache
from models.product import Product

eng = create_engine(str(get_settings().database_url))

def run_batch(db, client, shop, payloads, wid, trees):
    t0 = time.time()
    carriers = carrier_service.collect_carriers(db, client, count=len(payloads))
    t_copy = time.time() - t0
    t0 = time.time()
    r = carrier_service.fill_carriers(client, carriers, payloads, shop_id=shop,
                                      warehouse_id=wid, verify_readback=False,
                                      category_trees=trees)
    t_fill = time.time() - t0
    return t_copy, t_fill, len(r["saved"]), len(r["failures"]), r["failures"][:1]

with Session(eng) as db:
    acc = require_account(db, "9")
    client = account_client(acc)
    SHOP = 19956376
    prods = db.query(Product).filter(Product.payload.isnot(None)).limit(20).all()
    allp = []
    for p in prods:
        pl = dict(p.payload); pl["site"] = "MY"; allp.append(pl)
    wid = warehouse_cache.cached_warehouse_id(client, SHOP)
    trees = {"MY": client.category_tree("MY")}

    for interval in [1.5, 1.3, 1.2, 1.0]:
        client._pacer.interval = interval
        batch = allp[:5]
        try:
            tc, tf, ok, bad, sample = run_batch(db, client, SHOP, batch, wid, trees)
            print("interval=%.1f  复制%.1fs + 写入%.1fs = %.1fs  成功%d 失败%d %s" % (
                interval, tc, tf, tc + tf, ok, bad, sample), flush=True)
        except Exception as exc:
            print("interval=%.1f  异常: %s" % (interval, str(exc)[:100]), flush=True)
        time.sleep(3)
