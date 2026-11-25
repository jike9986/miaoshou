"""仓库→采集箱 规格与价格的回归测试。

这两条是 2026-11-05 线上 70 件认领里踩出来的，必须钉死：

1. skuMap 的 key 各段是**按 ID 字符串升序**排列的，段位置与 skuPropertyList
   的维度顺序无关。曾经按位置校验维度（`part in our_dim_ids[pos]`），
   尺码 id 小于颜色 id 时（如 White=25a81701fb / 尺码=1c383cd30b）就会把
   尺码值当颜色值，妙手报「SKU【White-35】不存在」。
2. 售价低于妙手报的基础成本时，要按它给的准确成本线上调后重试。
"""
from __future__ import annotations

from decimal import Decimal

from services import carrier_service
from services.miaoshou import MiaoshouError


def _payload() -> dict:
    """两色六码，颜色 id 全部大于尺码 id —— 正是触发位置错判的形状。"""
    return {
        "cid": "834696",
        "title": "Kasut sukan",
        "skuPropertyList": [
            {
                "attrName": "Warna",
                "attrValueList": [
                    {"attrValueId": "25a81701fb", "attrValue": "White"},
                    {"attrValueId": "b76f3315f5", "attrValue": "Black"},
                ],
            },
            {
                "attrName": "Saiz",
                "attrValueList": [
                    {"attrValueId": "1c383cd30b", "attrValue": "35"},
                    {"attrValueId": "19ca14e7ea", "attrValue": "36"},
                    {"attrValueId": "a5bfc9e079", "attrValue": "37"},
                ],
            },
        ],
        "skuMap": {
            # 尺码段在前：因为 1c38... < 25a8...
            ";1c383cd30b;25a81701fb;": {"priceIncludeVat": 20, "isDelete": "0"},
            ";19ca14e7ea;25a81701fb;": {"priceIncludeVat": 20, "isDelete": "0"},
            ";25a81701fb;a5bfc9e079;": {"priceIncludeVat": 20, "isDelete": "0"},
            ";1c383cd30b;b76f3315f5;": {"priceIncludeVat": 20, "isDelete": "0"},
            ";19ca14e7ea;b76f3315f5;": {"priceIncludeVat": 20, "isDelete": "0"},
            ";a5bfc9e079;b76f3315f5;": {"priceIncludeVat": 20, "isDelete": "0"},
        },
    }


def _carrier_info() -> dict:
    return {
        "shopCollectItemInfo": {
            "skuPropertyList": [],
            "_colorAttrId": "100000",
            "_colorAttrName": "Warna",
            "_sizeAttrId": "100007",
            "_sizeAttrName": "Saiz",
        },
        "_colorAttrId": "100000",
        "_colorAttrName": "Warna",
        "_sizeAttrId": "100007",
        "_sizeAttrName": "Saiz",
    }


def test_sku_key_segments_are_matched_by_dimension_not_position():
    data, notes = carrier_service.normalize_spec_for_save(_payload(), _carrier_info())
    assert len(data["skuMap"]) == 6, notes
    dims = {prop["attrId"]: {av["attrValueId"] for av in prop["attrValueList"]}
            for prop in data["skuPropertyList"]}
    assert dims == {
        "100000": {"25a81701fb", "b76f3315f5"},
        "100007": {"1c383cd30b", "19ca14e7ea", "a5bfc9e079"},
    }
    # 每条 key 都是升序、首尾带分号、每个维度恰好出现一次
    for key in data["skuMap"]:
        parts = [x for x in key.split(";") if x]
        assert key.startswith(";") and key.endswith(";")
        assert parts == sorted(parts)
        assert len(parts) == 2


def test_same_dimension_value_twice_is_rejected_not_silently_merged():
    payload = _payload()
    # 造一条颜色+颜色（没有尺码）的非法 key
    payload["skuMap"] = {";25a81701fb;b76f3315f5;": {"priceIncludeVat": 20}}
    data, _notes = carrier_service.normalize_spec_for_save(payload, _carrier_info())
    assert data["skuMap"] == {}


