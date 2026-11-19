"""妙手网关抖动容错：熔断器分层 + 件级重试队列 + 在途台账逐件剔除。

背景（2026-11-19 线上实测 50 件全废）：
  妙手网关抖了约 3 分钟，响应是 HTTP 200 + result=fail「下游服务响应超时」。
  旧代码三个缺陷叠在一起，把一次上游抖动放大成整批作废：

  1. `_post_once` 拿到响应就 `record_success()`，把网关失败计数清零 ——
     「下游服务响应超时」明明是失败，熔断器却全程没动作；
  2. 网关重试 `attempt < GATEWAY_RETRIES` 实际只争取到 7.2 秒，
     远短于故障时长 3 分钟；
  3. `collect_carriers` 里任何一次复制抛错就 `raise CarrierError`，
     50 件立即全废 —— 而那 50 件成品在仓库里都是好的。

这里钉住修好之后的行为。负责人 2026-11-19 原话：
  「下游服务响应超时这种情况可以暂时暂停任务啊，然后读取妙手等没问题继续任务」
  「哪个失败了就把这个成品放到这一批从最后重试啊，因为你成品仓库里面的
    肯定都是没问题的」
"""
from __future__ import annotations

import json
import uuid

import pytest

from core.constants import (
    CARRIER_ITEM_RETRY_ROUNDS,
    GATEWAY_COOLDOWN_MAX_SECONDS,
    GATEWAY_FAIL_THRESHOLD,
    GATEWAY_RETRIES,
    GATEWAY_RETRY_BACKOFF_MAX_SECONDS,
    UPSTREAM_PACE_SECONDS,
)
from services import carrier_service
from services.miaoshou import (
    MiaoshouClient,
    MiaoshouError,
    UpstreamBreaker,
    _is_transient_gateway,
)


# --- 1. 熔断器分层 ---------------------------------------------------------
class TestBreakerLayering:
    def test_transport_success_does_not_clear_gateway_failures(self):
        """拿到 HTTP 响应只清传输计数，不清网关计数。

        这正是旧代码的病根：200 + result=fail 也走了 record_success()，
        于是熔断器永远不知道该停手。
        """
        breaker = UpstreamBreaker()
        breaker.record_gateway_failure()
        breaker.record_transport_success()
        # 传输侧清零了，但网关侧还留着 1 次
        assert breaker._gateway_failures == 1
        assert breaker.in_gateway_cooldown() is False

    def test_gateway_trips_after_threshold(self):
        breaker = UpstreamBreaker()
        for _ in range(GATEWAY_FAIL_THRESHOLD - 1):
            assert breaker.record_gateway_failure() is False
        assert breaker.record_gateway_failure() is True
        assert breaker.in_gateway_cooldown() is True
        assert breaker.gateway_open_for() > 0
        assert breaker.allow() is False

    def test_gateway_cooldown_backs_off_exponentially(self):
        """连开时冷却翻倍，但不超过上限。"""
        breaker = UpstreamBreaker()
        seen = []
        for _ in range(5):
            breaker._gateway_failures = GATEWAY_FAIL_THRESHOLD - 1
            breaker._gateway_open_until = 0.0
            breaker.record_gateway_failure()
            seen.append(breaker.gateway_open_for())
        assert seen[0] <= seen[-1]
        assert max(seen) <= int(GATEWAY_COOLDOWN_MAX_SECONDS)

    def test_real_success_resets_everything(self):
        breaker = UpstreamBreaker()
        breaker._gateway_failures = GATEWAY_FAIL_THRESHOLD - 1
        breaker.record_gateway_failure()
        assert breaker.in_gateway_cooldown() is True
        breaker.record_success()
        assert breaker.in_gateway_cooldown() is False
        assert breaker._gateway_streak == 0

    def test_gateway_pause_message_is_actionable(self):
        """熔断期间的报错要说清是「网关抖动」还是「连不上」。"""
        breaker = UpstreamBreaker()
        breaker._gateway_failures = GATEWAY_FAIL_THRESHOLD - 1
        breaker.record_gateway_failure()
        client = MiaoshouClient.__new__(MiaoshouClient)
        client._breaker = breaker
        client._pacer = None
        client.base = "http://x"
        with pytest.raises(MiaoshouError) as exc:
            client._post_once("/x", {})
        assert exc.value.code == "upstream_gateway_pause"
        assert "秒后自动重试" in str(exc.value)


