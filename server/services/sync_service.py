"""同步业务层：定价模板回源、货源链接回收。

两条能力都来自老版，是「补齐缺失接口」而非新功能：

  · 定价模板（老版 `/api/price-sync`）：只读调用妙手
    `price_template/get_price_template_list`，把模板列表交给前端做下拉。
    结果**不落库**——上游模板是快照，落库会与本地 `settings['pricing.templates']`
    里的自建模板混淆；本地模板仍由 setting_service 管。
  · 货源链接回收（老版 `/api/sync-source-links`）：成品 payload 里藏着
    `sourceList[].sourceItemId` / `oriItemId`，把它们拼成 1688 offer 链接塞回
    链接池，载体重建才有料可用。写库动作全部委托
    `carrier_service.harvest_from_payloads`（池子结构、去重、合法性都在那里）。

区间校验放在这里（不走 pydantic 的 ge/le），这样报错文案是中文、错误码统一是
`VALIDATION_ERROR`，与 `core/errors.py` 的信封一致。
"""
from __future__ import annotations

import contextlib
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from core import errors
from core.utils import clean_text
from models.product import Product
from services import carrier_service, warehouse_service

# --- 常量 -----------------------------------------------------------------
SITE_DEFAULT = "MY"
SITE_MAX_LENGTH = 8
NAME_MAX_LENGTH = 128
PAGE_NO_DEFAULT = 1
PAGE_SIZE_DEFAULT = 20
PAGE_SIZE_MAX = 20
SOURCE_LINK_LIMIT_DEFAULT = 500
SOURCE_LINK_LIMIT_MAX = 2000
# 与 carrier_service.MAX_FAILURES 一致：到上限即视为不可用
UNUSABLE_FAILED_LIMIT = carrier_service.MAX_FAILURES

# 上游把模板列表放在哪个键下按接口而变，按候选键依次找第一个 list
_TEMPLATE_LIST_KEYS = (
    "detailList",
    "templateList",
    "priceTemplateList",
    "list",
    "records",
    "rows",
    "items",
    "dataList",
)

logger = logging.getLogger("aohuang.sync")


# --- 小工具 ---------------------------------------------------------------
def _as_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _int_field(value: Any, field: str, default: int) -> int:
    number = _as_int(value)
    if number is None:
        if value in (None, ""):
            return default
        raise errors.validation(f"{field} 必须是整数", field=field, value=value)
    return number


# --- 定价模板 -------------------------------------------------------------
def _text_field(value: Any, field: str) -> str:
    """只接受字符串 / null；数字或对象当参数错误处理，不静默吞掉。"""
    if value in (None, ""):
        return ""
    if isinstance(value, bool) or not isinstance(value, str):
        raise errors.validation(f"{field} 必须是字符串", field=field, value=value)
    return clean_text(value)


def normalize_site(value: Any, default: str = SITE_DEFAULT) -> str:
    """站点代码：去空白、转大写；空则用默认站点。"""
    text = _text_field(value, "site").upper()
    if not text:
        text = str(default or "").upper()
    if not text:
        raise errors.validation("站点代码不能为空（例如 MY / SG / TH）", field="site")
    if len(text) > SITE_MAX_LENGTH or not text.isalnum():
        raise errors.validation("站点代码无效（例如 MY / SG / TH）", field="site", value=value)
    return text


def normalize_name(value: Any) -> str:
    text = _text_field(value, "name")
    if len(text) > NAME_MAX_LENGTH:
        raise errors.validation(f"模板名关键字最多 {NAME_MAX_LENGTH} 个字符",
                                field="name", value=len(text))
    return text


def normalize_page(page_no: Any, page_size: Any) -> tuple[int, int]:
    """分页归一：pageNo ≥ 1，pageSize 1–20（上游硬限制，超了直接 400）。"""
    page = _int_field(page_no, "pageNo", PAGE_NO_DEFAULT)
    if page < 1:
        raise errors.validation("pageNo 必须是不小于 1 的整数", field="pageNo", value=page)
    size = _int_field(page_size, "pageSize", PAGE_SIZE_DEFAULT)
    if not 1 <= size <= PAGE_SIZE_MAX:
        raise errors.validation(f"pageSize 必须在 1 到 {PAGE_SIZE_MAX} 之间",
                                field="pageSize", value=size)
    return page, size


