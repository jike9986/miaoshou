"""定价模板：运费计算和售价解算测试。"""
from decimal import Decimal

import pytest

from services.price_template import (
    DEFAULT_FREIGHT_TABLE,
    DEFAULT_TEMPLATE,
    PriceTemplateError,
    cross_border_freight_myr,
    display_price,
    preview_price,
    normalize_template,
)

pytestmark = pytest.mark.unit

TEMPLATE = dict(DEFAULT_TEMPLATE)  # 马来站默认模板


# ---------------------------------------------------------------------------
# cross_border_freight_myr（运费计算）
# ---------------------------------------------------------------------------

class TestCrossBorderFreight:
    def test_first_weight_range(self):
        """包裹 ≤ 起重，只收起重价格。"""
        # 默认起重 30g，价格 0.18 MYR
        result = cross_border_freight_myr("0.03", TEMPLATE)
        assert result == Decimal("0.18")

    def test_exactly_first_weight(self):
        result = cross_border_freight_myr("0.030", TEMPLATE)
        assert result == Decimal("0.18")

    def test_continued_weight(self):
        """超过起重后按续重单位计费。"""
        # 起重 30g 0.18，续重 10g 0.12
        # 50g = 起重 30g + 续重 2×10g → 0.18 + 2×0.12 = 0.42
        result = cross_border_freight_myr("0.05", TEMPLATE)
        assert result == Decimal("0.42")

    def test_weight_rounding(self):
        """续重单位向上取整（35g → 1 单位续重）。"""
        # 35g = 起重 30g + 1×10g（5g 不足 10g 向上取整）
        result = cross_border_freight_myr("0.035", TEMPLATE)
        assert result == Decimal("0.30")

    def test_zero_weight_returns_first_rate(self):
        """0 重量被视为在起重范围内，返回起重价格。"""
        result = cross_border_freight_myr("0.001", TEMPLATE)
        assert result > 0  # 至少收起重费

    def test_large_weight(self):
        """1kg = 1000g，续重很多，价格明显更高。"""
        result = cross_border_freight_myr("1.0", TEMPLATE)
        assert result > Decimal("1.0")  # 肯定超过 1 MYR


# ---------------------------------------------------------------------------
# normalize_template
# ---------------------------------------------------------------------------

class TestNormalizeTemplate:
    def test_valid_template_passes(self):
        result = normalize_template(TEMPLATE)
        assert result["profitType"] == "bySalePriceProfit"
        assert isinstance(result["freightTable"], dict)

    def test_invalid_profit_raises(self):
        """所有费率加利润 ≥ 100% 时报错。"""
        bad = dict(TEMPLATE)
        bad["profitPercent"] = 100.0
        with pytest.raises(PriceTemplateError, match="利润率"):
            normalize_template(bad)

    def test_negative_freight_price_raises(self):
        bad = dict(TEMPLATE)
        bad["freightTable"] = dict(DEFAULT_FREIGHT_TABLE)
        bad["freightTable"]["firstWeightPrice"] = -1
        with pytest.raises(PriceTemplateError, match="价格不能为负"):
            normalize_template(bad)


# ---------------------------------------------------------------------------
# display_price（售价解算）
# ---------------------------------------------------------------------------

class TestDisplayPrice:
    def test_returns_positive_price(self):
        """同等条件下，售价必须高于采购价（有利润）。"""
        result = display_price(
            purchase_cny="65",
            weight_kg="0.6",
            template=TEMPLATE,
            rate="1.6393",
        )
        assert result["price"] > 0
        assert result["lines"]["profit"] > 0

    def test_zero_purchase_raises(self):
        with pytest.raises(PriceTemplateError, match="采购价"):
            display_price("0", "0.6", TEMPLATE, rate="1.6393")

    def test_zero_rate_raises(self):
        with pytest.raises(PriceTemplateError, match="汇率"):
            display_price("65", "0.6", TEMPLATE, rate="0")

    def test_heavier_package_costs_more(self):
        """更重的包裹运费更高，售价也应更高（其他条件相同）。"""
        r1 = display_price("65", "0.5", TEMPLATE, rate="1.6393")
        r2 = display_price("65", "1.0", TEMPLATE, rate="1.6393")
        assert r2["price"] > r1["price"]

    def test_higher_purchase_higher_price(self):
        """采购价越高，售价也应越高。"""
        r1 = display_price("50", "0.6", TEMPLATE, rate="1.6393")
        r2 = display_price("80", "0.6", TEMPLATE, rate="1.6393")
        assert r2["price"] > r1["price"]


class TestPreviewFollowsTemplate:
    """试算必须跟着模板参数走（负责人 2026-11-23）。

    「我切换定价模板，下面的数字都不会跟着变」——两个根因：
      1. 接口只按模板名读库，界面改完没保存就算旧值
      2. `_resolve_currency` 让 freightTable.currency 优先于站点，
         旧模板里那栏一律是 MYR，切到菲律宾模板仍按 MYR 汇率算
    """

    def test_profit_percent_changes_price(self):
        """同一份模板只改利润率，价格必须变。"""
        low = dict(DEFAULT_TEMPLATE); low.update({"name": "低", "site": "MY", "profitPercent": 10})
        high = dict(DEFAULT_TEMPLATE); high.update({"name": "高", "site": "MY", "profitPercent": 50})
        a = preview_price("35", "0.6", low)
        b = preview_price("35", "0.6", high)
        assert b["price"] > a["price"], "利润率提高，售价必须提高"

    def test_charge_rate_changes_price(self):
        """改手续费率也要反映到价格上。"""
        base = dict(DEFAULT_TEMPLATE); base.update({"name": "基准", "site": "MY"})
        raised = dict(base); raised["paymentChargePercent"] = 10.0
        a = preview_price("35", "0.6", base)
        b = preview_price("35", "0.6", raised)
        assert b["price"] != a["price"], "交易手续费变了，售价必须跟着变"

    def test_each_site_uses_its_own_currency(self):
        """五个站点各自用本币，价格量级必须不同（不能全按 MYR 算）。"""
        results = {}
        for site, expected in (("MY", "MYR"), ("PH", "PHP"),
                               ("TH", "THB"), ("VN", "VND"), ("SG", "SGD")):
            template = dict(DEFAULT_TEMPLATE)
            template.update({"name": site, "site": site})
            results[site] = preview_price("35", "0.6", template)
            assert results[site]["currency"] == expected, f"{site} 币种应为 {expected}"

        prices = {site: float(row["price"]) for site, row in results.items()}
        assert len(set(prices.values())) == len(prices), \
            f"各站点价格不该相同，实际：{prices}"
        # 越南盾面值最大，新加坡币最贵 → 数字最小
        assert prices["VN"] > prices["PH"] > prices["SG"]
