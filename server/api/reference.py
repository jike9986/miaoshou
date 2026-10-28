"""只读参考数据：仓库 / 品牌 / 制造商 / 负责人 / 类目发布要求 / 站点默认定价。

老版 CLI（`tk-product-automation/src/cli.py`）里这 6 个只读子命令
（warehouses :188、brands :221、manufacturers :225、responsible :229、
metadata :110、price-defaults :154）在新版一直没有路由，界面拿不到这些字典，
发布表单的下拉框只能靠用户手填。这里把「取数 + 归一化 + 缓存」集中成一层，
路由只负责解析参数，避免 5 个路由各写一遍缓存逻辑。

三条设计约束（都是踩过的坑）：

1. **只读，不落库**。这些是上游字典，缓存只进进程内存（services.cache），
   重启即失效，不会出现「库里存着三天前的品牌列表」。
2. **上游不支持不算故障**。某些站点/账号没有制造商或负责人（实测 MY 账号
   两个列表都返回空数组；老版对这类情况也只是空输出），所以上游明确回
   「不支持 / 未开通 / 无权限」时返回空列表 + `supported: false`，
   而不是 500 —— 前端据此把下拉框置灰，用户不会以为程序坏了。
3. **缓存只存成功结果**，且 `refresh=true` 直接绕过缓存（老版 `--refresh`
   的等价物）。上游这次失败但有旧值时给旧值并标 `degraded`，如实报出
   `staleSeconds`，不假装是新数据。
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from core import errors
from services import account_service
from services.cache import TTLCache, get_cache, ttl_from_env
from services.miaoshou import MiaoshouError

# 5 分钟：这些字典一天也变不了几次，但用户改完品牌/负责人会想立刻看到，
# 所以不能长到「必须重启进程」。要改设 AOHUANG_REFERENCE_CACHE_TTL（秒）。
REFERENCE_CACHE_TTL_ENV = "AOHUANG_REFERENCE_CACHE_TTL"
REFERENCE_CACHE_TTL_DEFAULT = 300.0
REFERENCE_CACHE_MAX_ENTRIES = 256

# 品牌全量翻页的保险丝默认值：上游一页 100 行，20 页 = 2000 个品牌。
# 为什么不是无限：nextPageToken 是服务端编码的游标（实测解出来是
# `page_number=2`），一旦上游 bug 让它不收敛，没有上限就是死循环 + 打爆配额。
# 2000 已经覆盖实测 totalCount=10000 场景下绝大多数真实店铺，撞到上限会
# 明确回 truncated=true，前端可以提示「未翻完」而不是假装拿到了全部。
BRAND_MAX_PAGES = 20

_CACHE: TTLCache | None = None


class UpstreamUnsupported(RuntimeError):
    """上游明确表示「这个接口/这个站点没有这项数据」。

    单独一个异常类型，是为了让缓存层**不要**拿旧值兜底：兜底会把
    「不支持」掩盖成「有一份过期数据」，前端反而更难判断。
    """

    def __init__(self, reason: str, *, code: str = "", message: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code
        self.message = message


def _cache() -> TTLCache:
    global _CACHE
    if _CACHE is None:
        _CACHE = get_cache("reference", max_entries=REFERENCE_CACHE_MAX_ENTRIES,
                           default_ttl=REFERENCE_CACHE_TTL_DEFAULT)
    return _CACHE


def cache_ttl() -> float:
    return ttl_from_env(REFERENCE_CACHE_TTL_ENV, REFERENCE_CACHE_TTL_DEFAULT)


def account_prefix(account: Any) -> str:
    return f"acct:{getattr(account, 'id', 0)}:"


# --- 参数解析 ---------------------------------------------------------------
def parse_shop_id(value: Any) -> int:
    """shopId 必须能转成 int：上游 body 里是数字，透传字符串会被判参数非法。

    不在这里拦的话，`int("abc")` 会在客户端方法里抛 ValueError，
    被 FastAPI 兜成 500 —— 用户只是少打一个数字，不该看到服务器错误。
    """
    text = str(value or "").strip()
    if not text:
        raise errors.validation("缺少 shopId 参数", field="shopId")
    try:
        number = int(text)
    except (TypeError, ValueError):
        raise errors.validation("shopId 必须是整数", field="shopId",
                                value=value) from None
    if number <= 0:
        raise errors.validation("shopId 必须是正整数", field="shopId", value=number)
    return number


def parse_shop_ids(value: Any) -> list[int]:
    """`shopIds=1,2` / 重复参数两种写法都吃，去重保序。"""
    if value in (None, ""):
        raise errors.validation("缺少 shopIds 参数", field="shopIds")
    parts: list[str] = []
    if isinstance(value, (list, tuple)):
        for item in value:
            parts.extend(str(item).split(","))
    else:
        parts.extend(str(value).split(","))
    result: list[int] = []
    for part in parts:
        text = part.strip()
        if not text:
            continue
        try:
            number = int(text)
        except (TypeError, ValueError):
            raise errors.validation("shopIds 必须是逗号分隔的整数",
                                    field="shopIds", value=value) from None
        if number <= 0:
            raise errors.validation("shopIds 必须是正整数", field="shopIds",
                                    value=number)
        if number not in result:
            result.append(number)
    if not result:
        raise errors.validation("缺少 shopIds 参数", field="shopIds")
    return result


def parse_cid(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        raise errors.validation("缺少 cid 参数", field="cid")
    try:
        number = int(text)
    except (TypeError, ValueError):
        raise errors.validation("cid 必须是整数", field="cid", value=value) from None
    if number <= 0:
        raise errors.validation("cid 必须是正整数", field="cid", value=number)
    return number


# --- 上游「不支持」判定 -----------------------------------------------------
# 只认这些明确的语义。**不能**把任意业务失败都算成「不支持」，
# 否则凭据错、限流、参数错都会被伪装成「这个站点没有这项数据」。
_UNSUPPORTED_CODES = {"notsupport", "unsupported", "not_support", "not_open"}
_UNSUPPORTED_HINTS = ("不支持", "暂不支持", "未开通", "未开放", "无权限",
                      "没有权限", "not support", "unsupported", "not authorized")


def unsupported_reason(exc: MiaoshouError) -> str | None:
    """上游错误是不是「不支持」；是就返回一句人话，否则 None。"""
    code = str(getattr(exc, "code", "") or "").strip().lower()
    text = str(exc).lower()
    if code in _UNSUPPORTED_CODES:
        return "上游不支持该查询"
    for hint in _UNSUPPORTED_HINTS:
        if hint.lower() in text:
            return f"上游不支持该查询：{exc}"
    return None


def _load(loader: Callable[[], Any]) -> Any:
    """执行一次上游调用，把「不支持」翻译成独立异常。"""
    try:
        return loader()
    except MiaoshouError as exc:
        reason = unsupported_reason(exc)
        if reason is not None:
            raise UpstreamUnsupported(reason, code=exc.code,
                                     message=str(exc)) from None
        raise


# --- 缓存读写 ---------------------------------------------------------------
def cached(account: Any, key: str, loader: Callable[[], Any], *,
           refresh: bool,
           cacheable: Callable[[Any], bool] | None = None
           ) -> tuple[Any, bool, bool, int]:
    """读缓存 → 回源 → 写缓存。

    返回 ``(value, cached, degraded, stale_seconds)``：

    * ``cached``：这次是否用了缓存（新鲜命中或旧值兜底）；
    * ``degraded``：上游这次失败了，返回的是旧值；
    * ``stale_seconds``：旧值年龄，``degraded=False`` 时恒为 0。

    `refresh=True` 跳过读取，但仍回写缓存 —— 否则「刷新一次」会把缓存清空，
    紧接着的另一次请求又得回源。

    `cacheable`：这次的结果能不能进缓存。默认全进；品牌全量翻页传进来，
    用来把「翻到一半失败」的结果挡在缓存外（见 api/shops.py 的注释）。
    """
    store = _cache()
    full_key = f"{account_prefix(account)}{key}"
    ttl = cache_ttl()
    allow_store = cacheable or (lambda _value: True)

    if refresh:
        value = _load(loader)
        if allow_store(value):
            store.set(full_key, value, ttl)
        return value, False, False, 0

    entry = store.lookup(full_key)
    if entry is not None and entry.is_fresh():
        return entry.value, True, False, 0
    try:
        value = _load(loader)
    except MiaoshouError:
        # 只有真正的上游故障才用旧值兜底；「不支持」由 UpstreamUnsupported
        # 冒泡出去，不走这条分支。
        if entry is None:
            raise
        return entry.value, True, True, int(entry.age_seconds)
    if allow_store(value):
        store.set(full_key, value, ttl)
    return value, False, False, 0


def envelope(account: Any, *, cached_flag: bool, degraded: bool,
             stale_seconds: int) -> dict[str, Any]:
    """每条参考数据响应共用的头部字段。"""
    return {
        "accountId": str(getattr(account, "id", "")),
        "supported": True,
        "cached": bool(cached_flag),
        "degraded": bool(degraded),
        "staleSeconds": int(stale_seconds),
        "cacheTtl": int(cache_ttl()),
    }


def unsupported_envelope(account: Any, reason: str, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "accountId": str(getattr(account, "id", "")),
        "supported": False,
        "reason": reason,
        "cached": False,
        "degraded": False,
        "staleSeconds": 0,
    }
    body.update(extra)
    return body


# --- 归一化 -----------------------------------------------------------------
def _data_of(response: Mapping[str, Any]) -> Mapping[str, Any]:
    data = (response or {}).get("data")
    return data if isinstance(data, Mapping) else {}


def warehouse_rows(response: Mapping[str, Any]) -> list[dict[str, Any]]:
    """把 shopWarehouseList 拍平成「一行一个仓库」。

    老版 cmd_warehouses（cli.py:188-207）就是这么拍的：上游按店铺分组，
    界面要的是一张能直接渲染的表。字段名沿用老版，避免前端两套命名。
    """
    data = _data_of(response)
    groups = (data.get("shopWarehouseList") or data.get("list")
              or data.get("records") or [])
    rows: list[dict[str, Any]] = []
    if not isinstance(groups, list):
        return rows
    for shop in groups:
        if not isinstance(shop, Mapping):
            continue
        warehouses = shop.get("warehouseList") or []
        if not isinstance(warehouses, list):
            continue
        for warehouse in warehouses:
            if not isinstance(warehouse, Mapping):
                continue
            rows.append({
                "shopId": str(shop.get("shopId") or warehouse.get("shopId") or ""),
                "shopName": shop.get("shopName") or "",
                "site": str(shop.get("site") or "").upper(),
                "warehouseId": str(warehouse.get("warehouseId") or ""),
                "warehouseName": warehouse.get("warehouseName") or "",
                # 上游给的是字符串 "1"/"0"，这里同时给布尔，省得前端到处比较字符串
                "isDefault": str(warehouse.get("isDefault")) == "1",
                "isDefaultRaw": warehouse.get("isDefault"),
                "effectStatus": warehouse.get("warehouseEffectStatus"),
                "subType": warehouse.get("warehouseSubType"),
                "inventoryRule": warehouse.get("inventoryRule"),
            })
    return rows


def brand_rows_from_items(items: Any) -> list[dict[str, Any]]:
    """上游 brandList 数组 → 界面行。

    单独拆出来是因为分页后有两个入口：单页拿的是上游信封（走 brand_rows），
    翻完所有页拿的是已经拍平的 items 列表（走这个）。两者必须是同一套归一化，
    否则「加载更多」和「全量」两条路径给出的行字段会不一样。
    """
    if not isinstance(items, list):
        return []
    return [
        {
            "brandId": str(row.get("brandId") or ""),
            "brandName": row.get("brandName") or "",
            "authorizedStatus": row.get("authorizedStatus") or "",
            # 授权状态是发布前必须看的：UNAUTHORIEZD 的品牌上传会被平台拒
            "authorized": str(row.get("authorizedStatus") or "").upper() == "AUTHORIZED",
        }
        for row in items if isinstance(row, Mapping)
    ]


def brand_rows(response: Mapping[str, Any]) -> list[dict[str, Any]]:
    data = _data_of(response)
    return brand_rows_from_items(data.get("brandList") or [])


def brand_cache_key(shop_id: int, *, page_token: str = "", all_pages: bool = False,
                    name: str = "") -> str:
    """品牌缓存的 key。

    游标 / all / name 三者任一不同，上游返回的就是**另一批**数据，所以三个
    都必须进 key。少了 pageToken 的后果很具体：第二页请求会命中第一页的
    缓存，前端「加载更多」永远拿到同一批 100 行，翻页形同不存在 —— 这个 bug
    在缓存层不会报错，只会静默返回错数据，比抛异常难查得多。
    name 同理：搜「abc」不能命中不带 name 的全量缓存，否则搜索结果里全是
    没筛过的品牌。
    """
    return (f"brands|shop={int(shop_id)}"
            f"|all={1 if all_pages else 0}"
            f"|token={page_token or '-'}"
            f"|name={name or '-'}")


def brand_page_view(page: Mapping[str, Any], *, all_pages: bool = False) -> dict[str, Any]:
    """把 brand_list_page / brand_list_all 的返回值翻译成路由响应字段。

    两件事在这里统一做，路由只管取数：

    * `brands` 用与单页旧实现完全相同的归一化（brand_rows_from_items），
      保证向后兼容 —— 老前端只认 brands 字段；
    * `truncated` 的语义分两种：单页模式下「还有下一页」就是 truncated，
      全量模式下只有「撞到 max_pages 或中途失败」才算 truncated。
      用同一个词表达两件事会误导人，所以全量模式额外给 `complete`。
    """
    items = page.get("items") or []
    rows = brand_rows_from_items(items)
    next_token = str(page.get("nextPageToken") or "")
    total = page.get("totalCount")
    total = total if isinstance(total, int) else None
    if all_pages:
        truncated = bool(page.get("truncated"))
        pages = int(page.get("pages") or 0)
        error = str(page.get("error") or "")
    else:
        truncated = bool(next_token)
        # 单页模式每次请求恰好拉一页；即使上游回空数组也是「拉过一页」，
        # 不能写 0 —— 0 会被前端理解成「还没开始翻」。
        pages = 1
        error = ""
    return {
        "brands": rows,
        "nextPageToken": next_token or None,
        "totalCount": total,
        "truncated": truncated,
        "pages": pages,
        # 单页模式返回的 brandList 已经是「一页」，complete 只对全量模式有意义
        "complete": (not truncated) if all_pages else None,
        "error": error or None,
    }


def brand_paging(response: Mapping[str, Any]) -> dict[str, Any]:
    """品牌列表是**分页**的：实测 MY 店铺上游 totalCount=10000，一页只回 100 条。

    老版 CLI 只打印 data，从不看 nextPageToken，所以「品牌搜不到」这个问题
    在老版也存在。这里把游标和总数一起回给界面：前端至少能显示
    「共 10000 个，当前 100 个」，而不是让用户以为店铺只有 100 个品牌。
    """
    data = _data_of(response)
    rows = data.get("brandList") or []
    total = data.get("totalCount")
    return {
        "totalCount": total if isinstance(total, int) else None,
        "returned": len(rows) if isinstance(rows, list) else 0,
        "nextPageToken": data.get("nextPageToken") or None,
        "truncated": bool(data.get("nextPageToken")),
    }


def _pass_through(response: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    """制造商 / 负责人的条目结构上游没给文档，原样透传，不猜字段名。

    这两个接口在实测账号（MY）上返回空数组，没有样本可以照着归一化；
    乱猜键名会把真实数据吃掉，所以这里只做「必须是对象」的过滤。
    """
    data = _data_of(response)
    rows = data.get(key) or []
    if not isinstance(rows, list):
        return []
    return [dict(row) for row in rows if isinstance(row, Mapping)]


def manufacturer_rows(response: Mapping[str, Any]) -> list[dict[str, Any]]:
    return _pass_through(response, "manufacturerList")


def responsible_rows(response: Mapping[str, Any]) -> list[dict[str, Any]]:
    return _pass_through(response, "responsiblePersonList")


def metadata_summary(response: Mapping[str, Any]) -> dict[str, Any]:
    """类目发布要求：老版 cmd_metadata（cli.py:110-141）的同一套摘要。

    `requirements` 那 4 个布尔是发布前必看的（包裹尺寸/制造商/负责人/尺码图
    是否必填）；`mandatoryAttributes` 是要用户填的属性清单。
    """
    data = _data_of(response)
    meta = data.get("categoryMetadata") or data.get("metadata") or data
    if not isinstance(meta, Mapping):
        meta = {}
    config = meta.get("categoryConfig") or {}
    if not isinstance(config, Mapping):
        config = {}
    sale_attrs = meta.get("categorySaleAttrList") or []
    product_attrs = meta.get("categoryProductAttrList") or []
    if not isinstance(sale_attrs, list):
        sale_attrs = []
    if not isinstance(product_attrs, list):
        product_attrs = []
    mandatory = [
        {"id": attr.get("attrId"), "name": attr.get("name"),
         "nameAlias": attr.get("attributeNameAlias"),
         "type": attr.get("attributeType")}
        for attr in [*sale_attrs, *product_attrs]
        if isinstance(attr, Mapping) and attr.get("isMandatory")
    ]
    return {
        "categoryConfig": dict(config),
        "saleAttributeCount": len(sale_attrs),
        "productAttributeCount": len(product_attrs),
        "mandatoryAttributes": mandatory,
        "requirements": {
            "packageDimensionRequired": config.get("packageDimensionIsRequired"),
            "manufacturerRequired": config.get("manufacturerIsRequired"),
            "responsiblePersonRequired": config.get("responsiblePersonIsRequired"),
            "sizeChartRequired": config.get("sizeChartIsRequired"),
        },
        # 完整属性清单留给表单渲染用：摘要里只有必填项，下拉选项在 values 里
        "categorySaleAttrList": sale_attrs,
        "categoryProductAttrList": product_attrs,
    }


def price_defaults_view(response: Mapping[str, Any],
                        site: str) -> dict[str, Any]:
    """站点默认定价：整表透传 + 按站点切出这一份。

    老版 `cmd_price_defaults`（cli.py:154-158）直接把 `data` 整个打出来，
    因为上游一次返回所有站点的费率（实测 22 个站点 + TIKTOKGLOBAL 兜底）。
    界面真正需要的是「当前站点那几项」，所以这里额外给一个 `defaults`：

    * 物流默认运费（本地币 / 折人民币）
    * 平台基础服务费
    * 平台支持费
    * 各项扣点百分比（platformChargePercent / vatChargePercent / ...）

    一个字段都取不到时 `defaults` 为 None —— 站点没有单独配置是正常情况
    （上游那张表里大量站点是全 0），不能当成错误。
    """
    data = _data_of(response)
    code = site.upper()

    def pick(key: str) -> Any:
        table = data.get(key)
        if isinstance(table, Mapping):
            return table.get(code)
        return None

    charges = pick("tiktokCbSiteAndDefaultChargePercentMap")
    view = {
        "logisticCharge": pick("siteAndBuyerLogisticDefaultChargeMap"),
        "logisticChargeCNY": pick("siteAndBuyerLogisticDefaultChargeCNYMap"),
        "platformInfrastructureCharge":
            pick("siteAndDefaultPlatformInfrastructureChargeMap"),
        "platformInfrastructureChargeCNY":
            pick("siteAndDefaultPlatformInfrastructureChargeCNYMap"),
        "platformSupportCharge": pick("siteAndDefaultPlatformSupportChargeMap"),
        "platformSupportChargeCNY": pick("siteAndDefaultPlatformSupportChargeCNYMap"),
        "chargePercent": charges if isinstance(charges, Mapping) else None,
        # 跨境站点的公共兜底费率：某个站点在表里没条目时用它
        "fallbackChargePercent": data.get("tiktokLocalDefaultChargePercentMap")
        if isinstance(data.get("tiktokLocalDefaultChargePercentMap"), Mapping) else None,
    }
    has_any = any(value not in (None, {}) for value in view.values())
    return {
        "defaults": view if has_any else None,
        "siteConfigured": has_any,
        # 整表也带上：界面切站点不必再打一次接口
        "allSites": data,
    }


def resolve_account_for(db: Any, account_id: Any,
                        credentials: tuple[str, str] | None) -> Any:
    """取账号（要有客户端才能查上游）。

    走 `account_service.resolve_account`：内部就是 `require_account`
    （没连接 → 401 AUTH_REQUIRED），并额外支持共享服务器模式下请求头自带凭据。
    直接调 `api.deps.require_account` 反而会漏掉共享模式。
    """
    return account_service.resolve_account(db, account_id, credentials)


__all__ = [
    "BRAND_MAX_PAGES",
    "UpstreamUnsupported",
    "account_prefix",
    "brand_cache_key",
    "brand_page_view",
    "brand_paging",
    "brand_rows",
    "brand_rows_from_items",
    "cache_ttl",
    "cached",
    "envelope",
    "manufacturer_rows",
    "metadata_summary",
    "parse_cid",
    "parse_shop_id",
    "parse_shop_ids",
    "price_defaults_view",
    "resolve_account_for",
    "responsible_rows",
    "unsupported_envelope",
    "unsupported_reason",
    "warehouse_rows",
]
