"""颜色清洗 / 校验探针（纯函数，不碰 DB）。

跑：python _meas/probe_color.py
结果写 _meas/color.json —— 中文在 PowerShell 控制台会乱码，一律看文件。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.color_service import (  # noqa: E402
    ColorError,
    MAX_COLORS,
    clean_color_strict,
    color_attribute_problem,
    normalize_colors,
)

rows = []


def record(case, ok, detail):
    rows.append({"case": case, "result": "PASS" if ok else "FAIL", "detail": detail})


def expect_eq(case, fn, expected):
    try:
        got = fn()
    except Exception as exc:  # noqa: BLE001 - 探针要把异常也记成 FAIL
        record(case, False, f"抛了 {type(exc).__name__}: {exc}")
        return
    record(case, got == expected, f"got={got!r} want={expected!r}")


def expect_raise(case, fn):
    try:
        got = fn()
    except ColorError as exc:
        record(case, True, f"ColorError: {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        record(case, False, f"抛错类型不对 {type(exc).__name__}: {exc}")
        return
    record(case, False, f"没抛错，返回 {got!r}")


def expect_contains(case, fn, needle):
    try:
        got = fn()
    except Exception as exc:  # noqa: BLE001
        record(case, False, f"抛了 {type(exc).__name__}: {exc}")
        return
    record(case, bool(got) and needle in got, f"got={got!r} 需含 {needle!r}")


def expect_empty(case, fn):
    try:
        got = fn()
    except Exception as exc:  # noqa: BLE001
        record(case, False, f"抛了 {type(exc).__name__}: {exc}")
        return
    record(case, got == "", f"got={got!r} 需为空串")


def color_prop(values, name="颜色"):
    return {"attrName": name,
            "attrValueList": [{"attrValueId": vid, "attrValue": text} for vid, text in values]}


# --- clean_color_strict ---------------------------------------------------
expect_eq("clean 1987白色", lambda: clean_color_strict("1987白色"), "白色")
expect_eq("clean Y18[beige]", lambda: clean_color_strict("Y18[beige]"), "Beige")
expect_eq("clean 黑色 加绒", lambda: clean_color_strict("黑色 加绒"), "黑色加绒")
expect_eq("clean X鞋-红色", lambda: clean_color_strict("X鞋-红色"), "红色")
expect_eq("clean 红色 2XL", lambda: clean_color_strict("红色 2XL"), "红色")
expect_raise("clean zzz 必须抛", lambda: clean_color_strict("zzz"))
expect_raise("clean 空值 必须抛", lambda: clean_color_strict(""))

# --- color_attribute_problem ---------------------------------------------
expect_contains("problem 0 个颜色属性", lambda: color_attribute_problem([]), "没有 SKU 属性")
expect_contains("problem 2 个颜色属性",
                lambda: color_attribute_problem(
                    [color_prop([("1", "白色")], "颜色"), color_prop([("2", "黑色")], "色彩")]),
                "2 个颜色属性")
expect_contains("problem 规格型号",
                lambda: color_attribute_problem([{"attrName": "规格型号", "attrValueList": []}]),
                "规格型号")
expect_contains("problem 颜色尺码合并",
                lambda: color_attribute_problem([{"attrName": "颜色尺码", "attrValueList": []}]),
                "合并在同一个属性")
expect_empty("problem 正常 1 个颜色属性",
             lambda: color_attribute_problem([color_prop([("1", "白色")])]))

# --- normalize_colors -----------------------------------------------------
NORMAL = {
    "cid": "123",
    "skuPropertyList": [color_prop([("1", "白色"), ("2", "黑色"), ("3", "红色")])],
    "skuMap": {"1;9": {"isDelete": "0"}, "2;9": {"isDelete": "0"}, "3;9": {"isDelete": "0"}},
}


def normal_case():
    payload = json.loads(json.dumps(NORMAL))
    out = normalize_colors(payload)
    return {"colors": out["colors"], "skuKeys": sorted(out["skuMap"]),
            "message": out["message"]}


expect_eq("normalize 3 色保留且 SKU 键在", normal_case,
          {"colors": ["白色", "黑色", "红色"],
           "skuKeys": ["1;9", "2;9", "3;9"], "message": ""})


def dedupe_case():
    payload = {"cid": "1",
               "skuPropertyList": [color_prop([("1", "白色"), ("2", "白色 "), ("3", "白")])],
               "skuMap": {"1;9": {"isDelete": "0"}, "2;9": {"isDelete": "1"},
                          "3;9": {"isDelete": "0"}}}
    out = normalize_colors(payload)
    return {"colors": out["colors"], "removed": out["removed"],
            "skuKeys": sorted(out["skuMap"])}


expect_eq("normalize 白/白色/白色空格 去重成 1 个", dedupe_case,
          {"colors": ["白色"], "removed": ["2", "3"], "skuKeys": ["1;9"]})


def eleven_case():
    # 用真实颜色词造 11 个，确保走的是「超过上限」而不是「认不出」。
    # SKU 键用的是 attrValueId（不是序号），否则映射不唯一会被直接拦下。
    names = ["白色", "黑色", "红色", "蓝色", "绿色", "黄色", "灰色", "粉色",
             "紫色", "橙色", "棕色"]
    values = [(f"id{index + 1}", name) for index, name in enumerate(names)]
    payload = {"cid": "1",
               "skuPropertyList": [color_prop(values)],
               "skuMap": {f"id{index + 1};9": {"isDelete": "0"} for index in range(11)}}
    out = normalize_colors(payload)
    return {"count": len(out["colors"]), "colors": out["colors"],
            "skuCount": len(out["skuMap"]), "message": out["message"]}


eleven = eleven_case()
record(f"normalize 11 色截断到 {MAX_COLORS}",
       eleven["count"] == MAX_COLORS and eleven["skuCount"] == MAX_COLORS
       and "超过上限" in eleven["message"],
       json.dumps(eleven, ensure_ascii=False))


def last_sku_removed_case():
    # 白色(1) 与「白色 」(3) 归并成一组，3 被删；SKU 里唯一存活的是 1;9，
    # 而它本身是已删除 SKU —— 剪完就没有任何在售 SKU 了，必须拦下。
    payload = {"cid": "1",
               "skuPropertyList": [color_prop([("1", "白色"), ("3", "白色 ")])],
               "skuMap": {"1;9": {"isDelete": "1"}, "3;9": {"isDelete": "0"}}}
    return normalize_colors(payload)


expect_raise("normalize 删完无活跃 SKU 必须抛", last_sku_removed_case)

expect_raise("normalize attrValueId 缺失必须抛",
             lambda: normalize_colors(
                 {"cid": "1",
                  "skuPropertyList": [{"attrName": "颜色",
                                       "attrValueList": [{"attrValue": "白色"}]}],
                  "skuMap": {"1;9": {"isDelete": "0"}}}))

expect_raise("normalize attrValueId 重复必须抛",
             lambda: normalize_colors(
                 {"cid": "1",
                  "skuPropertyList": [color_prop([("1", "白色"), ("1", "黑色")])],
                  "skuMap": {"1;9": {"isDelete": "0"}}}))

expect_raise("normalize 两个颜色属性必须抛",
             lambda: normalize_colors(
                 {"cid": "1",
                  "skuPropertyList": [color_prop([("1", "白色")], "颜色"),
                                      color_prop([("2", "黑色")], "色彩")],
                  "skuMap": {"1;9": {"isDelete": "0"}}}))

expect_raise("normalize 认不出的颜色必须抛",
             lambda: normalize_colors(
                 {"cid": "1",
                  "skuPropertyList": [color_prop([("1", "zzz")])],
                  "skuMap": {"1;9": {"isDelete": "0"}}}))

# --- 新旧对比：证明缺口真的存在过 -----------------------------------------
from services import product_service  # noqa: E402

old_zzz = product_service.clean_color("zzz")
# 旧实现并不是字面「原样返回」：纯拉丁文本走 capitalize 兜底，把 zzz 变成 Zzz
# 当颜色交上去。结论一样——非颜色被静默放行，只是伪装成了像颜色的样子。
record("对比 product_service.clean_color('zzz') 静默放行（旧缺口）",
       old_zzz == "Zzz", f"旧={old_zzz!r}")
record("对比 product_service.clean_color_strict 转发可用",
       product_service.clean_color_strict("1987白色") == "白色",
       repr(product_service.clean_color_strict("1987白色")))

# --- 与 pre_save_check 规则 8 的互操作 -------------------------------------
# 规则 8 要求「保留下来的 id 顺序与原来一致」。组代表可能是组里靠后的元素
# （「白色 」比「白色」晚出现），不重排就会让整件商品被挡在入库前。
from services import pre_save_check  # noqa: E402


def order_case():
    payload = {"cid": "1",
               "skuPropertyList": [color_prop([("1", "白色"), ("2", "黑色"),
                                               ("3", "白色 "), ("4", "红色")])],
               "skuMap": {"1;9": {"isDelete": "0"}, "2;9": {"isDelete": "0"},
                          "3;9": {"isDelete": "0"}, "4;9": {"isDelete": "0"}}}
    original = json.loads(json.dumps(payload))
    out = normalize_colors(payload)
    kept = [value["attrValueId"] for value in out["properties"][0]["attrValueList"]]
    unchanged = pre_save_check._retained_sku_images_unchanged(original, payload)
    return {"keptIds": kept, "colors": out["colors"], "rule8": unchanged}


expect_eq("normalize 保留下来的 id 顺序与原顺序一致（规则 8 不误伤）", order_case,
          {"keptIds": ["1", "2", "4"], "colors": ["白色", "黑色", "红色"], "rule8": True})

path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "color.json")
with open(path, "w", encoding="utf-8") as handle:
    json.dump(rows, handle, ensure_ascii=False, indent=1)

failed = [row for row in rows if row["result"] == "FAIL"]
print(f"PASS {len(rows) - len(failed)} / FAIL {len(failed)} / total {len(rows)}")
print(path)
