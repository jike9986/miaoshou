"""妙手开放平台客户端（httpx 版）。

签名规则（与旧 miaoshou_client.generate_sign 完全一致，不可改）：

    content = appSecret + path + timestamp + appKey + bodyJson + appSecret
    sign    = hex(hmac_sha256(key=appSecret, msg=content))

请求头：x-app-key / x-timestamp / x-sign。

同账号请求必须经过 `Pacer`（默认 1.5s 间隔）：实测 1.0s 会触发
`accountApiQpsRateLimit`，1.5s 不触发。不同账号可并行。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
from collections.abc import Mapping
from typing import Any

import httpx

from core.config import get_settings
from core.constants import (
    MIAOSHOU_COLLECT_PAGE_SIZE,
    MIAOSHOU_SHOP_PAGE_SIZE,
    PUBLISH_MAX_ITEMS,
    UPSTREAM_PACE_SECONDS,
    UPSTREAM_RATE_LIMIT_BACKOFF,
    UPSTREAM_RATE_LIMIT_RETRIES,
)

SHOP_LIST_PATH = "/open/v1/product/shop/shop/get_shop_list"
COLLECT_BOX_BASE = "/open/v1/product/collect_box/tiktok/collect_box/"
PUBLISH_PATH = COLLECT_BOX_BASE + "save_move_collect_task"
PUBLISH_RECORDS_PATH = "/open/v1/product/collect_box/tiktok/move_collect/search_move_collect_list"
COMMON_BOX_BASE = "/open/v1/product/common_collect_box/common_collect_box/"
PRICE_TEMPLATE_BASE = "/open/v1/product/collect_box/tiktok/price_template/"
# 老版 :23：AI 类目匹配（ai_match_cid_by_shop_ids）。放在模块级常量区，
# 与其余 path 常量并排，避免散落在类体里被别的 agent 的改动冲掉。
MATCH_CATEGORY_PATH = COLLECT_BOX_BASE + "ai_match_cid_by_shop_ids"

SUPPORTED_TARGET_LANGUAGES = {"zh", "zh-tw", "en", "ms", "th", "vi", "id"}


class MiaoshouError(RuntimeError):
    """上游或传输错误。消息里不含凭据。"""

    def __init__(self, message: str, *, code: str = "", payload: Any = None,
                 status: int | None = None, kind: str = "error") -> None:
        super().__init__(message)
        self.code = code
        self.payload = payload
        self.status = status
        # error / rate_limit / timeout / unknown
        self.kind = kind


def generate_sign(app_secret: str, path: str, timestamp: int, app_key: str, body_json: str) -> str:
    """HMAC-SHA256 签名，与旧实现逐字节一致。"""
    content = f"{app_secret}{path}{timestamp}{app_key}{body_json}{app_secret}"
    return hmac.new(app_secret.encode("utf-8"), content.encode("utf-8"), hashlib.sha256).hexdigest()


class Pacer:
    """同账号节流：保证两次上游请求之间至少间隔 ``interval`` 秒。"""

    def __init__(self, interval: float = UPSTREAM_PACE_SECONDS) -> None:
        self.interval = interval
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delta = now - self._last
            if delta < self.interval:
                time.sleep(self.interval - delta)
            self._last = time.monotonic()


_PACERS: dict[str, Pacer] = {}
_PACER_LOCK = threading.Lock()


def pacer_for(key: str) -> Pacer:
    """按账号（appKey）取节流器；同账号共享，不同账号并行。"""
    with _PACER_LOCK:
        pacer = _PACERS.get(key)
        if pacer is None:
            pacer = Pacer()
            _PACERS[key] = pacer
        return pacer


# --- 连通性熔断 -------------------------------------------------------------
# 上游不可达时 httpx 每次都要等满 request_timeout（30s），用户点一下「采集箱」
# 就得干等十几秒才看到错误 —— 老版没这么慢，因为失败之后不该反复白等。
# 这里按账号记连续的网络类失败：连续 FAIL_THRESHOLD 次后进入 OPEN 状态，
# 在 COOLDOWN 秒内直接快速失败（毫秒级），冷却结束放一个探针请求过去试通。
# 只对「连不上/超时」这类传输错误计数；业务错误（凭据错、参数错）不计。
BREAKER_FAIL_THRESHOLD = 3
BREAKER_COOLDOWN_SECONDS = 60.0


class UpstreamBreaker:
    """按 key（base|appKey）维护的极简熔断器。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._failures = 0
        self._open_until = 0.0

    def allow(self) -> bool:
        """是否允许真正发起请求。OPEN 期间返回 False。"""
        with self._lock:
            if self._open_until <= 0:
                return True
            if time.monotonic() >= self._open_until:
                # 冷却结束：放一个探针，同时把窗口重新压住，避免并发涌入
                self._open_until = 0.0
                self._failures = BREAKER_FAIL_THRESHOLD - 1
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._open_until = 0.0

    def record_transport_failure(self) -> bool:
        """记一次传输失败；返回是否刚跳闸。"""
        with self._lock:
            self._failures += 1
            if self._failures >= BREAKER_FAIL_THRESHOLD and self._open_until <= 0:
                self._open_until = time.monotonic() + BREAKER_COOLDOWN_SECONDS
                return True
            return False

    def retry_after(self) -> int:
        """还要等多少秒才解禁（供错误文案用）。"""
        with self._lock:
            if self._open_until <= 0:
                return 0
            return max(0, int(round(self._open_until - time.monotonic())))

    def reset(self) -> None:
        with self._lock:
            self._failures = 0
            self._open_until = 0.0


