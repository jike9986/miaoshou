import sys, os, json
sys.path.insert(0, os.getcwd())
from services.transform_service import transform_item, TransformError

def run(name, item, **kw):
    try:
        out, rep = transform_item(item, **kw)
        return {"case": name, "title": out.get("title"), "price": [s.get("price") for s in (out.get("skuMap") or {}).values()],
                "include": [s.get("priceIncludeVat") for s in (out.get("skuMap") or {}).values()],
                "imgs": len(out.get("imgUrls") or []),
                "changed": rep["changed"], "warn": rep["warnings"]}
    except TransformError as e:
        return {"case": name, "error": str(e)}

base = {"title": "2024新款跑步鞋 包邮 特价", "imgUrls": ["a.jpg","b.jpg","c.jpg","d.jpg"],
        "skuMap": {"s1": {"price": 100, "priceIncludeVat": 106}, "s2": {"price": 200, "priceIncludeVat": 212},
                   "s3": {"price": 50, "isDelete": "1"}}}
out = []
out.append(run("1.35倍", dict(base), price_multiplier=1.35))
out.append(run("目标价39.9", dict(base), target_price=39.9))
out.append(run("目标价+含税", dict(base), target_price=39.9, target_price_include_vat=42.3))
out.append(run("删图1和3", dict(base), remove_image_indexes=[1,3]))
out.append(run("删图URL", dict(base), remove_image_urls=["b.jpg"]))
out.append(run("序号越界", dict(base), remove_image_indexes=[9]))
out.append(run("倍数+目标价", dict(base), price_multiplier=1.2, target_price=50))
out.append(run("只给含税价", dict(base), target_price_include_vat=42))
out.append(run("清标题", dict(base), title_words={"remove":["包邮","特价"],"review":["新款"]}))
open("_meas/tr.json","w",encoding="utf-8").write(json.dumps(out, ensure_ascii=False, indent=1))
print("done")
