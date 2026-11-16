"""定时任务：到期判断纯函数测试。"""
from datetime import date, datetime

from services.schedule_service import is_due, is_scheduled_day, should_run

# ---------------------------------------------------------------------------
# is_scheduled_day
# ---------------------------------------------------------------------------

class TestIsScheduledDay:
    def test_daily_always_true(self):
        task = {"frequency": "daily"}
        assert is_scheduled_day(task, date(2026, 11, 14)) is True

    def test_weekly_matches_correct_day(self):
        # 2026-11-16 是周一（isoweekday=1）
        task = {"frequency": "weekly", "days": [1, 3]}
        assert is_scheduled_day(task, date(2026, 11, 16)) is True

    def test_weekly_no_match(self):
        task = {"frequency": "weekly", "days": [2, 4]}
        assert is_scheduled_day(task, date(2026, 11, 16)) is False  # 周一

    def test_monthly_matches(self):
        task = {"frequency": "monthly", "dayOfMonth": [1, 15]}
        assert is_scheduled_day(task, date(2026, 11, 15)) is True

    def test_monthly_no_match(self):
        task = {"frequency": "monthly", "dayOfMonth": [1, 15]}
        assert is_scheduled_day(task, date(2026, 11, 14)) is False

    def test_once_exact_date(self):
        task = {"frequency": "once", "startDate": "2026-11-16"}
        assert is_scheduled_day(task, date(2026, 11, 16)) is True

    def test_once_wrong_date(self):
        task = {"frequency": "once", "startDate": "2026-11-16"}
        assert is_scheduled_day(task, date(2026, 11, 17)) is False

    def test_range_inside(self):
        task = {"frequency": "range", "startDate": "2026-11-01", "endDate": "2026-11-30"}
        assert is_scheduled_day(task, date(2026, 11, 14)) is True

    def test_range_outside(self):
        task = {"frequency": "range", "startDate": "2026-11-01", "endDate": "2026-11-13"}
        assert is_scheduled_day(task, date(2026, 11, 14)) is False

    def test_unknown_frequency_false(self):
        task = {"frequency": "hourly"}
        assert is_scheduled_day(task, date(2026, 11, 14)) is False


# ---------------------------------------------------------------------------
# is_due / should_run
# ---------------------------------------------------------------------------

class TestIsDue:
    def _task(self, **kwargs):
        base = {
            "enabled": True,
            "frequency": "daily",
            "time": "09:00",
            "lastRunDate": "",
            "attemptDate": "",
            "attempts": 0,
        }
        base.update(kwargs)
        return base

    def test_due_after_scheduled_time(self):
        task = self._task(time="09:00")
        now = datetime(2026, 11, 14, 9, 30)
        assert is_due(task, now) is True

    def test_not_due_before_time(self):
        task = self._task(time="09:00")
        now = datetime(2026, 11, 14, 8, 59)
        assert is_due(task, now) is False

    def test_not_due_already_ran_today(self):
        task = self._task(time="09:00", lastRunDate="2026-11-14")
        now = datetime(2026, 11, 14, 10, 0)
        assert is_due(task, now) is False

    def test_not_due_disabled(self):
        task = self._task(enabled=False)
        now = datetime(2026, 11, 14, 10, 0)
        assert is_due(task, now) is False

    def test_should_run_max_attempts(self):
        """当天失败次数达上限时不再运行。"""
        from core.constants import SCHEDULE_MAX_ATTEMPTS_PER_DAY
        task = self._task(
            attemptDate="2026-11-14",
            attempts=SCHEDULE_MAX_ATTEMPTS_PER_DAY,
        )
        now = datetime(2026, 11, 14, 10, 0)
        assert should_run(task, now) is False

    def test_should_run_first_attempt(self):
        task = self._task()
        now = datetime(2026, 11, 14, 10, 0)
        assert should_run(task, now) is True
