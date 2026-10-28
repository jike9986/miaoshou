import sys, os, json
sys.path.insert(0, os.getcwd())
from services.price_template import resolve_rate_with_source
from services import exchange_rate

out = []
# 1 手动 override
r, s = resolve_rate_with_source(None, "1.72")
out.append({"case": "手动", "rate": str(r), "src": s})
# 2 商品自身比值
item = {"skuMap": {"a": {"originPrice": "100", "price": "61"}, "b": {"originPrice": "200", "price": "122", "isDelete": "1"}}}
r, s = resolve_rate_with_source(item)
out.append({"case": "商品比值", "rate": str(r), "src": s})
# 3 实时（允许联网）
exchange_rate.reset_cache()
live, src = exchange_rate.live_rate()
out.append({"case": "实时接口", "rate": str(live), "src": src})
r, s = resolve_rate_with_source(None)
out.append({"case": "走实时", "rate": str(r), "src": s})
# 4 断网兜底
r, s = resolve_rate_with_source(None, allow_live=False)
out.append({"case": "兜底", "rate": str(r), "src": s})
open("_meas/fx.json","w",encoding="utf-8").write(json.dumps(out, ensure_ascii=False, indent=1))
print("done")