def template_rows(response: Mapping[str, Any]) -> tuple[list[dict[str, Any]], int]:
    """取出模板行与总数：行内容原样保留（上游字段名不改），前端直接用。"""
    data: Any = response.get("data") if isinstance(response, Mapping) else None
    rows: list[Any] = []
    if isinstance(data, list):
        rows = data
        data = {}
    elif isinstance(data, Mapping):
        for key in _TEMPLATE_LIST_KEYS:
            value = data.get(key)
            if isinstance(value, list):
                rows = value
                break
    else:
        data = {}
    items = [dict(row) for row in rows if isinstance(row, Mapping)]
    total = _as_int(data.get("total"))
    return items, total if total is not None else len(items)


def fetch_price_templates(client: Any, *, site: Any = None, name: Any = "",
                          page_no: Any = PAGE_NO_DEFAULT,
                          page_size: Any = PAGE_SIZE_DEFAULT) -> dict[str, Any]:
    """调上游拿模板列表；上游异常（MiaoshouError）由调用方转标准信封。"""
    code = normalize_site(site)
    keyword = normalize_name(name)
    page, size = normalize_page(page_no, page_size)
    response = client.price_templates(site=code, name=keyword,
                                      page_no=page, page_size=size)
    items, total = template_rows(response)
    return {
        "items": items,
        "total": total,
        "site": code,
        "pageNo": page,
        "pageSize": size,
    }


# --- 上游模板列表（合并视图的「上游那一半」） -------------------------------
# 缓存 TTL 与 api/reference.py 的参考数据一致：模板一天也变不了几次，
# 但运营在妙手改完会想立刻看到，所以要能被 refresh=true 绕过。
TEMPLATE_CACHE_TTL_ENV = "AOHUANG_TEMPLATE_CACHE_TTL"
TEMPLATE_CACHE_TTL_DEFAULT = 300.0
TEMPLATE_CACHE_MAX_ENTRIES = 64

_TEMPLATE_CACHE: Any = None


def _template_cache() -> Any:
    """进程内模板缓存（只存成功结果，重启即失效）。

    不落库是刻意的：上游模板是快照，混进 `settings['pricing.templates']`
    就分不清「运营自建的」和「上游同步来的」。
    """
    global _TEMPLATE_CACHE
    if _TEMPLATE_CACHE is None:
        from services.cache import get_cache, ttl_from_env

        _TEMPLATE_CACHE = get_cache(
            "price_templates", max_entries=TEMPLATE_CACHE_MAX_ENTRIES,
            default_ttl=ttl_from_env(TEMPLATE_CACHE_TTL_ENV,
                                     TEMPLATE_CACHE_TTL_DEFAULT))
    return _TEMPLATE_CACHE


def upstream_template_rows(db: Session, *, site: Any = None,
                           account_id: Any = None,
                           credentials: Any = None,
                           refresh: bool = False
                           ) -> tuple[list[dict[str, Any]], str]:
    """拉上游模板并归一化，返回 ``(rows, error)``；**失败不抛错**。

    合并视图（`GET /system/templates/available`）断网时不能整个空掉：
    返回空列表 + 一句人话，调用方照常给出本地模板，前端下拉仍然可用。
    这正是老版 `service_price.available`（:44）的做法——上游异常只把
    `templates` 置空，`stored` 照旧返回。

    ``error`` 为空串表示这次真的拿到了上游数据（含「上游返回 0 套」）。
    """
    from services import account_service
    from services import price_template as price_template_service
    from services.cache import ttl_from_env
    from services.miaoshou import MiaoshouError

    try:
        code = normalize_site(site)
    except errors.AppError as exc:
        return [], exc.message

    try:
        account = account_service.resolve_account(db, account_id, credentials)
    except errors.AppError as exc:
        # 没连账号不是故障：本地模板库照常可读，只是上游那一半取不到。
        return [], exc.message

    key = f"acct:{getattr(account, 'id', 0)}:{code}"
    store = _template_cache()
    ttl = ttl_from_env(TEMPLATE_CACHE_TTL_ENV, TEMPLATE_CACHE_TTL_DEFAULT)

    if not refresh:
        cached = store.get(key)
        if isinstance(cached, list):
            return [dict(row) for row in cached if isinstance(row, Mapping)], ""

    client = account_service.account_client(account)
    try:
        raw = client.price_templates(site=code, name="", page_no=PAGE_NO_DEFAULT,
                                     page_size=PAGE_SIZE_MAX)
    except MiaoshouError as exc:
        # 上游抽风时用旧值兜底（过期也认）：给一份过期模板比给空下拉好，
        # 但要在 error 里说清楚，前端才知道这不是最新数据。
        stale = store.lookup(key)
        if stale is not None and isinstance(stale.value, list):
            return ([dict(row) for row in stale.value if isinstance(row, Mapping)],
                    f"上游读取失败，返回 {int(stale.age_seconds)} 秒前的缓存：{exc}")
        return [], str(exc)
    except Exception as exc:  # noqa: BLE001 - 任何上游异常都不该让下拉空掉
        logger.warning("读取上游定价模板失败：%s", type(exc).__name__)
        return [], f"读取上游定价模板失败：{type(exc).__name__}"
    finally:
        with contextlib.suppress(Exception):  # noqa: BLE001 - 关连接失败不该掩盖上游结果
            client.close()

    rows = price_template_service.list_upstream_templates(raw)
    store.set(key, rows, ttl)
    return rows, ""


