"""SKU 图读回核验：按「属性名+值名」定位，不用裸下标（2026-11-26）。

## 现场

11-21 有 45 件、11-26 有 1 件栽在 `mismatches=['skuImages']`：

    妙手草稿读回与处理结果不一致，已阻止后续发布

## 根因

`_sku_image_slots_match` 拿 `sku_image_refs` 的 `path` 列表逐项比，
而 path 是**裸下标**：

    skuPropertyList[0].attrValueList[0]

妙手读回时属性顺序可能调换（Warna/Saiz → Saiz/Warna），下标全错位，
于是「图明明都在」却被判失败。反过来，值顺序对调时下标不变，
「实际换了图」却被判通过 —— 两边都错。

## 改法

按 `属性名|值名` 定位，判据是「原来带图的规格值，读回后仍然带图」。
被截断掉的规格值不算（颜色上限 9 个是正常业务）。
"""
from __future__ import annotations

import pytest

from services import readback_check


def _payload(colors: list[tuple[str, str]], sizes: list[str] | None = None) -> dict:
    """colors = [(名, 图URL或空), ...]"""
    props = [{
        "attrName": "Warna",
        "attrId": "100000",
        "attrValueList": [{"attrValue": n, "imgUrl": u} for n, u in colors],
    }]
    if sizes is not None:
        props.append({
            "attrName": "Saiz",
            "attrId": "100007",
            "attrValueList": [{"attrValue": s, "imgUrl": ""} for s in sizes],
        })
    return {"skuPropertyList": props}


A = "http://x/a.jpg"
B = "http://x/b.jpg"


def test_attribute_order_swap_is_not_a_mismatch():
    """属性顺序被妙手调换，图都在 —— 不该报不一致。

    这是 11-21 那 45 件的成因。
    """
    normal = _payload([("Coklat", A), ("Hitam", B)], ["35", "36"])
    swapped = {"skuPropertyList": list(reversed(normal["skuPropertyList"]))}

    assert readback_check._sku_image_slots_match(normal, swapped) is True


def test_value_order_swap_keeps_images():
    """值顺序对调，图跟着值走 —— 图都还在，不该报不一致。"""
    v1 = _payload([("Coklat", A), ("Hitam", B)])
    v2 = _payload([("Hitam", B), ("Coklat", A)])

    assert readback_check._sku_image_slots_match(v1, v2) is True


def test_real_image_loss_is_detected():
    """真丢图必须报出来：原来带图的颜色，读回后空了。"""
    before = _payload([("Coklat", A), ("Hitam", B)])
    after = _payload([("Coklat", ""), ("Hitam", B)])

    assert readback_check._sku_image_slots_match(before, after) is False


def test_truncated_colors_are_not_a_mismatch():
    """颜色被截断（9 个上限）不是丢图。

    原来 20 色，截到 9 色 —— 留下的 9 个都还带图就算过。
    """
    before = _payload([(f"c{i}", A) for i in range(20)])
    after = _payload([(f"c{i}", A) for i in range(9)])

    assert readback_check._sku_image_slots_match(before, after) is True


def test_keeping_all_images_passes():
    """什么都没变。"""
    before = _payload([("Coklat", A), ("Hitam", B)], ["35"])
    assert readback_check._sku_image_slots_match(before, before) is True


def test_translation_renames_labels_still_matches_by_url():
    """颜色名被翻译（Coklat → Brown），但图还在 —— 不该判失败。

    按名字定位会对不上，所以名字对不上时要退回按 URL 集合判断。
    """
    before = _payload([("Coklat", A), ("Hitam", B)])
    after = _payload([("Brown", A), ("Black", B)])

    assert readback_check._sku_image_slots_match(before, after) is True


def test_both_empty_passes():
    """都没有 SKU 图（比如全部尺码类目）不算不一致。"""
    before = _payload([("Coklat", "")])
    after = _payload([("Coklat", "")])
    assert readback_check._sku_image_slots_match(before, after) is True


def test_refs_still_reports_paths_for_diagnostics():
    """`sku_image_refs` 仍要能给出位置信息，供日志排查用。"""
    payload = _payload([("Coklat", A), ("Hitam", B)], ["35"])
    refs = readback_check.sku_image_refs(payload)
    assert len(refs) == 2
    assert all(r["path"] for r in refs)
    assert A in [r["url"] for r in refs]