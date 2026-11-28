"""测这批商品的颜色清洗：到底是什么把 skuImages 弄不一致。"""
import warnings

warnings.filterwarnings("ignore")

import copy

from services import color_service

CASES = ["Hitam", "Warna putih", "Warna hitam", "Putih", "Kuning air",
         "Coklat", "Beras hitam.", "Meter", "Kelabu", "Merah"]

print("=" * 74)
print("① clean_color_strict 逐个值")
print("=" * 74)
for v in CASES:
    try:
        print(f"  {v!r:<20} -> {color_service.clean_color_strict(v)!r}")
    except Exception as ex:  # noqa: BLE001
        print(f"  {v!r:<20} -> ERR {str(ex)[:60]}")

print()
print("=" * 74)
print("② normalize_colors 整块（2 个颜色 + 若干尺码）")
print("=" * 74)


def make_props(labels):
    vals = [{"attrValue": lab, "attrValueId": f"id{i}",
             "imgUrl": f"https://x/{i}.jpg"} for i, lab in enumerate(labels)]
    sizes = [{"attrValue": s, "attrValueId": f"s{s}", "imgUrl": ""}
             for s in ("34", "35", "36", "37")]
    return [
        {"attrName": "Warna", "attrId": "100000", "attrValueList": vals},
        {"attrName": "Saiz", "attrId": "100007", "attrValueList": sizes},
    ]


def make_sku(labels):
    out = {}
    for lab in labels:
        for s in ("34", "35", "36", "37"):
            out[f";{lab};{s};"] = {"price": "10", "stock": 5}
    return out


for labels in (["Hitam", "Warna putih"],
               ["Warna hitam", "Warna putih"],
               ["Hitam", "Putih"],
               ["Beras hitam.", "Meter"]):
    props = make_props(labels)
    sku = make_sku(labels)
    payload = {"skuPropertyList": props, "skuMap": sku, "cid": "603751"}
    try:
        result = color_service.normalize_colors(payload)
        after = [v.get("attrValue")
                 for v in (result["properties"][0].get("attrValueList") or [])]
        print(f"  {labels}  →  {after}")
        print(f"      removed={result.get('removed')} "
              f"message={str(result.get('message') or '')[:60]}")
    except Exception as ex:  # noqa: BLE001
        print(f"  {labels}  → ERR {str(ex)[:90]}")
    print()