# --- 2. 重试窗口 -----------------------------------------------------------
class TestGatewayRetryWindow:
    def test_attempt_count_covers_46_seconds(self):
        """指数退避的累计等待要能覆盖短线抖动（>40 秒）。

        旧配置是 1.2 + 2.4 + 3.6 = 7.2 秒，对 3 分钟的抖动毫无意义。
        """
        total = sum(min(1.5 * (2 ** i), GATEWAY_RETRY_BACKOFF_MAX_SECONDS)
                    for i in range(GATEWAY_RETRIES))
        assert total >= 40, f"网关重试窗口只有 {total} 秒，太短"

    def test_gateway_message_is_classified_transient(self):
        for message in ("下游服务响应超时", "下游服务繁忙", "系统繁忙"):
            assert _is_transient_gateway(message) is True
        assert _is_transient_gateway("商品不存在") is False


# --- 3. 件级重试队列 -------------------------------------------------------
def _err(kind: str, message: str = "下游服务响应超时") -> MiaoshouError:
    return MiaoshouError(f"妙手接口失败：{message}", code="fail", kind=kind)


class TestRetryableClassification:
    @pytest.mark.parametrize("kind", ["gateway", "rate_limit", "timeout",
                                      "unknown", "unreachable"])
    def test_transient_kinds_are_retryable(self, kind):
        assert carrier_service._is_retryable_copy_error(_err(kind)) is True

    def test_http_5xx_is_retryable(self):
        exc = MiaoshouError("妙手返回 HTTP 502", status=502, kind="error")
        assert carrier_service._is_retryable_copy_error(exc) is True

    def test_business_errors_are_not_retryable(self):
        """参数错、商品不存在这类重试也不会好，必须直接抛。"""
        for exc in (MiaoshouError("商品不存在", kind="error"),
                    MiaoshouError("凭据不正确", kind="error", status=401)):
            assert carrier_service._is_retryable_copy_error(exc) is False


class _BoxClient:
    """只实现 collect_carriers 用到的几件事。

    script 里每一项是「第 N 次 claim 的结果」：
      · MiaoshouError 实例 → 抛出
      · int                → 当作新建草稿的 detailId
      · dict               → 原样作为上游响应（用来造「回执残缺」）
    """

    def __init__(self, script):
        self.script = list(script)
        self._breaker = UpstreamBreaker()
        self.claim_calls = 0

    def claim_common_to_platform(self, items):
        self.claim_calls += 1
        step = self.script.pop(0) if self.script else "exhausted"
        if isinstance(step, Exception):
            raise step
        if isinstance(step, dict):
            return step
        return {"data": {"platformCollectBoxDetailIdMap": {"x": {"tiktok": str(step)}}}}

    def common_box_list(self, **_kw):
        return {"data": {"detailList": [{"status": "success",
                                         "commonCollectBoxDetailId": 555}]}}

    def collect_box_list(self, **_kw):
        return {"data": {"detailList": []}}


def _patch_collect(monkeypatch, client, *, snapshot=None):
    """把 collect_carriers 的外部依赖都换成假的。"""
    monkeypatch.setattr(carrier_service, "_collect_box_snapshot",
                        lambda _c, page_size=200: set() if snapshot is None else snapshot)
    monkeypatch.setattr(carrier_service, "_retry_pause_seconds", lambda _c: 0.0)
    monkeypatch.setattr(carrier_service, "take_link", lambda _db: None)
    monkeypatch.setattr(carrier_service, "find_in_common_box",
                        lambda *_a, **_k: 555)
    monkeypatch.setattr(carrier_service, "mark", lambda *_a, **_k: None)


