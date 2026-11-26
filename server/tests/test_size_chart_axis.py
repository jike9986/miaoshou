"""尺码图判定：帽子/椅子不该套尺码图（负责人 2026-11-25）。

口径来源：
  「像帽子，户外椅帽子什么的都是不需要套尺码图的啊」

背景：原来 `_fill_claim_size_chart` 和入库时的 sizeChart 写入都是无条件的，
帽子、椅子、桌子的详情页也挂一张鞋码表。

第一次改的时候以为是「有没有尺码维度」的判据 —— 实测发现**帽子也有尺码维度**：

  帽  尺码 = ['均码'] / ['可调节'] / ['55-60cm(可调节)'] / ['58', '60']
  鞋  尺码 = ['36', '37'] / ['36-37', '38-39'] / ['24/25码', ...] / ['S','M','L']

所以真正的判据是「尺码值像不像鞋号/服装号」，而不是「有没有尺码维度」。
"""
import pytest

from services import warehouse_service


def _payload(dims: dict[str, list[str]]) -> dict:
    """构造 payload：dims = {维度名: [值...]}"""
    return {
        "skuPropertyList": [
            {"attrName": name,
             "attrValueList": [{"attrValue": v} for v in values]}
            for name, values in dims.items()
        ]
    }


@pytest.mark.parametrize("name,values", [
    ("帽子-均码", ["均码"]),
    ("帽子-可调节", ["可调节"]),
    ("帽子-可调节带cm", ["可调节(57-60cm）"]),
    ("帽子-头围区间", ["55-60cm(可调节)"]),
    ("椅子-只有颜色", []),
    ("水桶-容量升", ["12L", "20L"]),
    ("水桶-毫升", ["500ml", "750ml"]),
])
def test_no_size_chart_for_non_footwear(name, values):
    """这些商品不该套尺码图。"""
    payload = _payload({"颜色": ["黑色", "白色"]})
    if values:
        payload["skuPropertyList"].append({
            "attrName": "尺码",
            "attrValueList": [{"attrValue": v} for v in values],
        })
    assert warehouse_service._payload_needs_size_chart(payload) is False, name


@pytest.mark.parametrize("name,values", [
    ("鞋-单码", ["36", "37", "38"]),
    ("鞋-区间", ["36-37", "38-39", "40-41"]),
    ("鞋-带内长", ["36-37【内长235mm】", "38-39【内长245mm】"]),
    ("童鞋-斜杠码", ["24/25码", "26/27码", "28/29码"]),
    ("鞋-全码", ["36", "37", "38", "39", "40", "41", "42", "43", "44", "45"]),
    ("服装-字母码", ["S", "M", "L", "XL", "XXL"]),
])
def test_size_chart_for_footwear_and_apparel(name, values):
    """这些商品该套尺码图。"""
    payload = _payload({"颜色": ["黑色", "白色"], "尺码": values})
    assert warehouse_service._payload_needs_size_chart(payload) is True, name


def test_hat_with_numeric_head_circumference_uses_category():
    """帽子用纯数字标头围（58/60）时，靠类目名判，不靠尺码值。

    实测「户外迷彩战术帽定制 圆边帽定制」的尺码值是 ['58','60'] ——
    纯数字头围，和鞋号 58/60 完全无法区分。但它 cid=603654，
    本地类目表查到 name_cn='运动与户外帽'，类目名一锤定音。

    这是这批 229 件里唯一一件尺码值像鞋号的帽子，靠类目名才没误判。
    """
    payload = _payload({"颜色": ["黑色", "军绿"], "尺码": ["58", "60"]})
    # 只给类目名
    assert warehouse_service._payload_needs_size_chart(
        payload, "运动与户外帽") is False
    # 类目名在 payload 里也一样
    payload["categoryName"] = "运动与户外帽"
    assert warehouse_service._payload_needs_size_chart(payload) is False
    # 同样 58/60，但类目是鞋 → 需要
    assert warehouse_service._payload_needs_size_chart(
        payload, "运动鞋") is True


def test_numeric_size_without_category_falls_back_to_values():
    """没有类目名时只能按值判 —— 58/60 会被判成鞋号（可接受的误差）。"""
    payload = _payload({"颜色": ["黑色"], "尺码": ["58", "60"]})
    assert warehouse_service._payload_needs_size_chart(payload) is True