# --- 货源链接回收 ---------------------------------------------------------
@dataclass(frozen=True)
class SourceScope:
    """一次回收任务的范围（已校验）。"""

    scan_all: bool
    idents: tuple[str, ...]
    limit: int


def plan_source_scan(*, warehouse_ids: Sequence[Any] | None = None,
                     scan_all: bool = False, limit: Any = None) -> SourceScope:
    """校验 warehouseIds / all / limit 三者关系，产出可执行的范围。"""
    idents: list[str] = []
    for item in warehouse_ids or ():
        text = str(item or "").strip()
        if text and text not in idents:
            idents.append(text)
    if scan_all and idents:
        raise errors.validation("warehouseIds 与 all=true 只能二选一", field="all")
    if not scan_all and not idents:
        raise errors.validation(
            "请提供 warehouseIds，或给 all=true 扫描全部未删除商品",
            field="warehouseIds")
    cap = _int_field(limit, "limit", SOURCE_LINK_LIMIT_DEFAULT)
    if not 1 <= cap <= SOURCE_LINK_LIMIT_MAX:
        raise errors.validation(f"limit 必须在 1 到 {SOURCE_LINK_LIMIT_MAX} 之间",
                                field="limit", value=cap)
    if not scan_all and len(idents) > cap:
        raise errors.validation(
            f"warehouseIds 最多一次 {cap} 个（可用 limit 放宽，上限 {SOURCE_LINK_LIMIT_MAX}）",
            field="warehouseIds", value=len(idents))
    return SourceScope(scan_all=scan_all, idents=tuple(idents), limit=cap)


def _resolve_products(db: Session, idents: Sequence[str]) -> list[Product]:
    """按 id 或 `{detailId}:{shopId}` 取商品；软删除的视为不存在。"""
    products: list[Product] = []
    missing: list[str] = []
    for ident in idents:
        try:
            products.append(warehouse_service.resolve_product(db, ident))
        except errors.AppError as exc:
            if exc.code != errors.NOT_FOUND:
                raise
            missing.append(ident)
    if missing:
        raise errors.not_found(
            f"有 {len(missing)} 件商品不在仓库里：{'、'.join(missing[:5])}",
            missing=missing)
    return products


def _scan_products(db: Session, *, limit: int) -> list[Product]:
    """全部未删除商品，最新入库优先。"""
    return (db.query(Product)
            .filter(Product.deleted_at.is_(None))
            .order_by(Product.saved_at.desc(), Product.id.desc())
            .limit(limit)
            .all())


def _pool_rows(db: Session) -> list[dict[str, Any]]:
    pool = carrier_service.list_links(db)
    links = pool.get("links") if isinstance(pool.get("links"), list) else []
    return [dict(item) for item in links if isinstance(item, Mapping)]


def _is_usable(row: Mapping[str, Any]) -> bool:
    try:
        failed = int(row.get("failed") or 0)
    except (TypeError, ValueError):
        failed = 0
    return failed < UNUSABLE_FAILED_LIMIT