def test_duplicate_labels_are_deduplicated_and_keys_remapped():
    payload = _payload()
    payload["skuPropertyList"][0]["attrValueList"] = [
        {"attrValueId": "25a81701fb", "attrValue": "White"},
        {"attrValueId": "b76f3315f5", "attrValue": "white"},  # 仅大小写不同
    ]
    data, notes = carrier_service.normalize_spec_for_save(payload, _carrier_info())
    colors = [av["attrValue"] for av in data["skuPropertyList"][0]["attrValueList"]]
    assert len(colors) == 1
    assert any("规格SKU规范化" in note for note in notes)


def test_raise_to_cost_floor_bumps_purchase_price_then_clears_floor():
    """先按拿货价微微上挑；还不够就抬到成本线 + 0.01，刚好不报错。

    负责人口径（2026-11-25）：
      「只需要微微上挑让他不报成本不够就行了」
    """
    from services import carrier_service
    from scripts.claim_fast import _raise_to_cost_floor

    # 1 CNY = 2 MYR（钉住汇率）
    import time

    from services import exchange_rate

    exchange_rate.reset_cache()
    exchange_rate._cache["MYR"] = {
        "rate": Decimal("2"), "at": time.time(),
        "source": "test", "failedAt": None}
    try:
        body = {
            "site": "MY",
            "skuMap": {
                # 拿货 10 CNY → 上挑 5.90 MYR，成本线 12.87 → 抬到 12.88
                ";a;b;": {"price": "1.00", "priceIncludeVat": "1.00",
                          "originPrice": "10", "isDelete": "0"},
                # 拿货 30 CNY → 上挑 17.70 MYR，已高于成本线 → 保持 17.70
                ";a;c;": {"price": "2.00", "priceIncludeVat": "2.00",
                          "originPrice": "30", "isDelete": "0"},
                # 删除态：不动
                ";a;d;": {"price": "1.00", "priceIncludeVat": "1.00",
                          "originPrice": "40", "isDelete": "1"},
            },
        }
        message = ("产品设置的本地展示价1.00MYR，无法覆盖产品基础成本"
                   "(跨境物流运费+关税+增值税)：12.87MYR，售卖可能导致亏损")
        raised = _raise_to_cost_floor(body, message)
    finally:
        exchange_rate.reset_cache()

    # 低于成本线的抬到 12.87 + 0.01
    assert body["skuMap"][";a;b;"]["priceIncludeVat"] == "12.88"
    # 已经很够的保持自己的上挑值
    assert body["skuMap"][";a;c;"]["priceIncludeVat"] == "17.70"
    # 删除态不动
    assert body["skuMap"][";a;d;"]["priceIncludeVat"] == "1.00"

    # 抬价幅度只有 0.01，不是「成本线 + 30%」那种过冲
    assert Decimal(body["skuMap"][";a;b;"]["priceIncludeVat"]) == Decimal("12.88")

    # 和客户端函数同一系数
    assert carrier_service.CLAIM_PRICE_BUMP == Decimal("1.18")


def test_raise_to_cost_floor_never_lowers_price():
    """只往上挑，绝不把高价商品压回去。"""
    import time

    from services import exchange_rate
    from scripts.claim_fast import _raise_to_cost_floor

    exchange_rate.reset_cache()
    exchange_rate._cache["MYR"] = {
        "rate": Decimal("2"), "at": time.time(),
        "source": "test", "failedAt": None}
    try:
        body = {
            "site": "MY",
            "skuMap": {
                # 目标 5.90，当前 99.00 → 保持 99.00
                ";high;": {"price": "99.00", "priceIncludeVat": "99.00",
                           "originPrice": "10", "isDelete": "0"},
            },
        }
        raised = _raise_to_cost_floor(body, "无法覆盖产品基础成本 12.87MYR")
    finally:
        exchange_rate.reset_cache()

    assert body["skuMap"][";high;"]["priceIncludeVat"] == "99.00"
    assert raised is None, raised


