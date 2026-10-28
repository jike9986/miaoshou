"""定价模板：校验、存储（settings 里）、按模板重算 SKU 售价。

算法与旧 price_template.py 一致：解「本地展示价」，使
    售价 - VAT - 跨境运费 - 支付手续费 - 平台佣金 - 提现费 - BCP - SFP
         - 联盟佣金 - 尾程税 - 平台支持费 - 其他费用 - 利润 = 0
其中利润按售价（bySalePriceProfit）或成本（byCostProfit）计算。

originPrice（采购价）永远不改：它是汇率的来源，也是操作者核对成本的依据。
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

FALLBACK_EXCHANGE_RATE = Decimal("1.6393")

DEFAULT_FREIGHT_TABLE = {
    "currency": "MYR",
    "firstWeightGrams": 30,
    "firstWeightPrice": 0.18,
    "continuedUnitGrams": 10,
    "continuedUnitPrice": 0.12,
}

DEFAULT_TEMPLATE: dict[str, Any] = {
    "name": "马来",
    "site": "MY",
    "currency": "CNY",
    "profitType": "bySalePriceProfit",
    "profitPercent": 20.0,
    "discount": None,
    "freightMode": "officialCrossBorder",
    "lastMileFreightCny": 16.63,
    "weightSource": "spu",
    "platformCommissionPercent": None,
    "paymentChargePercent": 3.78,
    "vatChargePercent": 10.0,
    "withdrawChargePercent": 1.0,
    "sfpChargePercent": None,
    "affiliateChargePercent": None,
    "bcpChargePercent": 3.24,
    "platformSupportCny": 0.83,
    "lastMileFreightServiceTaxPercent": None,
    "otherChargeCny": None,
    # 上游模板会下发这六项，老版 pricing.py 的 13 费率公式里都参与计算。
    # 新版一度只保留 8 项，缺的这六项会被当成 0 —— 成本少算，报价偏低。
    # 默认 0 而不是 None：这些是「不加收」而不是「未知」。
    "activityChargePercent": 0.0,
    "tariffChargePercent": 0.0,
    "freeShippingChargePercent": 0.0,
    "eCommerceGrowthChargePercent": 0.0,
    "consumptionTaxChargePercent": 0.0,
    "localConsumptionTaxChargePercent": 0.0,
    "freightTable": dict(DEFAULT_FREIGHT_TABLE),
}

# 这六项与原有八项一样是「售价的百分比」，校验与合计口径必须一致。
EXTRA_CHARGE_FIELDS = (
    "activityChargePercent",
    "tariffChargePercent",
    "freeShippingChargePercent",
    "eCommerceGrowthChargePercent",
    "consumptionTaxChargePercent",
    "localConsumptionTaxChargePercent",
)


class PriceTemplateError(ValueError):
    """模板或输入不足以算出可信售价。"""


def _decimal(value: Any, field: str) -> Decimal:
    if value in (None, ""):
        raise PriceTemplateError(f"{field} 未填写")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise PriceTemplateError(f"{field} 不是有效数字") from exc
    if not number.is_finite():
        raise PriceTemplateError(f"{field} 不是有效数字")
    return number


def _optional_decimal(value: Any, field: str) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    if isinstance(value, bool):
        raise PriceTemplateError(f"{field} 不是有效数字")
    return _decimal(value, field)


def _percent(value: Any, field: str) -> Decimal:
    number = _optional_decimal(value, field)
    if number < 0 or number >= 100:
        raise PriceTemplateError(f"{field} 必须在 0 到 100 之间")
    return number


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def normalize_template(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """校验操作者给的模板并补默认值。"""
    template = dict(DEFAULT_TEMPLATE)
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            if key == "freightTable" and isinstance(value, Mapping):
                table = dict(DEFAULT_FREIGHT_TABLE)
                table.update(value)
                template["freightTable"] = table
            elif key in DEFAULT_TEMPLATE:
                template[key] = value
    name = str(template.get("name") or "马来").strip()
    if not name or len(name) > 64:
        raise PriceTemplateError("模板名称必须是 1 到 64 个字符")
    template["name"] = name
    site = str(template.get("site") or "MY").strip().upper()
    if not site.isalnum() or len(site) > 8:
        raise PriceTemplateError("站点代码无效")
    template["site"] = site
    if template.get("profitType") not in {"bySalePriceProfit", "byCostProfit"}:
        raise PriceTemplateError("利润方式无效")
    template["profitPercent"] = float(_percent(template.get("profitPercent"), "利润率"))
    for field in ("paymentChargePercent", "vatChargePercent", "withdrawChargePercent",
                  "bcpChargePercent"):
        template[field] = float(_percent(template.get(field), field))
    for field in ("platformCommissionPercent", "sfpChargePercent",
                  "affiliateChargePercent", "lastMileFreightServiceTaxPercent"):
        template[field] = float(_percent(template.get(field), field))
    for field in EXTRA_CHARGE_FIELDS:
        template[field] = float(_percent(template.get(field), field))
    template["lastMileFreightCny"] = float(
        _optional_decimal(template.get("lastMileFreightCny"), "尾程运费"))
    template["platformSupportCny"] = float(
        _optional_decimal(template.get("platformSupportCny"), "平台支持费"))
    template["otherChargeCny"] = float(
        _optional_decimal(template.get("otherChargeCny"), "其他费用"))

    table = template.get("freightTable") or {}
    first_g = _decimal(table.get("firstWeightGrams"), "起重重量")
    first_price = _optional_decimal(table.get("firstWeightPrice"), "起重价格")
    unit_g = _decimal(table.get("continuedUnitGrams"), "续重单位")
    unit_price = _optional_decimal(table.get("continuedUnitPrice"), "续重价格")
    if first_g <= 0 or unit_g <= 0:
        raise PriceTemplateError("运价表的重量必须大于 0")
    if first_price < 0 or unit_price < 0:
        raise PriceTemplateError("运价表的价格不能为负")
    template["freightTable"] = {
        "currency": str(table.get("currency") or "MYR"),
        "firstWeightGrams": float(first_g),
        "firstWeightPrice": float(first_price),
        "continuedUnitGrams": float(unit_g),
        "continuedUnitPrice": float(unit_price),
    }
    variable = (template["paymentChargePercent"] + template["vatChargePercent"]
                + template["withdrawChargePercent"] + template["bcpChargePercent"]
                + template["platformCommissionPercent"]
                + template["sfpChargePercent"] + template["affiliateChargePercent"]
                + template["lastMileFreightServiceTaxPercent"]
                + sum(template[field] for field in EXTRA_CHARGE_FIELDS))
    if template["profitType"] == "bySalePriceProfit":
        vat = Decimal(str(template["vatChargePercent"]))
        vat_share = vat / (Decimal("100") + vat) * Decimal("100")
        effective = (Decimal(str(template["profitPercent"])) + vat_share
                     + Decimal(str(variable - template["vatChargePercent"])))
        if effective >= Decimal("100"):
            raise PriceTemplateError("利润率与各项费率合计不允许计算售价")
    return template


def cross_border_freight_myr(weight_kg: Any, template: Mapping[str, Any]) -> Decimal:
    """官方跨境运费（表内币种，默认 MYR）。"""
    grams = _decimal(weight_kg, "包裹重量") * Decimal("1000")
    table = template.get("freightTable") or DEFAULT_FREIGHT_TABLE
    first_g = Decimal(str(table.get("firstWeightGrams", DEFAULT_FREIGHT_TABLE["firstWeightGrams"])))
    first_price = Decimal(str(
        table.get("firstWeightPrice", DEFAULT_FREIGHT_TABLE["firstWeightPrice"])))
    unit_g = Decimal(str(
        table.get("continuedUnitGrams", DEFAULT_FREIGHT_TABLE["continuedUnitGrams"])))
    unit_price = Decimal(str(
        table.get("continuedUnitPrice", DEFAULT_FREIGHT_TABLE["continuedUnitPrice"])))
    if grams <= first_g:
        return _money(first_price)
    extra = grams - first_g
    units = (extra / unit_g).to_integral_value(rounding=ROUND_HALF_UP)
    return _money(first_price + units * unit_price)


def resolve_rate_with_source(product: Mapping[str, Any] | None,
                             override: Any = None,
                             *, allow_live: bool = True) -> tuple[Decimal, str]:
    """CNY→站点币种汇率，连同来源一起返回（老版 exchange_rate 的四级链路）。

    顺序：手填 override > 商品自身 originPrice/price 比值 > 实时接口 > 内置兜底。
    来源文案会原样显示给用户——「这个价是按什么汇率算的」必须能追溯，
    否则报价对不对只能靠猜。
    """
    if override not in (None, ""):
        rate = _decimal(override, "汇率")
        if rate <= 0:
            raise PriceTemplateError("汇率必须大于 0")
        return rate, "手动填写"

    if isinstance(product, Mapping):
        sku_map = product.get("skuMap") or {}
        rates: list[Decimal] = []
        for sku in sku_map.values():
            if not isinstance(sku, Mapping) or str(sku.get("isDelete", "0")) == "1":
                continue
            origin = sku.get("originPrice")
            price = sku.get("price")
            if origin in (None, "") or price in (None, ""):
                continue
            origin_d, price_d = _decimal(origin, "originPrice"), _decimal(price, "price")
            if origin_d > 0 and price_d > 0:
                rates.append(origin_d / price_d)
        if rates:
            return sum(rates) / len(rates), "商品自身采购价与售价之比"

    if allow_live:
        from services import exchange_rate

        live, source = exchange_rate.live_rate()
        if live is not None:
            cached, cached_source = exchange_rate.cached_rate()
            label = source or cached_source
            return live, f"实时汇率（{label}）" if source else f"缓存汇率（{label}）"
        return FALLBACK_EXCHANGE_RATE, f"内置汇率（网络不可用，{FALLBACK_EXCHANGE_RATE}）"

    return FALLBACK_EXCHANGE_RATE, f"内置汇率（{FALLBACK_EXCHANGE_RATE}）"


def resolve_rate(product: Mapping[str, Any] | None, override: Any = None) -> Decimal:
    """只要汇率本身（保留老签名，内部走完整链路）。"""
    return resolve_rate_with_source(product, override)[0]


# 这些是「售价的百分比」，缺一项就少算一块成本，报价偏低。
# 列在这里（而不是散在 display_price 里）是因为 preview_price 也要用它：
# 归一化会把 None 折成 0，「这项没填」的信号只剩在原始输入里。
RATE_FIELDS: tuple[tuple[str, str], ...] = (
    ("vatChargePercent", "VAT"),
    ("paymentChargePercent", "支付手续费"),
    ("platformCommissionPercent", "平台佣金"),
    ("bcpChargePercent", "BCP"),
    ("withdrawChargePercent", "提现费"),
    ("sfpChargePercent", "SFP"),
    ("affiliateChargePercent", "达人佣金"),
    ("lastMileFreightServiceTaxPercent", "尾程服务税"),
    ("activityChargePercent", "活动费"),
    ("tariffChargePercent", "关税"),
    ("freeShippingChargePercent", "包邮成本"),
    ("eCommerceGrowthChargePercent", "电商增长费"),
    ("consumptionTaxChargePercent", "消费税"),
    ("localConsumptionTaxChargePercent", "地方消费税"),
)


def _declared(template: Mapping[str, Any] | None) -> dict[str, Any]:
    """归一化之前的「声明值」：缺键按内置默认补，None 原样保留。

    normalize_template 会把 None 费率折成 0（display_price 才能直接乘），
    但「这一项运营没填」这件事只有原始输入知道。估算标记（isEstimate）
    要靠它，否则一个缺平台佣金的模板会被当成精确价报出去。

    注意：库里存的模板都是归一化过的（save_template 存的就是归一化结果），
    0 到底是「不收」还是「没填」已经不可分辨，此时不标估算 —— 乱标会让
    每个报价都顶着「估算」角标，等于没有信号。要拿到估算标记，调用方得把
    上游原始模板（含 None）传进来。
    """
    declared = dict(DEFAULT_TEMPLATE)
    if isinstance(template, Mapping):
        for key, value in template.items():
            if key in DEFAULT_TEMPLATE:
                declared[key] = value
    return declared


def display_price(purchase_cny: Any, weight_kg: Any, template: Mapping[str, Any], *,
                  rate: Any) -> dict[str, Any]:
    """解出本地展示价，并给出完整费用拆分。"""
    rate_d = _decimal(rate, "汇率")
    if rate_d <= 0:
        raise PriceTemplateError("汇率必须大于 0")
    purchase = _decimal(purchase_cny, "产品采购价")
    if purchase <= 0:
        raise PriceTemplateError("产品采购价必须大于 0")
    weight = _decimal(weight_kg, "产品包裹重量")
    if weight <= 0:
        raise PriceTemplateError("产品包裹重量必须大于 0")

    freight = cross_border_freight_myr(weight, template)
    last_mile_myr = Decimal(str(template.get("lastMileFreightCny") or 0)) / rate_d
    support_myr = _money(Decimal(str(template.get("platformSupportCny") or 0)) / rate_d)
    other_myr = _money(Decimal(str(template.get("otherChargeCny") or 0)) / rate_d)
    purchase_myr = _money(purchase / rate_d)

    # 费率缺失时按 0 算会让报价偏低，但不该整单报错——运营的模板常常只填
    # 常用的那几项。这里把「缺了哪几项」记下来，报价标记为估算，
    # 让人知道这个数不是精确值（老版 pricing.calculate_template_price 的
    # missingRateFields / isEstimate）。
    declared = _declared(template)
    missing_rate_fields = [name for name, _label in RATE_FIELDS
                           if declared.get(name) in (None, "")]

    vat_pct = Decimal(str(template.get("vatChargePercent") or 0))
    payment_pct = Decimal(str(template.get("paymentChargePercent") or 0))
    commission_pct = Decimal(str(template.get("platformCommissionPercent") or 0))
    bcp_pct = Decimal(str(template.get("bcpChargePercent") or 0))
    withdraw_pct = Decimal(str(template.get("withdrawChargePercent") or 0))
    sfp_pct = Decimal(str(template.get("sfpChargePercent") or 0))
    affiliate_pct = Decimal(str(template.get("affiliateChargePercent") or 0))
    last_mile_tax_pct = Decimal(str(template.get("lastMileFreightServiceTaxPercent") or 0))
    # 这六项都是「售价的百分比」，直接乘售价进成本，不参与 withdraw 的税前基数
    # 推导（老版 pricing.calculate_template_price 里也是同一处理）。
    extra_pct = {field: Decimal(str(template.get(field) or 0))
                 for field in EXTRA_CHARGE_FIELDS}
    profit_pct = Decimal(str(template.get("profitPercent") or 0))
    profit_by_sale = template.get("profitType", "bySalePriceProfit") == "bySalePriceProfit"

    def parts(price: Decimal) -> dict[str, Decimal]:
        vat = price * vat_pct / (Decimal("100") + vat_pct) if vat_pct else Decimal("0")
        service_tax = (price * last_mile_tax_pct / (Decimal("100") + last_mile_tax_pct)
                       if last_mile_tax_pct else Decimal("0"))
        payment = payment_pct / Decimal("100") * (price + last_mile_myr + service_tax)
        commission = commission_pct / Decimal("100") * price
        bcp = bcp_pct / Decimal("100") * price
        sfp = sfp_pct / Decimal("100") * price
        affiliate = affiliate_pct / Decimal("100") * price
        pretax = price - vat - freight
        withdraw = withdraw_pct / Decimal("100") * (
            pretax - payment - bcp - support_myr - commission - sfp - affiliate)
        profit = (profit_pct / Decimal("100") * price if profit_by_sale
                  else profit_pct / Decimal("100") * (purchase_myr + freight))
        lines = {
            "purchasePrice": purchase_myr,
            "merchantFreight": freight,
            "vat": vat,
            "paymentCharge": payment,
            "platformCommission": commission,
            "withdrawCharge": withdraw,
            "bcpCharge": bcp,
            "sfpCharge": sfp,
            "affiliateCharge": affiliate,
            "lastMileTax": service_tax,
            "platformSupport": support_myr,
            "otherCharge": other_myr,
            "profit": profit,
            "pretaxPrice": pretax,
        }
        # 六个附加费率即使为 0 也要写进拆分：cost_keys 会按固定键名求和，
        # 缺键直接 KeyError。顺手让拆分结构稳定 —— 前端不必判断键是否存在。
        for field, pct in extra_pct.items():
            lines[field] = price * pct / Decimal("100") if pct else Decimal("0")
        return lines

    cost_keys = ("purchasePrice", "merchantFreight", "vat", "paymentCharge",
                 "platformCommission", "withdrawCharge", "bcpCharge", "sfpCharge",
                 "affiliateCharge", "lastMileTax", "platformSupport", "otherCharge",
                 *EXTRA_CHARGE_FIELDS)

    def residual(price: Decimal) -> Decimal:
        lines = parts(price)
        return sum(lines[k] for k in cost_keys) + lines["profit"] - price

    lo, hi = Decimal("0.01"), Decimal("1000000")
    if residual(lo) < 0 or residual(hi) > 0:
        raise PriceTemplateError("无法解出售价：利润率与费率组合不可行")
    for _ in range(200):
        mid = (lo + hi) / 2
        if residual(mid) > 0:
            lo = mid
        else:
            hi = mid
    price = _money((lo + hi) / 2)
    lines = parts(price)
    return {
        "price": float(price),
        "currency": "MYR",
        "rate": float(rate_d),
        "purchasePriceCny": float(purchase),
        "packageWeightKg": float(weight),
        "templateName": template.get("name"),
        "lines": {key: float(value) for key, value in lines.items()},
        "linesCny": {key: float(value * rate_d) for key, value in lines.items()},
        # 缺项 + 是否估算：前端据此显示「估算」角标，而不是把估算价当精确价用。
        "missingRateFields": missing_rate_fields,
        "isEstimate": bool(missing_rate_fields),
        "freightAssumption": (
            "按模板运费表计算"
            if template.get("freightTable") or template.get("firstWeightGrams")
            else "模板没有运费表，按 0 计运费"),
    }


def preview_price(purchase_cny: Any, weight_kg: Any, template: Mapping[str, Any], *,
                  rate: Any = None) -> dict[str, Any]:
    """未入仓商品的定价预览：手填采购价 + 重量，按模板算展示价。

    与 display_price 的区别只是「不需要 skuMap、汇率可以不给」。
    汇率不给时走 resolve_rate 的兜底（内置 FALLBACK_EXCHANGE_RATE）。
    返回 display_price 的结果 + rateSource。

    为什么单独一个函数：`apply_template` 要求完整 item（含 skuMap），
    未入仓商品还没有 SKU，套用时报「商品没有 SKU，不能套用定价模板」——
    于是「先算算这个采购价卖多少」这件最常用的事反而做不了。
    老版走的是 `service_price.preview`（:130）→ `price_template.display_price`，
    差别只在汇率来源。
    """
    normalized = normalize_template(template)
    if rate in (None, ""):
        # 没有商品就没有 originPrice/price 比值可用，链路直接落到实时/内置汇率。
        rate_d, rate_source = resolve_rate_with_source(None, None)
    else:
        rate_d, rate_source = resolve_rate_with_source(None, rate)
    result = display_price(purchase_cny, weight_kg, normalized, rate=rate_d)
    # display_price 收到的是归一化后的模板（None 已折成 0），「没填」的信号
    # 只能在原始输入上取，所以这里重算一次并覆盖。
    declared = _declared(template)
    result["missingRateFields"] = [name for name, _label in RATE_FIELDS
                                   if declared.get(name) in (None, "")]
    result["isEstimate"] = bool(result["missingRateFields"])
    result["rateSource"] = rate_source
    return result


def apply_template(item: Mapping[str, Any], template: Mapping[str, Any], *,
                   rate: Any = None, purchase_price: Any = None
                   ) -> tuple[dict[str, Any], dict[str, Any]]:
    """按模板重算每个有效 SKU 的售价；originPrice 不动。"""
    result = deepcopy(dict(item))
    weight = result.get("weight")
    if weight in (None, ""):
        raise PriceTemplateError("商品重量为空，不能套用定价模板")
    sku_map = result.get("skuMap")
    if not isinstance(sku_map, dict) or not sku_map:
        raise PriceTemplateError("商品没有 SKU，不能套用定价模板")

    rate_d, rate_source = resolve_rate_with_source(result, rate)
    by_spu = template.get("weightSource", "spu") != "sku"
    samples: list[dict[str, Any]] = []
    changed = 0
    for key, sku in sku_map.items():
        if not isinstance(sku, dict) or str(sku.get("isDelete", "0")) == "1":
            continue
        purchase = purchase_price if purchase_price not in (None, "") else sku.get("originPrice")
        if purchase in (None, ""):
            purchase = sku.get("price")
        sku_weight = weight if by_spu else (sku.get("weight") or weight)
        calculation = display_price(purchase, sku_weight, template, rate=rate_d)
        new_price = Decimal(str(calculation["price"]))
        old_price = sku.get("price")
        try:
            old_d = _decimal(old_price, f"SKU {key}.price")
        except PriceTemplateError:
            old_d = None
        new_include = new_price
        # changed 要连含税价一起看：只改 priceIncludeVat 的那种（四舍五入边界）
        # 老版算改动、新版只比 price 会报「未变」，前端「已重新定价」提示就漏了。
        old_include = sku.get("priceIncludeVat")
        include_moved = False
        if old_include not in (None, ""):
            try:
                include_moved = _money(_decimal(old_include, "priceIncludeVat")) != new_include
            except PriceTemplateError:
                include_moved = False
        changed += int(old_d is None or _money(old_d) != new_price or include_moved)
        sku["price"] = float(new_price)
        sku["priceIncludeVat"] = float(new_include)
        if len(samples) < 10:
            samples.append({
                "skuKey": str(key),
                "purchaseCny": float(_decimal(purchase, "purchase")),
                "weightKg": float(_decimal(sku_weight, "weight")),
                "before": float(old_d) if old_d is not None else None,
                "after": float(new_price),
                "calculation": calculation,
            })
    if not samples:
        raise PriceTemplateError("没有可套用定价模板的有效 SKU")
    return result, {
        "templateName": template.get("name"),
        "rate": float(rate_d),
        # 汇率来源要能追溯：这批价是按手填、商品比值、实时还是内置兜底算的，
        # 报价对不对全靠这一行判断。
        "rateSource": rate_source,
        "changedCount": changed,
        "skuCount": len(samples),
        "samples": samples,
    }


# --- 从妙手同步模板 ------------------------------------------------------
# 老版 price_template.list_miaoshou_templates / pick_miaoshou_template /
# template_from_miaoshou 三个函数，逐字段照搬语义。
#
# 为什么必须有 pick：妙手同一个站点可以有多套模板（「马来」和「马来 2.0」
# 利润率不同）。不选就取第 0 行，等于用别人的利润率算价 —— 这个错只在
# 真实订单上暴露，界面看不出任何异常。宁可不做也不能猜。
def list_upstream_templates(raw: Any) -> list[dict[str, Any]]:
    """把上游 priceTemplateList 归一化成可选项列表。

    上游偶尔把列表包在 data 里，也偶尔直接返回单条模板本身，两种都收。
    没有 name 的行直接丢掉：下拉里显示空白项比少一项更难用。
    """
    data = raw if isinstance(raw, Mapping) else {}
    if isinstance(data.get("data"), Mapping):
        data = data["data"]
    rows = data.get("priceTemplateList")
    if not isinstance(rows, list):
        rows = [data] if data else []
    templates: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        name = row.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        templates.append({
            "name": name.strip(),
            "site": str(row.get("site") or "").strip().upper(),
            "profitType": row.get("profitType"),
            "profitPercent": row.get("profitPercent"),
            "currency": row.get("currency"),
            # weightRefType 缺省按 spu：上游不传时用商品级重量，不是按 SKU 逐个。
            "weightSource": "spu" if row.get("weightRefType", "spu") == "spu" else "sku",
        })
    return templates


def pick_upstream_template(raw: Any, name: Any = "") -> Mapping[str, Any]:
    """选出操作者指定的那一套模板；多套未指定就报错并列出名字。

    报错文案要带名字，否则运营不知道该填哪个字符串。
    """
    data = raw if isinstance(raw, Mapping) else {}
    if isinstance(data.get("data"), Mapping):
        data = data["data"]
    rows = [row for row in (data.get("priceTemplateList") or [])
            if isinstance(row, Mapping) and str(row.get("name") or "").strip()]
    if not rows:
        raise PriceTemplateError("妙手没有返回定价模板，请先连接妙手")
    wanted = str(name or "").strip()
    if not wanted:
        if len(rows) == 1:
            return rows[0]
        names = "、".join(f"「{str(r.get('name')).strip()}」" for r in rows)
        raise PriceTemplateError(f"妙手有 {len(rows)} 套定价模板，请先选择用哪一套：{names}")
    for row in rows:
        if str(row.get("name") or "").strip() == wanted:
            return row
    names = "、".join(f"「{str(r.get('name')).strip()}」" for r in rows)
    raise PriceTemplateError(f"妙手里没有名为「{wanted}」的模板，现有：{names}")


def merge_from_upstream(raw: Any, existing: Mapping[str, Any] | None = None,
                        site_defaults: Mapping[str, Any] | None = None,
                        *, template_name: Any = "") -> dict[str, Any]:
    """上游模板 + 已存值 + 站点默认，三级合并成一份本地模板。

    每个字段的取值顺序：
      1. 上游模板给了值 → 用上游
      2. 已存值（操作者可能手工修正过）→ 用已存
      3. 站点默认 → 用默认

    **关键语义：上游「字段存在但为空」＝ 这一项不收，用已存值，不是把已存值清空。**
    妙手模板响应里 vatChargePercent / bcpChargePercent / sfpChargePercent /
    affiliateChargePercent / platformSupportCharge 这几个字段根本不出现；
    若把「不出现」当成 0，就会把操作者已经填好的费率冲成 0，报价立刻偏低。

    运费表是唯一不能同步的：接口报 logisticsComputeType=officialFreightTpl
    而 firstWeight*/continuedWeight* 全为空，所以已存的运费表原样保留。
    """
    data = raw if isinstance(raw, Mapping) else {}
    if isinstance(data.get("data"), Mapping):
        data = data["data"]
    source = pick_upstream_template(raw, template_name)
    defaults = site_defaults if isinstance(site_defaults, Mapping) else {}
    if isinstance(defaults.get("data"), Mapping):
        defaults = defaults["data"]
    stored = (normalize_template(existing) if isinstance(existing, Mapping)
              else normalize_template(None))

    name = source.get("name")
    if not isinstance(name, str) or not name.strip():
        raise PriceTemplateError("妙手模板没有名称，无法同步")
    site = str(source.get("site") or stored.get("site") or "MY").strip().upper()
    rate_map = defaults.get("tiktokCbSiteAndDefaultChargePercentMap") or {}
    site_rate = rate_map.get(site) or {}
    support_cny_map = defaults.get("siteAndDefaultPlatformSupportChargeCNYMap") or {}

    def pick(local_key: str, remote_keys: tuple[str, ...], site_field: str = "") -> Any:
        for key in remote_keys:
            if key in source:
                # 存在但为空 = 该模板不收这项费用，仍以已存值为准
                return None if source.get(key) in (None, "") else source.get(key)
        if stored.get(local_key) not in (None, ""):
            return stored.get(local_key)
        if site_field:
            return None if site_rate.get(site_field) in (None, "") else site_rate.get(site_field)
        return None

    support = pick("platformSupportCny", ("platformSupportCharge",))
    if support is None and stored.get("platformSupportCny") in (None, ""):
        support = support_cny_map.get(site)

    synced = {
        "name": name.strip(),
        "site": site,
        "currency": str(source.get("currency") or stored.get("currency") or "CNY"),
        "profitType": source.get("profitType") or stored.get("profitType"),
        "profitPercent": pick("profitPercent", ("profitPercent",)),
        "lastMileFreightCny": pick("lastMileFreightCny", ("buyerLogisticCharge",)),
        "platformCommissionPercent": pick("platformCommissionPercent", ("platformChargePercent",)),
        "paymentChargePercent": pick("paymentChargePercent", ("paymentChargePercent",),
                                     "paymentChargePercent"),
        "vatChargePercent": pick("vatChargePercent", ("vatChargePercent",), "vatChargePercent"),
        "withdrawChargePercent": pick("withdrawChargePercent", ("withdrawChargePercent",),
                                      "withdrawChargePercent"),
        "bcpChargePercent": pick("bcpChargePercent", ("bcpChargePercent",), "bcpChargePercent"),
        "sfpChargePercent": pick("sfpChargePercent", ("sfpChargePercent",), "sfpChargePercent"),
        "affiliateChargePercent": pick("affiliateChargePercent", ("affiliateChargePercent",),
                                       "affiliateChargePercent"),
        "platformSupportCny": support,
        "lastMileFreightServiceTaxPercent": pick(
            "lastMileFreightServiceTaxPercent", ("lastMileFreightServiceTaxPercent",)),
        "otherChargeCny": pick("otherChargeCny", ("otherCharge",)),
        "weightSource": ("spu" if source.get("weightRefType", "spu") == "spu" else "sku")
                        if "weightRefType" in source else stored.get("weightSource"),
        # 接口不下发运费表，已存的必须原样带走
        "freightTable": dict(stored.get("freightTable") or DEFAULT_FREIGHT_TABLE),
    }
    # 六个新增费率同样走三级合并。它们不在妙手模板响应里，所以实际来源是
    # 已存值或站点默认 —— 但保留 pick 调用，将来上游开始下发就能自动接上。
    for field in EXTRA_CHARGE_FIELDS:
        synced[field] = pick(field, (field,), field)
    return normalize_template(synced)
