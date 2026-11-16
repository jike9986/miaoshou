"""采集箱：listing_rows 解析和过滤测试。"""
import pytest

from services.collect_box_service import listing_rows
from core import errors


def _response(rows):
    """构造最简的 collect_box_list 响应体。"""
    return {"data": {"detailList": rows}}


def _row(detail_id=1001, shop_id=2001, title="Test", edit_model=None):
    return {
        "collectBoxDetailId": detail_id,
        "collectBoxDetailShopList": [{"shopId": shop_id, "shopName": "TestShop"}],
        "title": title,
        "editModel": edit_model,
        "mainImgUrl": "https://example.com/img.jpg",
    }


# ---------------------------------------------------------------------------
# 基础解析
# ---------------------------------------------------------------------------

class TestListingRows:
    def test_basic_row(self):
        rows = listing_rows(_response([_row()]))
        assert len(rows) == 1
        assert rows[0]["detailId"] == "1001"
        assert rows[0]["shopId"] == "2001"
        assert rows[0]["title"] == "Test"

    def test_site_mode_filtered_out(self):
        """editModel='site' 的行被跳过。"""
        rows = listing_rows(_response([
            _row(1001, 2001, edit_model=None),
            _row(1002, 2002, edit_model="site"),
        ]))
        assert len(rows) == 1
        assert rows[0]["detailId"] == "1001"

    def test_shop_mode_included(self):
        """editModel='shop' 的行正常返回。"""
        rows = listing_rows(_response([_row(edit_model="shop")]))
        assert len(rows) == 1

    def test_missing_detail_list_raises(self):
        """detailList 缺失时抛上游错误。"""
        with pytest.raises(errors.AppError) as exc_info:
            listing_rows({"data": {}})
        assert "detailList" in str(exc_info.value.message).lower() \
            or "detailList_missing" in str(exc_info.value.details or "")

    def test_dedup_same_key(self):
        """同一 detailId+shopId 组合只保留一条。"""
        rows = listing_rows(_response([_row(1001, 2001), _row(1001, 2001)]))
        assert len(rows) == 1

    def test_multiple_shops_per_row(self):
        """一个 detailId 绑多个店铺，各自展开成独立行。"""
        row = {
            "collectBoxDetailId": 1001,
            "collectBoxDetailShopList": [
                {"shopId": 2001, "shopName": "Shop A"},
                {"shopId": 2002, "shopName": "Shop B"},
            ],
            "title": "Multi-shop",
            "editModel": None,
            "mainImgUrl": "",
        }
        rows = listing_rows(_response([row]))
        assert len(rows) == 2
        shop_ids = {r["shopId"] for r in rows}
        assert shop_ids == {"2001", "2002"}

    def test_non_numeric_id_skipped(self):
        """无法转为整数的 detailId / shopId 跳过，不报错。"""
        row = {
            "collectBoxDetailId": "not-a-number",
            "collectBoxDetailShopList": [{"shopId": 2001}],
            "title": "Bad ID",
            "editModel": None,
        }
        rows = listing_rows(_response([row]))
        assert rows == []

    def test_empty_detail_list(self):
        rows = listing_rows(_response([]))
        assert rows == []
