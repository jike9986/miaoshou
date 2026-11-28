"""颜色清洗只在**颜色值本身是中文**时才跑（2026-11-27）。

负责人口径：
  「为什么会有这个东西，这个链接明明之前就被翻译了，那就不要处理文字不就行了吗」
  「不是已经让你加了文字/图片是否处理的标记吗」

## 为什么标记解决不了

标记是**缓存**，语言检测才是**事实**。标记上线之前就已经翻译过的存量草稿
一件标记都没有（昨晚翻译的那批全在这类），第一次走到清洗时 `text_done`
必然是 False —— 只能靠语言检测兜住，兜住后立刻补写标记（自愈）。

## 为什么不能拿标题当判据

标题是中文、颜色却已经是马来语的组合真实存在（分阶段处理）。
拿标题判断会让这类商品回到清洗，把马来语颜色改坏：

    Beras hitam.  → 只剩 hitam → 拼成 "Redmi"
    Beras hitam.  → Hitam     丢掉「Beras」（黑米）
    Warna putih   → Putih     丢掉「Warna」
    Merah wain    → Merah     丢掉「wain」（酒红）
    Meter         → 认不出，当厂家广告词直接删掉

洗坏的草稿写回妙手后，读回核验拿提交前草稿当基准，名字/数量对不上
→ 恒报 `skuImages` 不一致，商品永远进不了仓（实测 3563339780 / 3563339725 /
3563339835 / 3563339868 全部卡死，其中 3563339780 的颜色被洗成了 "Redmi"）。

所以判据下沉到**颜色值自己的语言**：任一颜色值含汉字才清洗。
"""
from __future__ import annotations

import pytest

from services.warehouse_service import (
    _colors_need_chinese_cleaning,
    _payload_has_chinese,
)


def _payload(title: str, colors: list[str], attr: str = "Warna") -> dict:
    return {
        "title": title,
        "skuPropertyList": [
            {"attrName": attr, "attrId": "100000",
             "attrValueList": [{"attrValue": c} for c in colors]},
            {"attrName": "Saiz", "attrId": "100007",
             "attrValueList": [{"attrValue": "35"}, {"attrValue": "36"}]},
        ],
    }


@pytest.mark.parametrize("colors", [
    ["Beras hitam.", "Meter"],
    ["Beras hitam.", "Beras berwarna merah wain."],
    ["Hitam", "Warna putih"],
    ["Merah wain"],
    ["Hitam Merah", "Kuning"],
    ["Redmi berwarna hitam.", "Redmi"],   # 已经被洗坏的形态，也不该再动
    ["Black", "White"],
    ["A19", "B20"],
])
def test_malay_or_english_colors_are_not_cleaned(colors):
    """外文颜色值不清洗 —— 洗只会改坏。"""
    assert _colors_need_chinese_cleaning(_payload("Kasut tumit tinggi", colors)) is False


@pytest.mark.parametrize("colors", [
    ["黑色", "白色"],
    ["酒红色"],
    ["米白", "米黄"],
    ["566【白绿色】"],
    ["HD866米色"],
    ["黑色加绒"],
])
def test_chinese_colors_are_cleaned(colors):
    """中文颜色值照常清洗（去型号、归一、去重）。"""
    assert _colors_need_chinese_cleaning(_payload("高跟鞋", colors, attr="颜色")) is True


def test_chinese_title_does_not_trigger_color_cleaning():
    """标题是中文、颜色已是马来语 → **不清洗**。

    这是关键场景：拿标题当判据会让这类商品回到清洗，把颜色改坏。
    """
    payload = _payload("高跟鞋女鞋", ["Beras hitam.", "Meter"])
    assert _colors_need_chinese_cleaning(payload) is False
    # 但标题确实有中文，翻译那一步仍应照常做
    assert _payload_has_chinese(payload) is True


def test_translated_payload_needs_neither_cleaning_nor_translation():
    """整件都已译好：既不清洗，也不翻译。"""
    payload = _payload("Kasut tumit tinggi", ["Beras hitam.", "Meter"])
    assert _colors_need_chinese_cleaning(payload) is False
    assert _payload_has_chinese(payload) is False


def test_mixed_colors_with_one_chinese_triggers_cleaning():
    """混合值里只要有一个中文就照常清洗 —— 宁可洗，不可漏。"""
    assert _colors_need_chinese_cleaning(_payload("鞋", ["黑色", "Hitam"])) is True


def test_size_values_do_not_affect_decision():
    """尺码值（纯数字）不参与颜色判断。"""
    payload = _payload("Kasut", ["Hitam"])
    payload["skuPropertyList"][1]["attrValueList"] = [
        {"attrValue": "35"}, {"attrValue": "36码建议拍大一码"}]
    assert _colors_need_chinese_cleaning(payload) is False


def test_missing_or_empty_payload_is_safe():
    """脏数据不该让处理流程崩。"""
    assert _colors_need_chinese_cleaning({}) is False
    assert _colors_need_chinese_cleaning(None) is False  # type: ignore[arg-type]
    assert _colors_need_chinese_cleaning(
        {"skuPropertyList": [{"attrValueList": [{"attrValue": ""}]}]}) is False