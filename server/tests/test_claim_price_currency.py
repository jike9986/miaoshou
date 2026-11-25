"""定价模板与认领价格的币种换算测试。

对应负责人口径：
  - 2026-11-18：仓库价格是人民币，认领到店铺要换算成当地币种；
  - 2026-11-18：认领到采集箱不套定价模板（模板在妙手那边套）；
  - 2026-11-25：「认领到采集箱不是拿货价增一点点，比如 23 就 27、28 人民币」
    → 加价在**人民币**层面做（× CLAIM_PRICE_BUMP），换算成外币看是浮动的。
"""
import time
from decimal import Decimal


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
    """认领时 skuMap.price 必须重置成「拿货价 + 一点点」，不能沿用模板售价。

    原来沿用 payload 里入仓时算好的售价：22 CNY 的商品进采集箱变成
    40+ MYR（约 67 CNY），看起来像凭空涨价。

    加价在人民币层面（负责人 2026-11-25）。汇率钉成 1 CNY = 2 MYR：
      · sku-a 拿货 20 CNY → 20 × 1.18 = 23.60 CNY → 11.80 MYR
      · sku-b 拿货 30 CNY → 30 × 1.18 = 35.40 CNY → 17.70 MYR
    """
    import services.collect_box_service as cbs
    from services import exchange_rate

    _pin_rate("MYR", "2")   # 1 MYR = 2 CNY

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
    assert skus["sku-a"]["price"] == "11.80"
    assert skus["sku-b"]["price"] == "17.70"
    assert skus["sku-a"]["priceIncludeVat"] == "11.80"
    assert adjustments and adjustments[0]["stage"] == "claimPrice"
    assert adjustments[0]["currency"] == "MYR"
    assert adjustments[0]["skuCount"] == 2


def test_claim_price_bump_is_applied_in_cny_not_local_currency():
    """加价必须作用在人民币拿货价上（负责人 2026-11-25 的 23→27、28）。

    23 CNY × 1.18 = 27.14 CNY，落在负责人给的 27~28 区间。
    如果反过来「先换算再加外币百分比」，折回人民币看就不是加一点点，
    而且汇率一变数字就跟着乱跳。
    """
    import services.collect_box_service as cbs
    from services import exchange_rate

    _pin_rate("MYR", "2")

    class _Shop:
        id = 1
        name = "test"

    payloads = [{"site": "MY", "skuMap": {
        "sku-a": {"price": "0", "originPrice": "23"}}}]

    original = cbs.require_shop
    cbs.require_shop = lambda *_a, **_k: {"site": "MY", "shopId": "1"}
    try:
        cbs._reset_claim_prices_to_purchase(
            None, _Shop(), payloads, [{"warehouseId": "w1"}],
            target_shop_id="1")
    finally:
        cbs.require_shop = original
        exchange_rate.reset_cache()

    quoted_myr = Decimal(payloads[0]["skuMap"]["sku-a"]["price"])
    quoted_cny = quoted_myr * Decimal("2")      # 汇率 1 CNY = 2 MYR
    assert Decimal("26") <= quoted_cny <= Decimal("29"), quoted_cny
    assert quoted_cny == Decimal("27.14"), quoted_cny


def test_claim_price_reset_uses_target_site_currency():
    """认领到菲律宾店铺要按 PHP 换算，不是 MYR。"""
    import services.collect_box_service as cbs
    from services import exchange_rate

    _pin_rate("PHP", "0.1")   # 1 PHP = 0.1 CNY

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

    # 20 CNY × 1.18 = 23.60 CNY ÷ 0.1 = 236.00 PHP
    assert payloads[0]["skuMap"]["sku-a"]["price"] == "236.00"
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
