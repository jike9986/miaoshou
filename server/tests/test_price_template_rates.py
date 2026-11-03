from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

import httpx
import pytest

from services import exchange_rate, price_template


@pytest.fixture(autouse=True)
def isolated_rate_cache():
    exchange_rate.reset_cache()
    yield
    exchange_rate.reset_cache()


@pytest.mark.parametrize("price", ["61", "80", "63217.19", "invalid"])
def test_product_selling_price_is_never_an_exchange_rate(monkeypatch, price):
    monkeypatch.setattr(
        exchange_rate, "live_rate", lambda: (Decimal("1.65"), "test-provider"))
    product = {"skuMap": {"a": {"originPrice": "100", "price": price}}}

    rate, source = price_template.resolve_rate_with_source(product)

    assert rate == Decimal("1.65")
    assert source == "实时汇率（test-provider）"


def test_manual_rate_does_not_read_cache_or_network(monkeypatch):
    def unexpected_call():
        pytest.fail("a manual exchange rate must not read another rate source")

    monkeypatch.setattr(exchange_rate, "cached_rate", unexpected_call)
    monkeypatch.setattr(exchange_rate, "live_rate", unexpected_call)

    assert price_template.resolve_rate_with_source(None, "1.72") == (
        Decimal("1.72"), "手动填写")


def test_valid_cache_is_used_and_labelled_without_network(monkeypatch):
    monkeypatch.setattr(exchange_rate.time, "time", lambda: 10000)
    exchange_rate._cache.update({
        "rate": Decimal("1.65"), "at": 9990, "source": "cached-provider"})

    def unexpected_call():
        pytest.fail("valid cached rates must not trigger a network request")

    monkeypatch.setattr(exchange_rate, "live_rate", unexpected_call)

    assert price_template.resolve_rate_with_source(None) == (
        Decimal("1.65"), "缓存汇率（cached-provider）")


def test_expired_cache_is_replaced_with_live_rate(monkeypatch):
    monkeypatch.setattr(exchange_rate.time, "time", lambda: 10000)
    exchange_rate._cache.update({
        "rate": Decimal("1.8"), "at": 0, "source": "expired-provider"})
    monkeypatch.setattr(
        exchange_rate, "live_rate", lambda: (Decimal("1.65"), "test-provider"))

    assert exchange_rate.cached_rate() == (None, "")
    assert price_template.resolve_rate_with_source(None) == (
        Decimal("1.65"), "实时汇率（test-provider）")


def test_expired_cache_with_network_failure_uses_explicit_fallback(monkeypatch):
    monkeypatch.setattr(exchange_rate.time, "time", lambda: 10000)
    exchange_rate._cache.update({
        "rate": Decimal("1.8"), "at": 0, "source": "expired-provider"})

    def offline(*_args, **_kwargs):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx.Client, "get", offline)
    product = {"skuMap": {"a": {"originPrice": "100", "price": "61"}}}

    rate, source = price_template.resolve_rate_with_source(product)

    assert rate == price_template.FALLBACK_EXCHANGE_RATE
    assert source == f"内置汇率（网络不可用，{rate}）"


def test_live_rate_inverts_myr_per_cny_and_reuses_valid_cache(monkeypatch):
    requests = []

    def respond(_client, url, **_kwargs):
        requests.append(url)
        return httpx.Response(200, json={"rates": {"MYR": 0.625}})

    monkeypatch.setattr(exchange_rate.time, "time", lambda: 10000)
    monkeypatch.setattr(httpx.Client, "get", respond)

    assert exchange_rate.live_rate() == (Decimal("1.6"), "open.er-api")
    assert exchange_rate.live_rate() == (Decimal("1.6"), "open.er-api")
    assert len(requests) == 1


