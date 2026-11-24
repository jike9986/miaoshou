"""每个站点只显示该国真实存在的费用项（负责人 2026-11-23）。

「定价模板这里每个国家的有些东西是没有的你不能全部弄出来」
"""
from __future__ import annotations

import pytest

from services.price_template import site_charge_fields

pytestmark = pytest.mark.unit


def test_malaysia_has_bcp_but_no_tariff():
    fields = site_charge_fields("MY")
    assert "bcpChargePercent" in fields
    assert "platformSupportCny" in fields
    assert "vatChargePercent" in fields
    assert "lastMileFreightServiceTaxPercent" in fields
    # 马来模板界面没有关税
    assert "tariffChargePercent" not in fields
    # 马来也没有「活动费率」（只有 BCP 活动费率，是两回事）
    assert "activityChargePercent" not in fields


def test_thailand_has_ecommerce_growth_not_bcp():
    fields = site_charge_fields("TH")
    assert "eCommerceGrowthChargePercent" in fields
    assert "orderProcessCny" in fields
    assert "tariffChargePercent" in fields
    assert "bcpChargePercent" not in fields


def test_vietnam_has_enterprise_withhold():
    fields = site_charge_fields("VN")
    assert "enterpriseWithholdPercent" in fields
    assert "bcpChargePercent" not in fields


def test_philippines_has_no_vat():
    """菲律宾没有 VAT，收的是包邮服务费 + 商家成长费。"""
    fields = site_charge_fields("PH")
    assert "vatChargePercent" not in fields
    assert "freeShippingChargePercent" in fields
    assert "eCommerceGrowthChargePercent" in fields


def test_singapore_uses_consumption_tax():
    fields = site_charge_fields("SG")
    assert "consumptionTaxChargePercent" in fields
    assert "vatChargePercent" not in fields


def test_common_fields_always_present():
    """公共项（利润、运费、佣金、手续费…）每个站点都得有。"""
    for site in ("MY", "TH", "VN", "PH", "SG", "ID"):
        fields = site_charge_fields(site)
        for common in ("profitPercent", "lastMileFreightCny",
                       "platformCommissionPercent", "paymentChargePercent",
                       "withdrawChargePercent"):
            assert common in fields, f"{site} 缺公共项 {common}"


def test_unknown_site_falls_back_to_common_only():
    """未知站点只给公共项，不把 22 项全摊开。"""
    fields = site_charge_fields("XX")
    assert "profitPercent" in fields
    assert "tariffChargePercent" not in fields
    assert "bcpChargePercent" not in fields


def test_field_list_is_shorter_than_full_template():
    """每个站点都应当比全量字段少 —— 全列出来就是要修的问题。"""
    for site in ("MY", "TH", "VN", "PH", "SG"):
        assert len(site_charge_fields(site)) < 25


class TestSeededTemplateRatesPerSite:
    """每人首次拿到的那套默认模板，费率必须是该国真实值。

    负责人 2026-11-23：「泰国增值税率不是 7 吗」「马来没有活动费率 % 这个啊
    能不能仔细核对啊」。原来五国都套 `DEFAULT_TEMPLATE`（马来口径），
    于是泰国 VAT 是 10、泰国挂着 BCP、菲律宾挂着增值税。
    """

    def test_thailand_vat_is_seven(self):
        from services import setting_service
        seed = setting_service._seed_templates()
        assert seed["泰国"]["vatChargePercent"] == 7.0

    def test_malaysia_has_no_activity_rate(self):
        from services import setting_service
        seed = setting_service._seed_templates()
        assert not seed["马来"].get("activityChargePercent")

    def test_philippines_has_no_vat(self):
        from services import setting_service
        seed = setting_service._seed_templates()
        assert not seed["菲律宾"].get("vatChargePercent")

    def test_singapore_uses_consumption_tax(self):
        from services import setting_service
        seed = setting_service._seed_templates()
        sg = seed["新加坡"]
        assert sg["consumptionTaxChargePercent"] == 9.0
        assert not sg.get("vatChargePercent")

    def test_thailand_has_no_bcp(self):
        from services import setting_service
        seed = setting_service._seed_templates()
        assert not seed["泰国"].get("bcpChargePercent")

    def test_each_country_keeps_its_own_trading_fee(self):
        """交易手续费率各国不同，不是全都 3.78。"""
        from services import setting_service
        seed = setting_service._seed_templates()
        assert seed["马来"]["paymentChargePercent"] == 3.78
        assert seed["泰国"]["paymentChargePercent"] == 3.21
        assert seed["越南"]["paymentChargePercent"] == 6.0
        assert seed["菲律宾"]["paymentChargePercent"] == 2.24
        assert seed["新加坡"]["paymentChargePercent"] == 3.27

    def test_normalize_does_not_inherit_other_site_rates(self):
        """按站点归一化时，该国没有的费率项不能从马来默认值抄过来。"""
        from services.price_template import normalize_template
        th = normalize_template({"name": "t", "site": "TH", "profitPercent": 15})
        assert not th.get("bcpChargePercent"), "泰国没有 BCP 活动费"
        ph = normalize_template({"name": "p", "site": "PH", "profitPercent": 15})
        assert not ph.get("vatChargePercent"), "菲律宾没有增值税"


class TestOtherChargeCountedOnce:
    """其他费用只加一次（负责人 2026-11-23 担心重复计入）。"""

    def test_increase_matches_the_entered_amount(self):
        from services.price_template import DEFAULT_TEMPLATE, preview_price
        base = dict(DEFAULT_TEMPLATE)
        base.update({"name": "x", "site": "MY", "otherChargeCny": 0})
        one = dict(base); one["otherChargeCny"] = 1
        a = preview_price("35", "0.6", base)
        b = preview_price("35", "0.6", one)
        # 其他费用那一行只应增加「1 CNY 等值本币」
        assert abs(b["lines"]["otherCharge"] - a["lines"]["otherCharge"]
                   - 1 / a["rate"]) < 0.01

    def test_blank_and_zero_are_the_same(self):
        from services.price_template import DEFAULT_TEMPLATE, preview_price
        blank = dict(DEFAULT_TEMPLATE)
        blank.update({"name": "x", "site": "MY", "otherChargeCny": None})
        zero = dict(blank); zero["otherChargeCny"] = 0
        assert preview_price("35", "0.6", blank)["price"] == \
            preview_price("35", "0.6", zero)["price"]