_BREAKERS: dict[str, UpstreamBreaker] = {}
_BREAKER_LOCK = threading.Lock()


def breaker_for(key: str) -> UpstreamBreaker:
    with _BREAKER_LOCK:
        breaker = _BREAKERS.get(key)
        if breaker is None:
            breaker = UpstreamBreaker()
            _BREAKERS[key] = breaker
        return breaker


def system_proxy() -> str | None:
    """读 Windows「Internet 选项」里的系统代理。

    为什么必须自己读：httpx/requests 的 ``trust_env`` 只认 HTTP_PROXY /
    HTTPS_PROXY 环境变量，**不读注册表**。而本机（以及大量国内机器）是靠
    代理软件开系统代理上网的，注册表里有 ProxyEnable=1 / ProxyServer=127.0.0.1:7892，
    环境变量却是空的。结果就是：浏览器能开妙手后台，程序连不上，
    DNS 还把域名解析到代理的假 IP（198.18.x.x）导致 TLS 直接 EOF。

    优先环境变量（用户显式指定时以它为准），其次注册表，最后 None（直连）。
    """
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            if not enabled:
                return None
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
    except (OSError, ImportError, FileNotFoundError):
        return None
    server = str(server or "").strip()
    if not server:
        return None
    # 注册表可能是 "host:port"，也可能是 "http=host:port;https=host:port"
    if "=" in server:
        parts = dict(
            item.split("=", 1) for item in server.split(";") if "=" in item
        )
        server = (parts.get("https") or parts.get("http") or "").strip()
    if not server:
        return None
    if "://" not in server:
        server = "http://" + server
    # 本地回环地址不要走代理，否则自己打自己的后端也会被劫持
    return server


