from __future__ import annotations

import pytest

from models.account import Account
from services import collect_box_service
from services.cache import TTLCache


def test_force_refresh_bypasses_fresh_entry() -> None:
    cache = TTLCache("test", default_ttl=60)
    cache.set("account:1", {"items": ["old"]})
    calls: list[bool] = []

    result = cache.fetch(
        "account:1",
        lambda: calls.append(True) or {"items": ["new"]},
        force_refresh=True,
    )

    assert calls == [True]
    assert result.value == {"items": ["new"]}
    assert not result.cached
    assert not result.degraded
    assert cache.get("account:1") == {"items": ["new"]}


def test_force_refresh_keeps_stale_fallback_on_upstream_failure() -> None:
    cache = TTLCache("test", default_ttl=60)
    cache.set("account:1", {"items": ["old"]})

    with pytest.raises(RuntimeError):
        cache.fetch("account:1", lambda: (_ for _ in ()).throw(RuntimeError("upstream")),
                    force_refresh=True, stale_on=())

    result = cache.fetch(
        "account:1",
        lambda: (_ for _ in ()).throw(RuntimeError("upstream")),
        force_refresh=True,
        stale_on=RuntimeError,
    )
    assert result.value == {"items": ["old"]}
    assert result.cached
    assert result.degraded


def test_collect_box_cache_isolated_for_transient_accounts_sharing_row_id(
        monkeypatch) -> None:
    cache = TTLCache("collect-box-test", default_ttl=60)
    monkeypatch.setattr(collect_box_service, "_cache", lambda: cache)
    monkeypatch.setattr(collect_box_service, "cache_ttl", lambda: 60)
    monkeypatch.setattr(collect_box_service, "get_settings", lambda: type(
        "Settings", (), {"local_secret": lambda self: "test-install-secret"})())
    monkeypatch.setattr(collect_box_service, "_annotate_warehouse", lambda *_args: None)
    loads: list[tuple[str, str]] = []

    def scan(account: Account, **_kwargs):
        pair = (account.app_key, account.app_secret_enc)
        loads.append(pair)
        return {"items": [{"key": "1:2", "title": account.app_key}],
                "total": 1, "scanned": 1}

    monkeypatch.setattr(collect_box_service, "_scan_upstream", scan)
    first_account = Account(id=0, app_key="app-key-a", app_secret_enc="secret-a")
    second_account = Account(id=0, app_key="app-key-b", app_secret_enc="secret-b")

    first = collect_box_service.list_items(object(), first_account)
    second = collect_box_service.list_items(object(), second_account)
    first_cached = collect_box_service.list_items(object(), first_account)

    assert first["items"][0]["title"] == "app-key-a"
    assert second["items"][0]["title"] == "app-key-b"
    assert not first["cached"]
    assert not second["cached"]
    assert first_cached["cached"]
    assert loads == [("app-key-a", "secret-a"), ("app-key-b", "secret-b")]

    key = collect_box_service._cache_key(
        first_account, page=1, page_size=20, status="notPublished", keyword="",
        shop_id=None, site=None, sort="default")
    assert "app-key-a" not in key
    assert "secret-a" not in key
