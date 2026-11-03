"""Remote update checks compare the installed client, not the Linux server."""
import pytest
from starlette.requests import Request

from api import system
from core.constants import APP_VERSION
from services import system_service


@pytest.mark.parametrize("value,expected", [
    ("0.1.5", "0.1.5"), (" 0.1.6 ", "0.1.6"),
    ("", APP_VERSION), ("0.0.0", APP_VERSION),
    ("garbage", APP_VERSION), ("v99<script>", APP_VERSION),
    ("9" * 5000 + ".1", APP_VERSION),
])
def test_client_version_validation(value, expected):
    assert system_service.client_version(value) == expected


@pytest.mark.parametrize("endpoint", ["version", "update_check", "update_status"])
@pytest.mark.parametrize("installed,available", [("0.1.5", True), ("0.1.6", False)])
def test_all_update_routes_compare_the_client(monkeypatch, endpoint, installed, available):
    monkeypatch.setattr(system.setting_service, "update_channel", lambda db: {})
    monkeypatch.setattr(system.setting_service, "is_read_only", lambda db: False)
    monkeypatch.setattr(system.update_service, "last_downloaded", lambda: None)
    monkeypatch.setattr(system_service, "fetch_manifest", lambda url: {
        "version": "0.1.6", "url": "https://aohuang888.com/miaoshou-downloads/setup.exe",
    })
    request = Request({"type": "http", "headers": [(b"x-client-version", installed.encode())]})
    response = getattr(system, endpoint)(db=None, request=request)
    assert response["current"] == installed
    assert response["updateAvailable"] is available


def test_bad_or_absent_header_falls_back_without_contaminating_another_client(monkeypatch):
    monkeypatch.setattr(system_service, "fetch_manifest", lambda url: {
        "version": "0.1.6", "url": "https://aohuang888.com/miaoshou-downloads/setup.exe",
    })
    assert system_service.check_update(current_version="0.1.5")["updateAvailable"]
    assert system_service.check_update(current_version="invalid")["current"] == APP_VERSION
    assert system_service.check_update()["current"] == APP_VERSION
    assert system_service.check_update(current_version="0.1.6")["updateAvailable"] is False
