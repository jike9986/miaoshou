"""把 ``_meas/`` 里的高价值探针搬进 pytest：一条用例 = 一个探针。

为什么是「搬运」不是「重写」：这些探针都已经跑出 0 FAIL，是真实踩过坑的
回归资产（模板可用性 105 条、批次 130 条、参考数据 85 条……）。重写成 pytest
风格只能让它们从「已验证」变成「刚写的」，没有任何收益。

用 ``pytest.param(..., id=...)`` 给每条一个短 id，是为了失败时一眼看出挂的是
哪一块业务，而不是 ``test_probe[probe_xxx]`` 这种要回去查的编号。

``probe_imgtest.py`` 刻意不在列表里：它不设 ``AOHUANG_DATA_DIR``，会直接写真实
``app.db``。保持原样，不进 CI（pyproject 里也排除了 ``_meas/``）。
"""
from __future__ import annotations

import pytest

# (探针模块名, pytest id) —— id 用业务名，不用文件名
PROBES = [
    pytest.param("probe_bootstrap", id="bootstrap"),
    pytest.param("probe_confirm_save", id="confirm-save"),
    pytest.param("probe_release", id="release"),
    pytest.param("probe_tpl_available", id="tpl-available"),
    pytest.param("probe_price_sync", id="price-sync"),
    pytest.param("probe_color", id="color"),
    pytest.param("probe_gate", id="gate"),
    pytest.param("probe_category", id="category"),
    pytest.param("probe_batch", id="batch"),
    pytest.param("probe_images_patch", id="images-patch"),
    pytest.param("probe_custom_cat", id="custom-cat"),
    pytest.param("probe_public_url", id="public-url"),
    pytest.param("probe_lang_verify", id="lang-verify"),
    pytest.param("probe_reference", id="reference"),
    pytest.param("probe_size_chart", id="size-chart"),
    pytest.param("probe_building", id="building"),
    pytest.param("test_pre_save", id="pre-save"),
    pytest.param("probe_wire3", id="wire3"),
    pytest.param("probe_img_apply", id="img-apply"),
    pytest.param("probe_img_wire", id="img-wire"),
]


@pytest.mark.legacy
@pytest.mark.parametrize("probe", PROBES)
def test_probe(probe: str, probe_runner) -> None:
    """跑一个 ``_meas`` 探针；退出码非 0 时失败信息里直接列出挂掉的断言。"""
    probe_runner(probe)
