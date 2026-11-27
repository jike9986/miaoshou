"""草稿是否还有中文 —— 决定「还要不要按中文规则清洗 / 翻译」。

负责人口径（2026-11-27）：
  「我已经说了这些都是昨天晚上你自己翻译导致的，不是采集就是马来语」

也就是说草稿本来有中文，是**我们翻译完写回妙手**之后才变成马来语的。
第二天重新处理时草稿里一个汉字都没有，但流程仍从头跑中文颜色清洗，
拿中文词表切马来语：

    Warna putih  → Putih     丢掉 "Warna"，与草稿原文对不上
    Merah wain   → Merah     丢掉 "wain"（酒红），语义变了
    Beras hitam. → Hitam     丢掉 "Beras"（黑米）
    Meter        → 认不出，当厂家广告词直接删掉

提交后妙手按我们的规格重建规格表，读回核验拿草稿原文（`Warna putih`）比对
→ 名字对不上 → 退回按数量比 → 数量也少了 → 报 `skuImages` 不一致。
实测 3563343903 / 3563343858 / 3563343878 / 3563343842 全部如此。

`_payload_has_chinese` 就是这道闸：没有汉字就不再清洗、不再翻译。
"""
from __future__ import annotations

import pytest

from services.warehouse_service import _payload_has_chinese


def _payload(title="", colors=(), sizes=("35", "36"), attr_name="Warna"):
    return {
        "title": title,
        "skuPropertyList": [
            {"attrName": attr_name, "attrId": "100000",
             "attrValueList": [{"attrValue": c, "imgUrl": "https://x/a.jpg"}
                               for c in colors]},
            {"attrName": "Saiz", "attrId": "100007",
             "attrValueList": [{"attrValue": s, "imgUrl": ""} for s in sizes]},
        ],
    }


@pytest.mark.parametrize(("title", "colors"), [
    # 已经是马来语/英文：昨晚翻译后写回的样子
    ("Kasut tumit tinggi gaya untuk wanita, koleksi", ["Hitam", "Warna putih"]),
    ("2025 Kasut tumit tinggi berwarna merah wain", ["Merah wain"]),
    ("Kasut tumit tinggi berbulu hitam", ["Beras hitam."]),
    ("Kasut tumit tinggi tumit tebal", ["Meter", "Beras hitam."]),
    ("", ["Hitam"]),
])
def test_translated_payload_has_no_chinese(title, colors):
    """译文草稿一个汉字都没有 → 不该再清洗、再翻译。"""
    assert _payload_has_chinese(_payload(title, colors)) is False


@pytest.mark.parametrize(("title", "colors"), [
    ("2026新款女鞋细跟高跟鞋", ["黑色", "白色"]),
    ("红色高跟鞋", ["酒红色"]),
    ("黑色绒面高跟鞋", ["黑米色"]),
    # 中文夹杂型号：清洗要保留型号，仍属「有中文」
    ("女鞋", ["566【白绿色】"]),
    ("鞋", ["HD866米色"]),
    # 标题是中文、颜色是型号：只要有汉字就算
    ("高跟鞋", ["A19"]),
])
def test_chinese_payload_is_detected(title, colors):
    """中文草稿必须识别出来，正常清洗 + 翻译。"""
    assert _payload_has_chinese(_payload(title, colors)) is True


def test_title_alone_is_enough():
    """只有标题有中文、规格值全是型号，也算中文草稿。"""
    assert _payload_has_chinese(_payload("黑色高跟鞋", ["A19", "B20"])) is True


def test_color_values_alone_are_enough():
    """标题是型号、颜色有中文，同样算中文草稿。"""
    assert _payload_has_chinese(_payload("X-5", ["黑色", "白色"])) is True


def test_empty_payload_has_no_chinese():
    """空 payload 不该被当成中文，也不该抛。"""
    assert _payload_has_chinese({}) is False
    assert _payload_has_chinese({"title": "", "skuPropertyList": []}) is False


def test_non_mapping_is_safe():
    """脏数据不该让处理流程崩。"""
    assert _payload_has_chinese(None) is False  # type: ignore[arg-type]
    assert _payload_has_chinese([]) is False  # type: ignore[arg-type]


def test_malay_color_words_are_not_chinese():
    """马来语颜色词全是拉丁字母，不能误判成中文。"""
    malay = ["Hitam", "Putih", "Kelabu", "Merah", "Biru", "Hijau",
             "Kuning air", "Coklat", "Beras hitam.", "Merah wain"]
    assert _payload_has_chinese(_payload("", malay)) is False
