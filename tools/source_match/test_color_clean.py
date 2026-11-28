"""验证颜色清洗对 Beras hitam. / Meter 的实际输出。"""
import warnings

warnings.filterwarnings("ignore")

from services import color_service

CASES = [
    "Beras hitam.",
    "Meter",
    "Hitam",
    "Beras hitam",
    "meter",
    "METER",
]

print("=" * 70)
print("clean_color_strict 输出")
print("=" * 70)
for v in CASES:
    try:
        out = color_service.clean_color_strict(v)
        print(f"  {v!r:<22} -> {out!r}")
    except Exception as ex:  # noqa: BLE001
        print(f"  {v!r:<22} -> ERR {type(ex).__name__}: {str(ex)[:60]}")

print()
print("=" * 70)
print("normalize_colors 对整块 props 的效果")
print("=" * 70)
props = [
    {
        "attrName": "Warna", "attrId": "100000",
        "attrValueList": [
            {"attrValue": "Beras hitam.", "imgUrl": "https://x/a.jpg",
             "attrValueId": "id1"},
            {"attrValue": "Meter", "imgUrl": "https://x/b.jpg",
             "attrValueId": "id2"},
        ],
    },
    {
        "attrName": "Saiz", "attrId": "100007",
        "attrValueList": [
            {"attrValue": "35", "imgUrl": "", "attrValueId": "s35"},
            {"attrValue": "36", "imgUrl": "", "attrValueId": "s36"},
        ],
    },
]
sku_map = {
    ";Beras hitam.;35;": {"price": "10"},
    ";Beras hitam.;36;": {"price": "10"},
    ";Meter;35;": {"price": "10"},
    ";Meter;36;": {"price": "10"},
}

import copy
p2 = copy.deepcopy(props)
sm2 = copy.deepcopy(sku_map)
try:
    result, report = color_service.normalize_colors(p2, sm2)
    print(f"  report: {report}")
    for prop in result:
        vals = [v.get("attrValue") for v in (prop.get("attrValueList") or [])]
        print(f"  {prop.get('attrName')}: {vals}")
    print(f"  skuMap 剩余: {list(sm2.keys())}")
except Exception as ex:  # noqa: BLE001
    print(f"  ERR {type(ex).__name__}: {str(ex)[:200]}")