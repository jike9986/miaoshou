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
    # 马来模板界面没有关税
    assert "tariffChargePercent" not in fields


def test_thailand_has_ecommerce_growth_not_bcp():
    fields = site_charge_fields("TH")
    assert "eCommerceGrowthChargePercent" in fields
    assert "orderProcessCny" in fields
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
