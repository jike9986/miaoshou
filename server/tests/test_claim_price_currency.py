"""定价模板与认领价格的币种换算测试。

对应负责人 2026-11-18 口径：
  - 仓库价格是人民币，认领到店铺要换算成当地币种；
  - 认领到采集箱不套定价模板（模板在妙手那边套）；
  - 只允许妙手成本下限的兜底微调。
"""
from decimal import Decimal
import time


def test_currency_for_site_covers_all_five_sites():
    """五个站点各自对应妙手官方运费表的币种。"""
    from services import exchange_rate

    assert exchange_rate.currency_for_site("MY") == "MYR"
    assert exchange_rate.currency_for_site("PH") == "PHP"
    assert exchange_rate.currency_for_site("TH") == "THB"
    assert exchange_rate.currency_for_site("VN") == "VND"
    assert exchange_rate.currency_for_site("SG") == "SGD"
    assert exchange_rate.currency_for_site("my") == "MYR"
    assert exchange_rate.currency_for_site(None) == "MYR"
    assert exchange_rate.currency_for_site("XX") == "MYR"


def test_fallback_rates_exist_for_every_supported_currency():
    from services import exchange_rate

    for code in ("MYR", "PHP", "THB", "VND", "SGD"):
        assert exchange_rate.fallback_rate(code) > 0, code
    assert exchange_rate.fallback_rate("VND") < Decimal("0.001")
    assert exchange_rate.fallback_rate("MYR") > Decimal("1")


def test_cny_rate_is_identity():
    from services import exchange_rate

    assert exchange_rate.cached_rate(currency="CNY")[0] == Decimal("1")


def _pin_rate(currency: str, rate: str) -> None:
    from services import exchange_rate

    exchange_rate.reset_cache()
    exchange_rate._cache[currency] = {
        "rate": Decimal(rate), "at": time.time(),
        "source": "test", "failedAt": None}


def test_claim_price_reset_uses_purchase_price_not_template_price():
    """认领时 skuMap.price 必须重置成拿货价换算值，不能沿用模板售价。

    原来沿用 payload 里入仓时算好的售价：22 CNY 的商品进采集箱变成
    40+ MYR（约 67 CNY），看起来像凭空涨价。
    """
    import services.collect_box_service as cbs
    from services import exchange_rate

    _pin_rate("MYR", "2")   # 1 MYR = 2 CNY -> 20 CNY = 10.00 MYR

    class _Shop:
        id = 1
        name = "test"

    payloads = [{
        "site": "MY",
        "skuMap": {
            "sku-a": {"price": "41.20", "priceIncludeVat": "41.20",
                      "originPrice": "20"},
            "sku-b": {"price": "41.20", "priceIncludeVat": "41.20",
                      "originPrice": "30"},
        },
    }]
    entries = [{"warehouseId": "w1"}]

    original = cbs.require_shop
    cbs.require_shop = lambda *_a, **_k: {"site": "MY", "shopId": "1"}
    try:
        adjustments = cbs._reset_claim_prices_to_purchase(
            None, _Shop(), payloads, entries, target_shop_id="1")
    finally:
        cbs.require_shop = original
        exchange_rate.reset_cache()

    skus = payloads[0]["skuMap"]
    assert skus["sku-a"]["price"] == "10.00"
    assert skus["sku-b"]["price"] == "15.00"
    assert skus["sku-a"]["priceIncludeVat"] == "10.00"
    assert adjustments and adjustments[0]["stage"] == "claimPrice"
    assert adjustments[0]["currency"] == "MYR"
    assert adjustments[0]["skuCount"] == 2


def test_claim_price_reset_uses_target_site_currency():
    """认领到菲律宾店铺要按 PHP 换算，不是 MYR。"""
    import services.collect_box_service as cbs
    from services import exchange_rate

    _pin_rate("PHP", "0.1")   # 1 PHP = 0.1 CNY -> 20 CNY = 200.00 PHP

    class _Shop:
        id = 2
        name = "ph"

    payloads = [{"site": "PH", "skuMap": {
        "sku-a": {"price": "300.00", "originPrice": "20"}}}]

    original = cbs.require_shop
    cbs.require_shop = lambda *_a, **_k: {"site": "PH", "shopId": "2"}
    try:
        adjustments = cbs._reset_claim_prices_to_purchase(
            None, _Shop(), payloads, [{"warehouseId": "w1"}],
            target_shop_id="2")
    finally:
        cbs.require_shop = original
        exchange_rate.reset_cache()

    assert payloads[0]["skuMap"]["sku-a"]["price"] == "200.00"
    assert adjustments[0]["currency"] == "PHP"
    assert adjustments[0]["site"] == "PH"


def test_claim_price_reset_skips_missing_purchase_price():
    """没有 originPrice 的 SKU 不动它，不要算出 0 元。"""
    import services.collect_box_service as cbs
    from services import exchange_rate

    _pin_rate("MYR", "2")

    class _Shop:
        id = 1
        name = "test"

    payloads = [{"site": "MY", "skuMap": {
        "sku-a": {"price": "41.20", "originPrice": None},
        "sku-b": {"price": "41.20"},
    }}]

    original = cbs.require_shop
    cbs.require_shop = lambda *_a, **_k: {"site": "MY", "shopId": "1"}
    try:
        cbs._reset_claim_prices_to_purchase(
            None, _Shop(), payloads, [{"warehouseId": "w1"}],
            target_shop_id="1")
    finally:
        cbs.require_shop = original
        exchange_rate.reset_cache()

    assert payloads[0]["skuMap"]["sku-a"]["price"] == "41.20"
    assert payloads[0]["skuMap"]["sku-b"]["price"] == "41.20"