def harvest_source_links(db: Session, scope: SourceScope) -> dict[str, Any]:
    """扫描成品 payload，把 1688 货源链接回收进池子（调用方负责 commit）。"""
    if scope.scan_all:
        products = _scan_products(db, limit=scope.limit)
    else:
        products = _resolve_products(db, scope.idents)
    payloads = [product.payload for product in products
                if isinstance(product.payload, dict) and product.payload]
    added = carrier_service.harvest_from_payloads(db, payloads)
    rows = _pool_rows(db)
    usable = [row for row in rows if _is_usable(row)]
    result = {
        "scanned": len(products),
        "added": added,
        "total": len(rows),
        "usable": len(usable),
    }
    logger.info("货源链接回收 scanned=%d payloads=%d added=%d total=%d usable=%d",
                result["scanned"], len(payloads), added, result["total"], result["usable"])
    return result


def sync_price_templates(db: Session, *, site: Any = None, account_id: Any = None,
                         template_name: Any = "", save: bool = False,
                         client: Any = None) -> dict[str, Any]:
    """从妙手同步定价模板：拉上游 + 站点默认 → 选一套 → 三级合并 → 可选落库。

    老版 `service_price.sync`（:70）的等价实现。`save=False` 是刻意的默认值：
    现有 `POST /sync/price-templates` 的调用方依赖「只读、不落库」，
    同步落库必须由调用方显式要求。

    `save=True` 时**落库并设为当前选中**（照老版「保存即设为当前」语义）。
    只存不选会留下一个尴尬状态：模板库里多了一套，报价却还在用旧的。

    合并细节全部委托 `price_template.merge_from_upstream`，
    这里只负责取数据与落库，避免两处各写一套合并规则。
    """
    from services import price_template as price_template_service
    from services import setting_service

    code = normalize_site(site)
    account = account_id
    if client is None:
        from services import account_service

        account_obj = account_service.resolve_account(db, account_id, None)
        account = account_obj.id
        client = account_service.account_client(account_obj)
        owns_client = True
    else:
        owns_client = False

    try:
        # 上游模板：多套时 pick 会抛错并列名字，让运营去选
        raw = client.price_templates(site=code, name="", page_no=PAGE_NO_DEFAULT,
                                     page_size=PAGE_SIZE_MAX)
        templates = price_template_service.list_upstream_templates(raw)
        # 站点默认费率（老版 get_site_default_setting，空 body）
        try:
            defaults = client.price_site_defaults()
        except Exception as exc:  # noqa: BLE001 - 拿不到默认费率不该让同步整体失败
            logger.warning("站点默认费率获取失败：%s", type(exc).__name__)
            defaults = {}

        stored = None
        if template_name:
            stored = setting_service.price_templates(db).get(str(template_name))
        if stored is None:
            selected = setting_service.selected_templates(db)
            current = (selected.get("accounts") or {}).get(str(account or ""))
            if current:
                stored = setting_service.price_templates(db).get(str(current))
        merged = price_template_service.merge_from_upstream(
            raw, stored, defaults, template_name=template_name)
    finally:
        if owns_client:
            with contextlib.suppress(Exception):  # noqa: BLE001 - 关连接失败不该影响合并结果
                client.close()

    result: dict[str, Any] = {
        "synced": True,
        "upstreamCount": len(templates),
        "templates": templates,
        "merged": merged,
        "selected": merged.get("name"),
        "saved": False,
    }
    # 缺哪些费率：合并后仍为空就是真的没数据，前端据此提示「报价为估算」
    result["missingRateFields"] = [
        field for field in (
            "vatChargePercent", "paymentChargePercent", "platformCommissionPercent",
            "bcpChargePercent", "withdrawChargePercent", "sfpChargePercent",
            "affiliateChargePercent", "lastMileFreightServiceTaxPercent",
            *price_template_service.EXTRA_CHARGE_FIELDS,
        )
        if merged.get(field) in (None, "")
    ]
    if save:
        setting_service.save_template(db, merged)
        setting_service.select_template(db, merged["name"], str(account or ""))
        # 四级兜底的第三级：把「妙手侧当前是哪一套」也记下来。
        # 不记的话，用户清掉本地选择后第三级永远取不到值，等于只有三级兜底。
        setting_service.set_miaoshou_selected(db, merged["name"], str(account or ""))
        db.commit()
        result["saved"] = True
    return result
