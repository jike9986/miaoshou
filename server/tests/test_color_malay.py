"""马来语颜色值必须原样保留「型号 + 颜色短语」整串。

实测（2026-11-09）：商品 3516326314 的颜色值是 `HD866 kuning air` / `HD866 hitam`，
上游规格表里存的就是这两整串。清洗链路曾把它们截成 `Kuning` / `Hitam`，
绑定的 SKU 值与上游对不上，妙手保存报「SKU【Kuning-41】不存在」，整件处理失败。
只截掉 `air`（浅）也一样失败：报「SKU【Kuning air-41】不存在」。
所以马来语这一支只去多余空白，不做任何截短。

中文颜色仍按老版口径清洗（去型号、留颜色+变体），见下面的对照用例。
"""
from __future__ import annotations

import pytest

from services import color_service


@pytest.mark.parametrize(("raw", "expected"), [
    ("HD866 kuning air", "HD866 kuning air"),
    ("HD866 hitam", "HD866 hitam"),
    ("kuning air", "kuning air"),
    ("hitam pekat", "hitam pekat"),
    ("merah jambu", "merah jambu"),
    ("biru muda", "biru muda"),
    ("  HD866   kuning air  ", "HD866 kuning air"),
])
def test_malay_color_value_is_kept_verbatim(raw, expected):
    assert color_service.clean_color_strict(raw) == expected


@pytest.mark.parametrize(("raw", "expected"), [
    ("HD866米色", "米色"),
    ("HD866黑色", "黑色"),
    ("黑色 加绒", "黑色加绒"),
    ("G22绿色", "绿色"),
    ("1987白色", "白色"),
])
def test_chinese_color_cleaning_is_unchanged(raw, expected):
    """中文口径是老版行为，修马来语那条分支不能顺手改它。"""
    assert color_service.clean_color_strict(raw) == expected


def test_malay_light_and_plain_variants_stay_distinct():
    """浅黄与黄必须是两个不同颜色值，否则 SKU 键撞在一起。"""
    assert (color_service.clean_color_strict("HD866 kuning air")
            != color_service.clean_color_strict("HD866 kuning"))