def test_category_fallback_when_no_sku_property():
    """没有 SKU 规格时按类目名兜底。"""
    assert warehouse_service._payload_needs_size_chart(
        {}, "运动鞋") is True
    assert warehouse_service._payload_needs_size_chart(
        {"categoryName": "运动服饰"}) is True
    assert warehouse_service._payload_needs_size_chart(
        {"categoryName": "棒球帽"}) is False
    assert warehouse_service._payload_needs_size_chart(
        {"categoryName": "户外折叠椅"}) is False


def test_category_beats_size_values_for_hats():
    """类目名优先于尺码值：帽子即使尺码是数字也不挂鞋码表。

    反面：鞋的类目 + 数字尺码 → 需要。
    """
    numeric = _payload({"尺码": ["40", "41", "42"]})
    assert warehouse_service._payload_needs_size_chart(
        dict(numeric), "运动与户外帽") is False
    assert warehouse_service._payload_needs_size_chart(
        dict(numeric), "男士运动鞋") is True


def test_real_hat_from_production():
    """生产真实数据：这顶帽子有尺码维度，值全是帽子写法 → 不套尺码图。"""
    payload = {
        "title": "军迷迷彩莽纹棒球网帽 特种兵战术迷彩帽",
        "skuPropertyList": [
            {"attrName": "颜色",
             "attrValueList": [{"attrValue": "绿色"}, {"attrValue": "卡其色"}]},
            {"attrName": "尺码",
             "attrValueList": [{"attrValue": "均码"}]},
        ],
    }
    assert warehouse_service._payload_needs_size_chart(payload) is False


def test_real_shoe_from_production():
    """生产真实数据：洞洞鞋的尺码是 36-37 区间 → 套尺码图。"""
    payload = {
        "title": "洞洞鞋男款夏季外穿2026新款",
        "skuPropertyList": [
            {"attrName": "颜色",
             "attrValueList": [{"attrValue": "白色"}, {"attrValue": "黑色"}]},
            {"attrName": "尺码",
             "attrValueList": [{"attrValue": "36-37"}, {"attrValue": "38-39"},
                               {"attrValue": "40-41"}]},
        ],
    }
    assert warehouse_service._payload_needs_size_chart(payload) is True


def test_real_chair_from_production():
    """生产真实数据：户外椅只有颜色维度 → 不套尺码图。"""
    payload = {
        "title": "户外折叠椅克米特椅便携式野餐桌椅",
        "skuPropertyList": [
            {"attrName": "颜色",
             "attrValueList": [{"attrValue": "低背月亮椅白色（常规款）"},
                               {"attrValue": "低背月亮椅黑色（常规款）"}]},
        ],
    }
    assert warehouse_service._payload_needs_size_chart(payload) is False


def test_claim_fill_clears_size_chart_for_hats():
    """认领时：没有尺码轴的商品，连历史 payload 里的尺码图也要清掉。

    仓库里 225 件的 payload 是旧逻辑无条件写进去的，帽子上那张鞋码表
    就是这么来的。不清的话会一直带着走进妙手。
    """
    from services import publish_service

    hat = {
        "skuPropertyList": [
            {"attrName": "尺码", "attrValueList": [{"attrValue": "均码"}]},
        ],
        "sizeChart": "https://aohuang888.com/tk-product-automation/size-chart.png",
        "sizeChartType": "image",
    }
    shoe = {
        "skuPropertyList": [
            {"attrName": "尺码",
             "attrValueList": [{"attrValue": "36-37"}, {"attrValue": "38-39"}]},
        ],
    }
    payloads = [hat, shoe]
    entries = [{"warehouseId": "1"}, {"warehouseId": "2"}]

    publish_service._fill_claim_size_chart(
        payloads, entries,
        "https://aohuang888.com/tk-product-automation/size-chart.png")

    assert hat["sizeChart"] == "", "帽子的尺码图没被清掉"
    assert shoe["sizeChart"] == (
        "https://aohuang888.com/tk-product-automation/size-chart.png")
    assert shoe["sizeChartType"] == "image"