class MiaoshouClient:
    def __init__(self, app_key: str, app_secret: str, *, base: str | None = None,
                 pace: bool = True) -> None:
        self.app_key = app_key
        self.app_secret = app_secret
        self.base = (base or get_settings().miaoshou_base).rstrip("/")
        self.timeout = get_settings().request_timeout
        self.proxy = system_proxy()
        # trust_env=True 让 httpx 认环境变量；系统代理（注册表）由上面显式传进去
        self._client = httpx.Client(timeout=self.timeout, proxy=self.proxy, trust_env=True)
        self._pacer = pacer_for(f"{self.base}|{app_key}") if pace else None
        self._breaker = breaker_for(f"{self.base}|{app_key}")

    # --- 生命周期 -----------------------------------------------------------
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> MiaoshouClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # --- 传输 ---------------------------------------------------------------
    def _post_once(self, path: str, body: Mapping[str, Any] | None) -> dict[str, Any]:
        # 熔断打开时直接快速失败，不要再去等满 request_timeout
        if not self._breaker.allow():
            wait = self._breaker.retry_after()
            raise MiaoshouError(
                f"妙手连续连不上，已暂停请求（约 {wait} 秒后自动重试）",
                code="upstream_unreachable", kind="unreachable")
        payload = {k: v for k, v in (body or {}).items() if v is not None}
        body_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        timestamp = int(time.time())
        headers = {
            "Content-Type": "application/json",
            "x-app-key": self.app_key,
            "x-timestamp": str(timestamp),
            "x-sign": generate_sign(self.app_secret, path, timestamp, self.app_key, body_json),
        }
        if self._pacer is not None:
            self._pacer.wait()
        try:
            response = self._client.post(self.base + path, content=body_json.encode("utf-8"),
                                         headers=headers)
        except httpx.TimeoutException:
            self._breaker.record_transport_failure()
            raise MiaoshouError("妙手接口超时", kind="timeout") from None
        except httpx.HTTPError as exc:
            self._breaker.record_transport_failure()
            _name = type(exc).__name__
            # ConnectError / ConnectTimeout 这类是「根本连不上」，归成 unreachable，
            # 好让上层给出「网络不通」而不是含糊的「接口失败」。
            _kind = "unreachable" if "Connect" in _name else "error"
            raise MiaoshouError(f"请求妙手接口失败：{_name}", kind=_kind) from None

        raw = response.content
        # 拿到响应就说明网络通了，清掉传输失败计数（业务错误不算连通性问题）
        self._breaker.record_success()
        if response.status_code == 401:
            raise MiaoshouError("妙手拒绝了凭据（AppKey / AppSecret 不正确或已停用）",
                                code="invalid_app_key", status=401, kind="error")
        if response.status_code >= 400:
            code, message = "", f"妙手返回 HTTP {response.status_code}"
            try:
                data = json.loads(raw.decode("utf-8")) if raw else {}
                if isinstance(data, dict):
                    code = str(data.get("code") or "")
                    message = str(data.get("message") or data.get("msg") or message)
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
            raise MiaoshouError(f"妙手接口失败：{message}", code=code,
                                status=response.status_code, kind="error")
        if not raw:
            raise MiaoshouError("妙手接口返回空响应", kind="error")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise MiaoshouError("妙手接口返回的不是 JSON", kind="error") from None
        if not isinstance(data, dict):
            raise MiaoshouError("妙手接口返回结构异常", kind="error")
        code = str(data.get("code", ""))
        result_flag = str(data.get("result", "")).lower()
        if result_flag not in {"success", "ok"} and code not in {"0", "200", "success", ""}:
            message = str(data.get("message") or data.get("msg") or "妙手接口返回失败")
            kind = "rate_limit" if _is_rate_limit(code, message) else "error"
            raise MiaoshouError(f"妙手接口失败 [{code}]：{message}", code=code,
                                payload=data, kind=kind)
        return data

    def post(self, path: str, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """带限流重试的上游调用（重试 5 次，逐步退避）。"""
        last: MiaoshouError | None = None
        for attempt in range(UPSTREAM_RATE_LIMIT_RETRIES):
            try:
                return self._post_once(path, body)
            except MiaoshouError as exc:
                if exc.kind != "rate_limit":
                    raise
                last = exc
                time.sleep(UPSTREAM_RATE_LIMIT_BACKOFF * (attempt + 1))
        assert last is not None
        raise last

    # --- 店铺 ---------------------------------------------------------------
    def shop_list(self, *, site: str | None = None, page_no: int = 1,
                  page_size: int = MIAOSHOU_SHOP_PAGE_SIZE) -> dict[str, Any]:
        body: dict[str, Any] = {"platform": "tiktok", "pageNo": page_no, "pageSize": page_size}
        if site:
            body["site"] = site.upper()
        return self.post(SHOP_LIST_PATH, body)

    # --- 采集箱 -------------------------------------------------------------
    def collect_box_list(self, *, status: str | None = "notPublished",
                         keyword: str = "", page_no: int = 1,
                         page_size: int = MIAOSHOU_COLLECT_PAGE_SIZE) -> dict[str, Any]:
        body: dict[str, Any] = {"pageNo": page_no, "pageSize": page_size,
                                "filter": {"sourceItemIdKeyword": keyword or ""}}
        if status and status != "all":
            body["filter"]["status"] = status
        return self.post(COLLECT_BOX_BASE + "search_collect_box_detail_list", body)

    def shop_detail(self, detail_id: str | int, shop_id: str | int) -> dict[str, Any]:
        return self.post(COLLECT_BOX_BASE + "get_shop_collect_item_info",
                         {"detailId": int(detail_id), "shopId": int(shop_id)})

    def site_detail(self, detail_id: str | int, site: str) -> dict[str, Any]:
        return self.post(COLLECT_BOX_BASE + "get_site_collect_item_info",
                         {"detailId": int(detail_id), "site": site.upper()})

    def save_shop(self, detail_id: str | int, shop_id: str | int, oss_md5: str,
                  item: Mapping[str, Any]) -> dict[str, Any]:
        payload = dict(item)
        if payload.get("sizeChart") in (None, ""):
            payload.pop("sizeChart", None)
            payload.pop("sizeChartType", None)
        return self.post(COLLECT_BOX_BASE + "save_shop_collect_item_info", {
            "ossMd5": oss_md5,
            "detailId": int(detail_id),
            "shopId": int(shop_id),
            "shopCollectItemInfo": payload,
        })

    def claim_to_shop(self, detail_ids: list[int], shop_ids: list[int]) -> dict[str, Any]:
        return self.post(COLLECT_BOX_BASE + "claim_to_shop",
                         {"detailIds": list(detail_ids), "shopIds": list(shop_ids)})

    def delete_collect_box(self, detail_ids: list[int]) -> dict[str, Any]:
        return self.post(COLLECT_BOX_BASE + "delete_collect_box_detail",
                         {"detailIds": list(detail_ids)})

    def category_tree(self, site: str = "MY") -> dict[str, Any]:
        return self.post(COLLECT_BOX_BASE + "get_category_tree_by_site", {"site": site.upper()})

    def translate_collect_item(self, item: Mapping[str, Any], *, source_language: str = "auto",
                               target_language: str = "ms",
                               translate_sku_item_num: bool = False,
                               translate_sku_property: bool = True) -> dict[str, Any]:
        if source_language not in ({"auto"} | SUPPORTED_TARGET_LANGUAGES):
            raise MiaoshouError("源语言不受支持", kind="error")
        if target_language not in SUPPORTED_TARGET_LANGUAGES:
            raise MiaoshouError(f"目标语言不受支持：{target_language}", kind="error")
        return self.post(COLLECT_BOX_BASE + "translate_collect_item_info", {
            "sourceLanguage": source_language,
            "targetLanguage": target_language,
            "collectItemInfo": dict(item),
            "isTranslateSkuItemNum": int(translate_sku_item_num),
            "isTranslateSkuProperty": int(translate_sku_property),
        })

    def warehouse_list(self, shop_ids: list[int]) -> dict[str, Any]:
        return self.post(COLLECT_BOX_BASE + "get_shop_warehouse_list",
                         {"shopIds": [int(s) for s in shop_ids]})

    # --- 店铺参考数据（品牌 / 制造商 / 负责人） ------------------------------
    # 路径全部照抄老版 src/miaoshou_client.py，一个字符都不能猜：
    #   brand_list              → 老版 :289-290
    #   manufacturer_list       → 老版 :283-284
    #   responsible_person_list → 老版 :286-287
    # 这三个都是「店铺维度」的只读字典，用来填发布表单里的下拉框。
    def brand_list(self, shop_id: str | int) -> dict[str, Any]:
        return self.post(COLLECT_BOX_BASE + "get_brand_list", {"shopId": int(shop_id)})

    def manufacturer_list(self, shop_id: str | int, refresh: int = 0) -> dict[str, Any]:
        # refresh 是上游自己的缓存开关（0/1），不是本地缓存；老版 CLI 的
        # --refresh 就是把它透传下去，这里保持同样的语义。
        return self.post(COLLECT_BOX_BASE + "get_manufacturer_list",
                         {"shopId": int(shop_id), "refresh": int(refresh)})

    def responsible_person_list(self, shop_id: str | int,
                               refresh: int = 0) -> dict[str, Any]:
        return self.post(COLLECT_BOX_BASE + "get_responsible_person_list",
                         {"shopId": int(shop_id), "refresh": int(refresh)})

    # --- 类目发布要求 -------------------------------------------------------
    # 老版 :277-278。注意 cid 在 body 里是 int（老版显式 int(cid)），
    # 传字符串上游会返回参数不合法。
    def category_metadata(self, site: str, cid: str | int,
                          shop_ids: list[int]) -> dict[str, Any]:
        return self.post(COLLECT_BOX_BASE + "get_category_metadata",
                         {"site": site.upper(), "cid": int(cid),
                          "shopIds": [int(s) for s in shop_ids]})

    # --- AI 类目匹配 --------------------------------------------------------
    # 老版 :23 的 MATCH_CATEGORY_PATH + :233-239 的 match_category。
    # 上游是 `ai_match_cid_by_shop_ids`：给「采集箱明细 + 目标店铺」，让妙手自己
    # 猜一个 cid 出来。它**只给建议，不落库**（老版注释原话），所以可以放心
    # 在「商品没类目」时调它试探。
    #
    # 为什么限制 1–3 个店铺：上游接口按店铺逐个匹配，超过 3 个会直接报参数
    # 不合法（老版在客户端就拦了，不要在路由层才炸）。
    def match_category(self, detail_id: str | int,
                       shop_ids: list[int]) -> dict[str, Any]:
        """返回 AI 类目建议；不保存任何东西。"""
        wanted = [int(s) for s in shop_ids]
        if not wanted or len(wanted) > 3:
            raise MiaoshouError("类目匹配需要 1 到 3 个店铺", kind="error")
        return self.post(MATCH_CATEGORY_PATH, {
            "collectBoxDetailId": int(detail_id),
            "shopIds": wanted,
        })

    # --- 公共采集箱（载体重建） ---------------------------------------------
    def fetch_items(self, links: list[str]) -> dict[str, Any]:
        cleaned = [str(link).strip() for link in links if str(link).strip()]
        if not cleaned:
            raise MiaoshouError("采集链接不能为空", kind="error")
        if len(cleaned) > 50:
            raise MiaoshouError("一次最多采集 50 个链接", kind="error")
        return self.post(COMMON_BOX_BASE + "fetch_item", {"collectLinks": cleaned})

    def common_box_list(self, *, page_no: int = 1, page_size: int = 20, tab: str = "",
                        source_item_keyword: str = "") -> dict[str, Any]:
        body: dict[str, Any] = {"pageNo": page_no, "pageSize": page_size}
        wanted = {key: value for key, value in
                  (("tabPaneName", tab), ("sourceItemIdKeyword", source_item_keyword)) if value}
        if wanted:
            body["filter"] = wanted
        return self.post(COMMON_BOX_BASE + "get_common_collect_box_list", body)

    def common_box_detail(self, common_detail_id: int | str) -> dict[str, Any]:
        return self.post(COMMON_BOX_BASE + "get_common_collect_box_detail",
                         {"commonCollectBoxDetailId": int(common_detail_id)})

    def claim_common_to_platform(self, items: list[tuple[int, str, int]]) -> dict[str, Any]:
        if not items:
            raise MiaoshouError("请指定要认领的商品", kind="error")
        if len(items) > 100:
            raise MiaoshouError("一次最多认领 100 个商品", kind="error")
        payload = []
        for common_id, platform, copies in items:
            count = int(copies)
            if count < 1:
                raise MiaoshouError("复制数量至少为 1", kind="error")
            payload.append({"detailId": int(common_id),
                            "platform": str(platform or "tiktok"),
                            "serialNumber": count})
        return self.post(COMMON_BOX_BASE + "claimed",
                         {"detailSerialNumberPlatformList": payload})

    # --- 发布 ---------------------------------------------------------------
    def publish_items(self, detail_ids: list[int], shop_ids: list[int]) -> dict[str, Any]:
        """发布产品：把采集箱草稿移入店铺。发布不可撤销，调用方必须先过三重保险。"""
        if not detail_ids:
            raise MiaoshouError("发布需要至少一个 detailId", kind="error")
        if not shop_ids:
            raise MiaoshouError("发布需要至少一个 shopId", kind="error")
        if len(detail_ids) > PUBLISH_MAX_ITEMS:
            raise MiaoshouError(
                f"一次最多发布 {PUBLISH_MAX_ITEMS} 个商品，当前 {len(detail_ids)} 个",
                kind="error")
        return self.post(PUBLISH_PATH, {"detailIds": list(detail_ids),
                                        "shopIds": [int(s) for s in shop_ids]})

    def publish_records(self, *, page_no: int = 1, page_size: int = 20,
                        status: str | None = None, item_id: str | None = None,
                        source_item_id: str | None = None) -> dict[str, Any]:
        if not isinstance(page_no, int) or page_no < 1:
            raise MiaoshouError("pageNo 必须是不小于 1 的整数", kind="error")
        if not isinstance(page_size, int) or not 1 <= page_size <= 20:
            raise MiaoshouError("pageSize 必须是 1 到 20 的整数", kind="error")
        body: dict[str, Any] = {"pageNo": page_no, "pageSize": page_size}
        wanted = {key: value for key, value in
                  (("status", status), ("itemId", item_id), ("sourceItemId", source_item_id))
                  if value not in (None, "")}
        if wanted:
            body["filter"] = wanted
        return self.post(PUBLISH_RECORDS_PATH, body)

    # --- 定价模板 -----------------------------------------------------------
    def price_templates(self, *, site: str = "MY", name: str = "", page_no: int = 1,
                        page_size: int = 20) -> dict[str, Any]:
        if page_no < 1 or not 1 <= page_size <= 20:
            raise MiaoshouError("定价模板分页参数无效", kind="error")
        body: dict[str, Any] = {"site": site.upper(), "pageNo": page_no, "pageSize": page_size}
        if name:
            body["name"] = name
        return self.post(PRICE_TEMPLATE_BASE + "get_price_template_list", body)

    def price_site_defaults(self) -> dict[str, Any]:
        """站点默认定价（老版 :274-275）。

        body 是空对象，**不带 site**：老版就是这么调的，上游按「当前 appKey
        所属站点」返回，塞一个 site 进去反而可能被当成非法参数。所以路由层的
        site 只用于回显与校验，不下传。
        """
        return self.post(PRICE_TEMPLATE_BASE + "get_site_default_setting", {})

    # --- 品牌分页 -----------------------------------------------------------
    # 上游 `get_brand_list` 是**游标分页**：实测 MY 店铺一次回 101 行
    # （100 个品牌 + 1 行 brandId=0 的「无品牌」合计行），同时给出
    # `data.totalCount=10000` 和 `data.nextPageToken="cGFnZV9udW1iZXI9Mg=="`。
    # 老版 client（tk-product-automation/src/miaoshou_client.py:289-290）和它的
    # 唯一调用方（cli.py:213 `cmd_brands` → `_reference_list`）都只打印一页，
    # 从不看 nextPageToken，所以「第 101 个之后的品牌搜不到」在老版就存在。
    #
    # 翻页只能用 nextPageToken，不能用 pageNo：token 解出来是
    # `page_number=2` 这种被服务端编码过的游标，页码语义不在协议里。
    def brand_list_page(self, shop_id: str | int, *, page_token: str = "",
                        page_size: int = 100, name: str = "") -> dict[str, Any]:
        """拉一页品牌。返回 {items, nextPageToken, totalCount}。

        上游一页最多 100 行且必须靠 nextPageToken 翻页，页码翻页无效。
        """
        body: dict[str, Any] = {"shopId": int(shop_id)}
        # token 是服务端编码的不透明串，不做任何 trim / decode 以外的加工；
        # 空串表示「第一页」，此时绝对不能把 pageToken 塞进 body —— 上游会把
        # 空游标当成非法参数，而不是「从头开始」。
        if page_token:
            body["pageToken"] = page_token
        # 只有调用方显式改了页大小才传 pageSize：默认 100 时保持与老版 body
        # 逐字节一致（老版只发 shopId），避免多一个上游没文档的字段把请求打挂。
        if page_size != 100:
            body["pageSize"] = int(page_size)
        if name:
            body["name"] = name
        response = self.post(COLLECT_BOX_BASE + "get_brand_list", body)
        data = response.get("data") if isinstance(response, dict) else None
        data = data if isinstance(data, dict) else {}
        rows = data.get("brandList") or []
        token = data.get("nextPageToken") or ""
        total = data.get("totalCount")
        return {
            "items": rows if isinstance(rows, list) else [],
            "nextPageToken": str(token) if token else "",
            "totalCount": total if isinstance(total, int) else None,
        }

    def brand_list_all(self, shop_id: str | int, *, max_pages: int = 20,
                       name: str = "") -> dict[str, Any]:
        """翻完所有页。max_pages 是保险丝，防止上游 nextPageToken 不收敛时死循环。

        返回 {items, pages, truncated, totalCount}。
        truncated=True 表示撞到 max_pages 上限还没翻完。
        """
        if max_pages < 1:
            raise MiaoshouError("max_pages 必须是不小于 1 的整数", kind="error")
        items: list[dict[str, Any]] = []
        seen: set[str] = set()          # 品牌 id 去重：上游相邻页会重叠
        token = ""
        pages = 0
        total: int | None = None
        truncated = False
        error = ""
        while True:
            # 先判断保险丝再发请求：撞上限时不该多打一次上游
            if pages >= max_pages:
                truncated = True
                break
            try:
                page = self.brand_list_page(shop_id, page_token=token, name=name)
            except Exception as exc:  # noqa: BLE001
                # 第一页就失败 = 一条数据都没有，没有「保住已拿到的页」可言，
                # 直接上抛：路由层要把它翻成 502 / supported:false 的错误信封，
                # 而不是 200 + 空列表（那会让前端以为这个店真没有品牌）。
                if pages == 0:
                    raise
                # 翻到一半失败不能整体抛：前几页是真实数据，丢掉等于「本来能搜到
                # 的品牌又搜不到了」。把已拿到的页交出去，同时如实标 truncated。
                error = f"{type(exc).__name__}: {exc}"
                truncated = True
                break
            pages += 1
            if total is None:
                total = page.get("totalCount")
            for row in page.get("items") or []:
                if not isinstance(row, dict):
                    continue
                key = str(row.get("brandId") or "").strip()
                if key:
                    if key in seen:
                        continue
                    seen.add(key)
                items.append(row)
            token = str(page.get("nextPageToken") or "")
            if not token:
                break
        return {
            "items": items,
            "pages": pages,
            "truncated": truncated,
            "totalCount": total,
            "nextPageToken": token,
            "error": error,
        }


def _is_rate_limit(code: str, message: str) -> bool:
    text = f"{code} {message}".lower()
    return ("ratelimit" in text or "rate_limit" in text or "qps" in text
            or "频率" in message or "限流" in message or "too many" in text)
