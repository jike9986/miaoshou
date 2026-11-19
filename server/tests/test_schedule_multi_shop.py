"""定时任务一次发多个店铺（targets）：纯函数 + 归一化测试。

负责人 2026-11-19：发布弹窗早就能一次发多店，定时任务却只能一个店铺。
这里钉住三件事：
  1. targets 为空的老任务必须仍然只按 shop_id 发（不能把老任务发成多店）；
  2. targets 里的第一个店铺要写回 shop_id / template_name，
     列表、日志、回执这些只认老字段的地方不用改；
  3. 每个店铺用它自己站点的模板，没填就回落到全局模板。
"""
from types import SimpleNamespace

import pytest

from services import schedule_service
from services.schedule_service import targets_of

pytestmark = pytest.mark.unit


def _row(**kwargs):
    """造一个只带 schedule 相关字段的假 Schedule 行。"""
    base = {"targets": None, "shop_id": "", "template_name": ""}
    base.update(kwargs)
    return SimpleNamespace(**base)


class TestTargetsOf:
    def test_old_task_falls_back_to_single_shop(self):
        """老任务（targets 为 None）只发 shop_id 那一个店铺。"""
        row = _row(shop_id="111", template_name="马来模板")
        assert targets_of(row) == [{"shopId": "111", "template": "马来模板"}]

    def test_multi_shop_targets_preserved_in_order(self):
        row = _row(
            shop_id="111",
            template_name="马来模板",
            targets=[{"shopId": "111", "template": "马来模板"},
                     {"shopId": "222", "template": "菲律宾模板"},
                     {"shopId": "333", "template": ""}],
        )
        assert [item["shopId"] for item in targets_of(row)] == ["111", "222", "333"]

    def test_blank_and_broken_entries_dropped(self):
        row = _row(shop_id="111", template_name="马来模板",
                   targets=[{"shopId": ""}, "not-a-dict", {"shopId": "222"}])
        assert [item["shopId"] for item in targets_of(row)] == ["222"]

    def test_empty_everything_returns_empty(self):
        """脏数据（连 shop_id 都没有）不能凭空造一个店铺出来。"""
        assert targets_of(_row()) == []


class TestNormalizeTargets:
    """normalize 里的 targets 解析：不连数据库，用假 account 拦在 require_shop 之前。"""

    def _normalize(self, monkeypatch, payload, existing=None):
        from core import errors

        account = SimpleNamespace(id=7, name="账号A", shops=[])

        def fake_get_account(db, account_id):
            return account

        def fake_require_shop(db, acc, shop_id):
            if str(shop_id) not in {"111", "222", "333"}:
                raise errors.validation("目标店铺不存在", field="shopId")

        monkeypatch.setattr(schedule_service, "get_account", fake_get_account)
        # normalize 只做格式校验，店铺归属在 create/update 里核，这里不碰 DB
        return schedule_service.normalize(None, payload, existing=existing)

    def test_first_target_wins_legacy_columns(self, monkeypatch):
        fields = self._normalize(monkeypatch, {
            "accountId": "7",
            "targets": [{"shopId": "222", "template": "菲律宾模板"},
                        {"shopId": "111", "template": "马来模板"}],
            "template": "马来模板",
            "frequency": "daily",
            "perRun": 5,
            "time": "09:00",
            "selectedProductIds": ["1"],
            "plannedRuns": 3,
        })
        assert fields["shop_id"] == "222"
        assert fields["template_name"] == "菲律宾模板"
        assert fields["targets"] == [
            {"shopId": "222", "template": "菲律宾模板"},
            {"shopId": "111", "template": "马来模板"},
        ]

    def test_missing_target_template_falls_back_to_global(self, monkeypatch):
        fields = self._normalize(monkeypatch, {
            "accountId": "7",
            "targets": [{"shopId": "111"}, {"shopId": "222", "template": "菲律宾模板"}],
            "template": "马来模板",
            "frequency": "daily",
            "perRun": 5,
            "time": "09:00",
            "selectedProductIds": ["1"],
            "plannedRuns": 3,
        })
        assert [item["template"] for item in fields["targets"]] == [
            "马来模板", "菲律宾模板"]

    def test_duplicate_shop_kept_once(self, monkeypatch):
        fields = self._normalize(monkeypatch, {
            "accountId": "7",
            "targets": [{"shopId": "111", "template": "马来模板"},
                        {"shopId": "111", "template": "马来模板"}],
            "template": "马来模板",
            "frequency": "daily",
            "perRun": 5,
            "time": "09:00",
            "selectedProductIds": ["1"],
            "plannedRuns": 3,
        })
        assert len(fields["targets"]) == 1

    def test_invalid_shop_id_rejected(self, monkeypatch):
        from core import errors

        with pytest.raises(errors.AppError):
            self._normalize(monkeypatch, {
                "accountId": "7",
                "targets": [{"shopId": "abc"}],
                "template": "马来模板",
                "frequency": "daily",
                "perRun": 5,
                "time": "09:00",
                "selectedProductIds": ["1"],
                "plannedRuns": 3,
            })

    def test_no_targets_keeps_single_shop(self, monkeypatch):
        fields = self._normalize(monkeypatch, {
            "accountId": "7",
            "shopId": "111",
            "template": "马来模板",
            "frequency": "daily",
            "perRun": 5,
            "time": "09:00",
            "selectedProductIds": ["1"],
            "plannedRuns": 3,
        })
        assert fields["targets"] is None
        assert fields["shop_id"] == "111"