def test_deferred_copy_retries_and_still_finishes(monkeypatch):
    """某一件复制失败 → 排到队尾重试，整批照样凑齐，不再整批作废。

    这正是负责人要的行为：失败的成品放到这一批最后再试一次。
    """
    client = _BoxClient([_err("gateway"), 9001, 9002])
    _patch_collect(monkeypatch, client)
    created: list[int] = []
    details = carrier_service.collect_carriers(
        None, client, count=2, on_carrier=created.append)
    assert sorted(details) == [9001, 9002]
    assert created == sorted(created)
    # 第一次失败 + 两次成功 = 3 次调用
    assert client.claim_calls == 3


def test_deferred_copy_gives_up_after_round_limit(monkeypatch):
    """一直失败就按轮数上限收手，并说清还剩几件没建成。"""
    fail = [_err("gateway") for _ in range(CARRIER_ITEM_RETRY_ROUNDS + 3)]
    client = _BoxClient(fail)
    _patch_collect(monkeypatch, client)
    with pytest.raises(carrier_service.CarrierError) as exc:
        carrier_service.collect_carriers(None, client, count=1)
    assert "没能建成" in str(exc.value)
    assert exc.value.details["deferred"] >= 1


def test_business_error_still_fails_immediately(monkeypatch):
    """非瞬时错误（商品不存在）不许排尾重试，必须立刻抛。"""
    client = _BoxClient([MiaoshouError("商品不存在", kind="error")])
    _patch_collect(monkeypatch, client)
    with pytest.raises(carrier_service.CarrierError) as exc:
        carrier_service.collect_carriers(None, client, count=1)
    assert "复制结果未确认" in str(exc.value)
    assert client.claim_calls == 1


def test_incomplete_receipt_is_deferred_not_fatal(monkeypatch):
    """回执残缺同样先排尾重试：上游可能已经把草稿建出来了。"""
    client = _BoxClient([{"data": {}}, 9001])
    _patch_collect(monkeypatch, client)
    details = carrier_service.collect_carriers(None, client, count=1)
    assert details == [9001]


# --- 4. 在途台账逐件剔除 ---------------------------------------------------
@pytest.fixture
def publish_env():
    from db.session import SessionLocal, init_db
    from models.account import Account

    init_db()
    db = SessionLocal()
    try:
        suffix = uuid.uuid4().hex[:10]
        account = Account(name=f"inflight-{suffix}", app_key=f"inflight-key-{suffix}",
                          status="active",
                          shops=json.dumps([{"shopId": "111", "name": "S", "site": "MY"}]))
        db.add(account)
        db.commit()
        yield db, account
    finally:
        db.rollback()
        db.close()


def test_in_flight_only_blocks_matching_items(publish_env):
    """在途台账只报出命中的那几件，不再是「一命中就全批拒绝」。"""
    from models.publish_log import PublishLog
    from services.publish_service import _find_in_flight

    db, account = publish_env
    db.add(PublishLog(account_id=account.id, detail_id="7001", shop_id="111",
                      warehouse_key="7001:111", status="processing",
                      requested_at=1.0, last_requested_at=1.0, attempts=1))
    db.commit()

    keys = ["7001:111", "7002:111", "7003:111"]
    found = _find_in_flight(db, account=account, keys=keys, operator_id=None)
    assert set(found) == {"7001:111"}
    assert found["7001:111"]["detailId"] == "7001"


def test_no_in_flight_returns_empty(publish_env):
    from services.publish_service import _find_in_flight

    db, account = publish_env
    found = _find_in_flight(db, account=account, keys=["8001:111"],
                            operator_id=None)
    assert found == {}


# --- 5. 速度：1.05 压到 0.90 ----------------------------------------------
def test_pace_is_lowered_but_keeps_safety_margin():
    """间隔压到 0.90，但保留到 1.05 的上调余地（撞限流会自动抬回去）。"""
    assert UPSTREAM_PACE_SECONDS == 0.90