def test_failed_rate_lookup_has_short_cache_and_force_refresh_bypasses_it(monkeypatch):
    requests = []
    now = [10000]

    def offline(_client, url, **_kwargs):
        requests.append(url)
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(exchange_rate.time, "time", lambda: now[0])
    monkeypatch.setattr(httpx.Client, "get", offline)

    assert exchange_rate.live_rate() == (None, "")
    assert exchange_rate.live_rate() == (None, "")
    assert len(requests) == len(exchange_rate.FX_ENDPOINTS)

    assert exchange_rate.live_rate(use_cache=False) == (None, "")
    assert len(requests) == 2 * len(exchange_rate.FX_ENDPOINTS)

    now[0] += exchange_rate.FX_FAILURE_CACHE_TTL_SECONDS
    assert exchange_rate.live_rate() == (None, "")
    assert len(requests) == 3 * len(exchange_rate.FX_ENDPOINTS)


def test_reset_cache_retries_after_failed_rate_lookup(monkeypatch):
    requests = []

    def offline(_client, url, **_kwargs):
        requests.append(url)
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx.Client, "get", offline)

    assert exchange_rate.live_rate() == (None, "")
    exchange_rate.reset_cache()
    assert exchange_rate.live_rate() == (None, "")
    assert len(requests) == 2 * len(exchange_rate.FX_ENDPOINTS)


def test_disabling_live_rates_uses_builtin_fallback_without_sources(monkeypatch):
    def unexpected_call():
        pytest.fail("allow_live=False must not read cache or network")

    monkeypatch.setattr(exchange_rate, "cached_rate", unexpected_call)
    monkeypatch.setattr(exchange_rate, "live_rate", unexpected_call)

    rate, source = price_template.resolve_rate_with_source(
        {"skuMap": {"a": {"originPrice": "100", "price": "61"}}},
        allow_live=False)

    assert rate == price_template.FALLBACK_EXCHANGE_RATE
    assert source == f"内置汇率（{rate}）"


def test_reapplying_template_keeps_prices_and_purchase_cost_stable(monkeypatch):
    monkeypatch.setattr(
        exchange_rate, "live_rate", lambda: (Decimal("1.65"), "test-provider"))
    product = {
        "weight": "0.1",
        "skuMap": {
            "a": {"originPrice": "100", "price": "61"},
            "b": {"originPrice": "200", "price": "122"},
        },
    }
    original = deepcopy(product)
    raw_template = dict.fromkeys((field for field, _ in price_template.RATE_FIELDS), 0)
    raw_template.update({
        "profitPercent": 20,
        "lastMileFreightCny": 0,
        "platformSupportCny": 0,
        "freightTable": {"firstWeightPrice": 0, "continuedUnitPrice": 0},
    })
    template = price_template.normalize_template(raw_template)

    first, first_report = price_template.apply_template(product, template)
    second, second_report = price_template.apply_template(first, template)
    preview = price_template.preview_price("100", "0.1", template)

    assert first == second
    assert first_report["rate"] == second_report["rate"] == preview["rate"] == 1.65
    assert first["skuMap"]["a"]["price"] == preview["price"]
    assert second_report["changedCount"] == 0
    assert second["skuMap"]["a"]["originPrice"] == "100"
    assert second["skuMap"]["b"]["originPrice"] == "200"
    assert product == original


@pytest.mark.parametrize("origin", [None, ""])
def test_missing_purchase_cost_is_not_inferred_from_selling_price(origin):
    product = {
        "weight": "0.1",
        "skuMap": {"a": {"originPrice": origin, "price": "100"}},
    }
    original = deepcopy(product)

    with pytest.raises(price_template.PriceTemplateError, match="SKU a.*采购价.*补充"):
        price_template.apply_template(
            product, price_template.normalize_template(None), rate="1.65")

    assert product == original


def test_explicit_purchase_cost_supports_sku_without_origin_and_repeat_pricing():
    product = {"weight": "0.1", "skuMap": {"a": {"price": "100"}}}
    template = price_template.normalize_template(None)

    first, _ = price_template.apply_template(
        product, template, rate="1.65", purchase_price="100")
    second, report = price_template.apply_template(
        first, template, rate="1.65", purchase_price="100")

    assert first == second
    assert report["changedCount"] == 0
    assert report["samples"][0]["purchaseCny"] == 100
