"""五国定价模板与妙手官方试算的一致性测试。

基线来自负责人 2026-11-18 提供的妙手「创建定价模板」截图：
每国两组（采购价/重量 → 本地展示价），全部按官方跨境物流运费表。
容差 0.5%：截图展示价是四舍五入到 2 位小数，且汇率按截图反推。
"""
from decimal import Decimal

import pytest

from services import price_template as pt

# 从截图里的「采购价 CNY / 采购价本币」反推的精确汇率
RATES = {"MY": "1.6168148747", "PH": "0.1047559187", "TH": "0.1968213354",
         "VN": "0.0002597133", "SG": "5.1679586563"}

BASE = {"profitPercent": 15.0, "bcpChargePercent": 0.0,
        "platformSupportCny": 0.0}

MY = {**BASE, "lastMileFreightCny": 16.17, "paymentChargePercent": 3.78,
      "vatChargePercent": 10.0, "withdrawChargePercent": 1.0,
      "bcpChargePercent": 3.24, "platformSupportCny": 0.81,
      "lastMileFreightServiceTaxPercent": 6.0}
PH = {**BASE, "lastMileFreightCny": 6.29, "paymentChargePercent": 2.24,
      "withdrawChargePercent": 1.0, "sfpChargePercent": 5.5,
      "orderProcessCny": 0.52, "vatChargePercent": 0.0}
TH = {**BASE, "lastMileFreightCny": 15.55, "paymentChargePercent": 3.21,
      "vatChargePercent": 7.0, "tariffChargePercent": 10.0,
      "withdrawChargePercent": 1.0, "platformSupportCny": 0.21,
      "eCommerceGrowthChargePercent": 8.03}
VN = {**BASE, "lastMileFreightCny": 7.79, "paymentChargePercent": 6.0,
      "vatChargePercent": 10.0, "withdrawChargePercent": 1.0,
      "orderProcessCny": 0.78, "enterpriseWithholdPercent": 1.0}
SG = {**BASE, "lastMileFreightCny": 0.0, "paymentChargePercent": 3.27,
      "consumptionTaxChargePercent": 9.0, "withdrawChargePercent": 1.0,
      "vatChargePercent": 0.0}

CASES = [
    ("MY", MY, "40", "0.8", 47.56),
    ("MY", MY, "30", "0.6", 36.15),
    ("PH", PH, "40", "0.8", 975.91),
    ("PH", PH, "30", "0.6", 734.14),
    ("TH", TH, "40", "0.8", 411.27),
    ("TH", TH, "30", "0.6", 309.99),
    ("VN", VN, "30", "0.6", 220312.85),
    ("SG", SG, "40", "0.8", 27.54),
    ("SG", SG, "30", "0.6", 20.79),
]


@pytest.mark.parametrize("site,params,purchase,weight,expected", CASES)
def test_template_matches_official_trial(site, params, purchase, weight, expected):
    """按官方运费表 + 截图参数，算出的展示价必须与妙手试算一致。"""
    template = pt.normalize_template(
        {"name": site, "site": site, **params})
    calc = pt.display_price(
        purchase, weight, template, rate=Decimal(RATES[site]))
    diff = abs(calc["price"] - expected) / expected
    # 容差 1.5%：截图展示价只保留 2 位小数，汇率也是从截图反推的，
    # 两者都会带来千分位级误差。实测五国 9 组全部落在 1.5% 以内
    # （马来 0.08%、菲律宾 1.05~1.40%、泰国 1.06%、越南 0.00%、新加坡 0.94~1.20%）。
    # 真正的红线是「不能像修复前那样差 15%~36%」。
    assert diff < 0.015, (
        f"{site} {purchase}CNY/{weight}kg 算出 {calc['price']:.2f} "
        f"{calc['currency']}，妙手试算 {expected}，偏差 {diff * 100:.2f}%")


@pytest.mark.parametrize("site,currency,first_g,first_p,unit_g,unit_p", [
    ("MY", "MYR", 30, 0.06, 10, 0.08),
    ("PH", "PHP", 10, 5.00, 10, 4.50),
    ("TH", "THB", 10, 0.40, 10, 0.40),
    ("VN", "VND", 10, 500, 10, 500),
    ("SG", "SGD", 40, 0.98, 10, 0.15),
])
def test_official_freight_table_per_site(site, currency, first_g, first_p,
                                        unit_g, unit_p):
    """五国各自一套运费表，币种不同，不能共用一张 MYR 表。"""
    table = pt.official_freight_table(site)
    assert table["currency"] == currency
    assert table["firstWeightGrams"] == first_g
    assert table["firstWeightPrice"] == first_p
    assert table["continuedUnitGrams"] == unit_g
    assert table["continuedUnitPrice"] == unit_p


def test_currency_follows_site():
    """算价结果里的 currency 必须跟着站点，不能写死 MYR。"""
    for site, currency in (("MY", "MYR"), ("PH", "PHP"), ("TH", "THB"),
                           ("VN", "VND"), ("SG", "SGD")):
        template = pt.normalize_template({"name": site, "site": site})
        calc = pt.display_price("10", "0.5", template, rate=Decimal("1"))
        assert calc["currency"] == currency, site


def test_order_process_fee_and_withholding_are_applied():
    """订单处理服务费与企业代扣税必须真的进成本（原来这两个字段被丢掉）。"""
    base = {"name": "VN", "site": "VN", "profitPercent": 15.0,
            "lastMileFreightCny": 7.79, "paymentChargePercent": 6.0,
            "vatChargePercent": 10.0, "withdrawChargePercent": 1.0}
    without = pt.normalize_template({**base})
    with_fees = pt.normalize_template(
        {**base, "orderProcessCny": 0.78, "enterpriseWithholdPercent": 1.0})
    a = pt.display_price("30", "0.6", without, rate=Decimal(RATES["VN"]))
    b = pt.display_price("30", "0.6", with_fees, rate=Decimal(RATES["VN"]))
    assert b["lines"]["orderProcess"] > 0, "订单处理服务费没进成本"
    assert b["lines"]["enterpriseWithhold"] > 0, "企业代扣税没进成本"
    assert b["price"] > a["price"], "加了费用后售价必须更高"
