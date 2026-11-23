"""采集箱：listing_rows 解析和过滤测试。"""
import pytest

from core import errors
from services.collect_box_service import listing_rows

pytestmark = pytest.mark.unit

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

    def test_site_mode_is_kept(self):
        """editModel='site' 的行**照常返回**（2026-11-21 行为变更）。

        ## 为什么改了

        负责人报「这个账号的采集箱显示不出来」（截图：采集箱 / 49，
        列表空白，提示「当前筛选没有结果」）。

        根因就是这里：账号 8 的 49 条全是 `editModel='site'`（只采集到
        采集箱、还没分配店铺），旧代码把它们全丢掉 —— 列表空、
        而 `total` 用的是上游原始计数，于是显示「49」却一件不显示。

        现在 site 行照常返回，用 `site` 作店铺维度替代标识：
            key     = f"{detailId}:site:{site}"
            shopId  = ""（空串，前端据此显示「未分配店铺」）
            shopName= "未分配店铺（{site}）"

        真正该跳过的仍然跳过：`editModel` 既不是 shop 也不是 site 的行。
        """
        rows = listing_rows(_response([
            _row(1001, 2001, edit_model=None),
            _row(1002, 2002, edit_model="site"),
        ]))
        assert len(rows) == 2
        assert rows[0]["detailId"] == "1001"
        assert rows[1]["detailId"] == "1002"
        assert rows[1]["editModel"] == "site"
        # site 行的 shopId 必须是空串（不是 None）—— 前端靠它判断「未分配」
        assert rows[1]["shopId"] == ""

    def test_unknown_edit_model_still_filtered(self):
        """既不是 shop 也不是 site 的 editModel 仍然跳过。"""
        rows = listing_rows(_response([
            _row(1001, 2001, edit_model="shop"),
            _row(1002, 2002, edit_model="weird"),
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
