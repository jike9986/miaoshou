"""成品仓库：纯函数测试（不需要数据库）。"""
import pytest

from services.warehouse_service import category_default_weight, category_package_spec


class TestCategoryDefaultWeight:
    def test_shoe_category(self):
        assert category_default_weight("运动鞋") == 0.6

    def test_rollerblade_category(self):
        assert category_default_weight("轮滑鞋") == 0.8

    def test_clothing_category(self):
        assert category_default_weight("运动裤") == 0.3

    def test_unknown_category(self):
        assert category_default_weight("其他商品") == 0.6

    def test_empty_category(self):
        assert category_default_weight("") == 0.6

    def test_none_category(self):
        assert category_default_weight(None) == 0.6


class TestCategoryPackageSpec:
    def test_rollerblade_has_larger_box(self):
        spec = category_package_spec("轮滑鞋")
        assert spec is not None
        assert spec["length"] >= 30
        assert spec["weightRange"][1] >= 2.0

    def test_shoes_spec(self):
        spec = category_package_spec("普通运动鞋")
        assert spec is not None
        assert spec["length"] == 33

    def test_unknown_returns_default(self):
        spec = category_package_spec("火箭推进器")
        assert spec is not None  # 返回通用默认规格，不返回 None

    def test_empty_returns_default(self):
        spec = category_package_spec("")
        assert spec is not None
