"""按目标店铺站点自动选定价模板。

负责人 2026-11-18：多店铺账号同时发 MY + PH 时，一套马来模板套到菲律宾
店铺会按 MYR 的费率结构算出 PHP 数字，报价必错。必须按站点各用各的模板。
"""
from unittest.mock import patch

from services import publish_service

STORED = {
    "马来": {"name": "马来", "site": "MY", "profitPercent": 15.0},
    "菲律宾": {"name": "菲律宾", "site": "PH", "profitPercent": 15.0},
    "泰国": {"name": "泰国", "site": "TH", "profitPercent": 15.0},
}


def _resolve(name, site):
    with patch.object(publish_service.setting_service, "price_templates",
                      return_value=STORED):
        return publish_service._template_name_for_site(None, name, site)


def test_keeps_template_when_site_matches():
    assert _resolve("马来", "MY") == "马来"
    assert _resolve("菲律宾", "PH") == "菲律宾"


def test_switches_to_same_site_template():
    """拿马来模板去发菲律宾店铺 → 自动换成菲律宾模板。"""
    assert _resolve("马来", "PH") == "菲律宾"
    assert _resolve("菲律宾", "MY") == "马来"
    assert _resolve("马来", "TH") == "泰国"


def test_keeps_original_when_no_template_for_that_site():
    """目标站点没有对应模板时原样返回，不做隐式猜测。"""
    assert _resolve("马来", "SG") == "马来"


def test_keeps_original_when_site_unknown():
    assert _resolve("马来", "") == "马来"
    assert _resolve("马来", None) == "马来"


def test_keeps_original_when_template_missing():
    """没选模板 / 模板不存在时不乱换。"""
    assert _resolve("", "PH") == ""
    assert _resolve(None, "PH") is None
    assert _resolve("不存在的模板", "PH") == "菲律宾"
