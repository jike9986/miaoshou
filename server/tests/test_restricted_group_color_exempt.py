"""受限分组的颜色不受「颜色数量」上限约束。

负责人 2026-11-19：「三个分组的那里面的颜色图片不受影响，超过九种都没关系」。

自营海外仓 / 海外仓定制款 / 精品 这三组是精品线，颜色多少由厂家定。
运营不能因为界面上设了「颜色数量 = 9」就把它们的颜色裁掉 ——
裁掉等于改了商品本身（SKU 少一批、颜色少几个，发布出去就是另一件货）。

主图 / 详情图的上限**仍然生效**：那两个是展示取舍，颜色是商品本体。
"""
from __future__ import annotations

import pytest

AUTO = "https://aohuang888.com/tk-product-automation/auto-temp/"

# 三个受限分组 id（生产库实测）
RESTRICTED = (1, 2, 3)


def _payload(n_colors: int) -> dict:
    values = []
    color_map = {}
    for index in range(n_colors):
        label = f"颜色{index}"
        url = AUTO + f"c{index}.png"
        values.append({
            "attrValueId": f"id{index}",
            "attrValue": label,
            "imgUrl": url,
            "imageUrl": url,
            "imgUrls": [url],
        })
        color_map[label] = {"name": label, "imgUrl": url, "imgUrls": [url]}
    sku_map = {
        f";颜色{i};{size};": {"price": "10", "stock": 5}
        for i in range(n_colors) for size in ("35", "36")
    }
    return {
        "skuPropertyList": [
            {"attrId": "100000", "attrName": "Warna", "propName": "颜色",
             "attrValueList": values},
        ],
        "skuMap": sku_map,
        "colorMap": color_map,
        "imgUrls": [AUTO + f"main{i}.png" for i in range(15)],
        "notes": "".join(f'<img src="{AUTO}d{i}.png">' for i in range(20)),
    }


@pytest.mark.parametrize("group_id", RESTRICTED)
def test_restricted_group_keeps_all_colors(group_id):
    """受限分组：12 个颜色设上限 9，一个都不能少。"""
    from services import warehouse_service

    payload = _payload(12)
    report = warehouse_service.apply_image_limits(
        payload, sku_limit=9, group_id=group_id)

    colors = payload["skuPropertyList"][0]["attrValueList"]
    assert len(colors) == 12, \
        f"受限分组 group_id={group_id} 的颜色被裁到 {len(colors)} 个"
    assert len(payload["colorMap"]) == 12, "受限分组的 colorMap 被裁了"
    assert len(payload["skuMap"]) == 24, "受限分组的 SKU 被删了"
    assert report.get("colors", {}).get("skipped"), "报告里没标注豁免"


@pytest.mark.parametrize("group_id", RESTRICTED)
def test_restricted_group_still_limits_main_and_detail(group_id):
    """主图 / 详情图上限对受限分组仍然生效 —— 那是展示取舍，不是商品本体。"""
    from services import warehouse_service

    payload = _payload(12)
    warehouse_service.apply_image_limits(
        payload, main_limit=9, detail_limit=9, sku_limit=9, group_id=group_id)

    assert len(payload["imgUrls"]) == 9, "主图上限失效了"
    assert payload["notes"].count("<img") == 9, "详情图上限失效了"
    # 颜色仍然完整
    assert len(payload["skuPropertyList"][0]["attrValueList"]) == 12


def test_normal_group_still_limited():
    """普通分组照旧裁到上限 —— 别把豁免写成全局放开。"""
    from services import warehouse_service

    for group_id in (None, "", 4, 10, 99):
        payload = _payload(12)
        warehouse_service.apply_image_limits(
            payload, sku_limit=9, group_id=group_id)
        colors = payload["skuPropertyList"][0]["attrValueList"]
        assert len(colors) == 9, f"group_id={group_id!r} 的颜色没被裁"
        assert len(payload["colorMap"]) == 9


def test_restricted_group_id_helper():
    """`is_restricted_group_id` 是不查库的快速判定。"""
    from services.group_access import is_restricted_group_id

    for value in (1, 2, 3, "1", " 2 ", "3"):
        assert is_restricted_group_id(value) is True, f"{value!r} 应判为受限"
    for value in (None, "", 0, 4, 10, "abc", "精品"):
        assert is_restricted_group_id(value) is False, f"{value!r} 不该判为受限"


def test_restricted_group_more_than_nine_is_normal():
    """超过 9 种颜色在受限分组里是正常的 —— 20 种也不裁。"""
    from services import warehouse_service

    payload = _payload(20)
    warehouse_service.apply_image_limits(payload, sku_limit=9, group_id=1)

    assert len(payload["skuPropertyList"][0]["attrValueList"]) == 20
    assert len(payload["colorMap"]) == 20
    assert len(payload["skuMap"]) == 40


def test_trim_color_map_script_skips_restricted_by_count():
    """清理脚本不能按「颜色数」去裁受限分组的 colorMap。

    受限组的 colorMap 条目多于 9 是正常的；脚本只该清掉混进来的尺码条目。
    """
    from scripts.trim_color_map import _SIZE_LIKE_RE

    # 这些是尺码，受限组里也该清（它们本来就不是颜色）
    for text in ("40", "45", "EUR33", "Saiz", "L(37-39)", "36-38"):
        assert _SIZE_LIKE_RE.match(text), f"{text} 应识别为尺码"

    # 这些是颜色，受限组里绝不能动
    for text in ("Hitam", "Putih", "Merah jambu", "Hijau Army", "Khaki",
                 "Hitam Merah", "Biru Langit"):
        assert not _SIZE_LIKE_RE.match(text), f"{text} 被误判成尺码了"