def test_raise_to_cost_floor_without_floor_only_bumps():
    """妙手没报成本线时，就只做微微上挑。"""
    import time

    from services import exchange_rate
    from scripts.claim_fast import _raise_to_cost_floor

    exchange_rate.reset_cache()
    exchange_rate._cache["MYR"] = {
        "rate": Decimal("2"), "at": time.time(),
        "source": "test", "failedAt": None}
    try:
        body = {
            "site": "MY",
            "skuMap": {
                ";a;": {"price": "1.00", "priceIncludeVat": "1.00",
                        "originPrice": "10", "isDelete": "0"},
            },
        }
        raised = _raise_to_cost_floor(body, "无法覆盖产品基础成本")
    finally:
        exchange_rate.reset_cache()

    # 10 CNY × 1.18 ÷ 2 = 5.90 MYR
    assert body["skuMap"][";a;"]["priceIncludeVat"] == "5.90"
    assert raised == "5.90", raised


def test_raise_to_cost_floor_returns_none_without_floor_in_message():
    from scripts.claim_fast import _raise_to_cost_floor

    body = {"skuMap": {";a;b;": {"priceIncludeVat": "5", "isDelete": "0"}}}
    assert _raise_to_cost_floor(body, "妙手接口失败 [fail]：SKU【White-35】不存在") is None


def test_fill_carriers_is_the_same_standard_as_the_batch_script():
    """客户端走 fill_carriers，脚本走 claim_fast，两条链路必须同一口径。

    这条用例把两边最容易分叉的三件事钉在一起：
      1. ossMd5 冲突要重试多轮，不是一次
      2. 规格 key 按维度校验（不发「SKU 不存在」）
      3. 成本线上调后仍能保存成功
    """
    class FlakyClient:
        """前 3 次 save_shop 报 md5 变动，第 4 次成功。"""

        def __init__(self):
            self.save_attempts = 0
            self.sent_keys: list[list[str]] = []

        def shop_detail(self, *_args):
            return {"data": {"ossMd5": f"v{self.save_attempts}",
                             "shopCollectItemInfo": {}}}

        def category_tree(self, _site):
            return {"data": {"cateTree": {}}}

        def category_metadata(self, *_args, **_kwargs):
            return {"data": {"categorySaleAttrList": [
                {"attrId": "100000", "attrName": "Warna"},
                {"attrId": "100007", "attrName": "Saiz"}]}}

        def save_shop(self, _detail_id, _shop_id, _md5, body):
            self.save_attempts += 1
            self.sent_keys.append(sorted(body["skuMap"]))
            if self.save_attempts <= 3:
                raise MiaoshouError(
                    "您编辑过程中产品数据发生变动，为避免覆盖其它重要修改，请重新打开弹窗编辑",
                    payload={"message": "您编辑过程中产品数据发生变动"})

    client = FlakyClient()
    payload = _payload()
    result = carrier_service.fill_carriers(
        client, [1], [payload], shop_id=9, warehouse_id="w1",
        verify_readback=False)

    assert result["saved"] == [1], result["failures"]
    assert client.save_attempts == 4
    # 每次提交的 key 都是升序、每个维度各一次
    for keys in client.sent_keys:
        for key in keys:
            parts = [x for x in key.split(";") if x]
            assert parts == sorted(parts)
            assert len(parts) == 2


def test_version_refresh_is_bounded_and_reported_as_failure():
    class AlwaysStale:
        def shop_detail(self, *_args):
            return {"data": {"ossMd5": "stale", "shopCollectItemInfo": {}}}

        def category_tree(self, _site):
            return {"data": {"cateTree": {}}}

        def category_metadata(self, *_args, **_kwargs):
            return {"data": {"categorySaleAttrList": [
                {"attrId": "100000", "attrName": "Warna"},
                {"attrId": "100007", "attrName": "Saiz"}]}}

        def save_shop(self, *_args):
            raise MiaoshouError("您编辑过程中产品数据发生变动",
                                payload={"message": "您编辑过程中产品数据发生变动"})

    result = carrier_service.fill_carriers(
        AlwaysStale(), [1], [_payload()], shop_id=9, warehouse_id="w1",
        verify_readback=False)

    assert result["saved"] == []
    assert carrier_service.MAX_VERSION_REFRESH >= 3
    assert result["failureDetails"][0]["certain"] is True
