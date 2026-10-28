"""settings 表的读写封装（点分键 + JSON 值）。"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from core.utils import now_ts
from models.setting import (
    IMAGE_LIMITS_KEY,
    PRICING_SELECTED_KEY,
    PRICING_TEMPLATES_KEY,
    PUBLISH_ENABLED_KEY,
    READ_ONLY_KEY,
    UPDATE_CHANNEL_KEY,
    WORKFLOW_DEFAULTS_KEY,
    Setting,
)


def get(db: Session, key: str, default: Any = None) -> Any:
    row = db.query(Setting).filter(Setting.key == key).one_or_none()
    if row is None:
        return default
    return row.value if row.value is not None else default


def put(db: Session, key: str, value: Any) -> Any:
    row = db.query(Setting).filter(Setting.key == key).one_or_none()
    if row is None:
        row = Setting(key=key, value=value, updated_at=now_ts())
        db.add(row)
    else:
        row.value = value
        row.updated_at = now_ts()
    db.flush()
    return row.value


# --- 语义化读写 ------------------------------------------------------------
def is_read_only(db: Session) -> bool:
    return bool(get(db, READ_ONLY_KEY, False))


def set_read_only(db: Session, value: bool) -> bool:
    return bool(put(db, READ_ONLY_KEY, bool(value)))


def publish_enabled(db: Session) -> bool:
    return bool(get(db, PUBLISH_ENABLED_KEY, False))


def set_publish_enabled(db: Session, value: bool) -> bool:
    return bool(put(db, PUBLISH_ENABLED_KEY, bool(value)))


def image_limits(db: Session) -> dict[str, Any]:
    value = get(db, IMAGE_LIMITS_KEY) or {}
    return value if isinstance(value, dict) else {}


# 额度上下限：给死值保护，避免手滑把每天改成 100 万张
IMAGE_LIMIT_BOUNDS = {
    "minute": (1, 600),
    "hour": (1, 6000),
    "day": (1, 100000),
    "limitMilliyuan": (0, 100_000_000),
}


def set_image_limits(db: Session, payload: Any) -> dict[str, Any]:
    """改图片限流额度（老版可用 TK_IMAGE_RATE_* 环境变量覆盖，新版走这里）。

    只接受已知键、只接受整数、超界直接拒绝：限流值写歪了，要么白扣费
    要么整批卡死，比报错难查得多。
    """
    from core import errors

    if not isinstance(payload, dict):
        raise errors.validation("额度必须是对象", field="limits")
    current = dict(image_limits(db))
    budgets = dict(current.get("budgets") or {})
    updated = False
    for key, (low, high) in IMAGE_LIMIT_BOUNDS.items():
        if key not in payload or payload.get(key) in (None, ""):
            continue
        try:
            value = int(payload[key])
        except (TypeError, ValueError):
            raise errors.validation(f"{key} 必须是整数", field=key) from None
        if not low <= value <= high:
            raise errors.validation(f"{key} 必须在 {low} 到 {high} 之间",
                                   field=key, value=value)
        if key in ("minute", "hour", "day"):
            budgets[key] = value
        else:
            current[key] = value
        updated = True
    if budgets:
        current["budgets"] = budgets
    if not updated:
        raise errors.validation("没有可更新的额度项", field="limits")
    put(db, IMAGE_LIMITS_KEY, current)
    return current


def workflow_defaults(db: Session) -> dict[str, Any]:
    value = get(db, WORKFLOW_DEFAULTS_KEY) or {}
    return value if isinstance(value, dict) else {}


def save_workflow_defaults(db: Session, cleaned: dict[str, Any]) -> dict[str, Any]:
    """合并写入工作流默认值（只覆盖传入的键，其余保持原值）。"""
    current = workflow_defaults(db)
    merged = {**current, **cleaned}
    put(db, WORKFLOW_DEFAULTS_KEY, merged)
    return merged


def price_templates(db: Session) -> dict[str, Any]:
    value = get(db, PRICING_TEMPLATES_KEY) or {}
    return value if isinstance(value, dict) else {}


def selected_templates(db: Session) -> dict[str, str]:
    value = get(db, PRICING_SELECTED_KEY) or {}
    accounts = value.get("accounts") if isinstance(value, dict) else None
    return {str(k): str(v) for k, v in (accounts or {}).items()}


def select_template(db: Session, name: str, account: str = "") -> str:
    current = get(db, PRICING_SELECTED_KEY) or {}
    if not isinstance(current, dict):
        current = {}
    accounts = current.get("accounts") if isinstance(current.get("accounts"), dict) else {}
    accounts[str(account or "")] = str(name or "")
    put(db, PRICING_SELECTED_KEY, {**current, "accounts": accounts})
    return str(name or "")


def save_template(db: Session, template: dict[str, Any]) -> dict[str, Any]:
    templates = price_templates(db)
    name = str(template.get("name") or "").strip()
    if not name:
        raise ValueError("模板名称不能为空")
    templates[name] = template
    put(db, PRICING_TEMPLATES_KEY, templates)
    return template


def delete_template(db: Session, name: str) -> bool:
    templates = price_templates(db)
    if name not in templates:
        return False
    templates.pop(name, None)
    put(db, PRICING_TEMPLATES_KEY, templates)
    return True


def update_channel(db: Session) -> dict[str, Any]:
    value = get(db, UPDATE_CHANNEL_KEY) or {}
    return value if isinstance(value, dict) else {}


# --- 定价模板：当前选中（四级兜底）与合并视图 -------------------------------
# 妙手侧的记录。老版由上游 `get_site_default_setting` 带回「当前模板名」，
# 新版不联网也要能读到同一件事，所以落成本地设置项（写它的地方是同步接口）。
MIAOSHOU_SELECTED_KEY = "pricing.miaoshouSelected"


def _miaoshou_selected(db: Session, account_id: str = "") -> str:
    """妙手侧记录的模板名；历史形态有三种，都认。

    纯字符串 / `{"name": ...}` / `{"accounts": {accountId: name}}`。
    认三种是因为这个键在不同版本里被写过不同形状，认不出就等于没有兜底 3，
    用户又得每次重选模板。
    """
    value = get(db, MIAOSHOU_SELECTED_KEY)
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    accounts = value.get("accounts")
    if isinstance(accounts, dict):
        wanted = str(account_id or "").strip()
        if wanted:
            own = accounts.get(wanted)
            if isinstance(own, str) and own.strip():
                return own.strip()
        shared = accounts.get("")
        if isinstance(shared, str) and shared.strip():
            return shared.strip()
    for key in ("name", "templateName", "template"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def set_miaoshou_selected(db: Session, name: str, account_id: str = "") -> str:
    """记住妙手侧当前用的模板名（四级兜底的第三级）。

    老版这一级来自上游 `get_site_default_setting` 的返回，但实测那份响应里
    **没有**模板名字段（只有费率表），所以新版在同步模板时把「上游说当前是
    哪一套」记到本地。不记的话第三级永远是空的，等于只有三级兜底。

    带账号时只写该账号那一条，不动共享的 `name`：否则「A 账号同步了一次」
    就会把 B 账号的兜底也改掉，与老版 `select_template` 的语义一致
    （price_template.py:347-352，per-account 条目单独存，共享值保持不变）。
    """
    name = str(name or "").strip()
    wanted = str(account_id or "").strip()
    current = get(db, MIAOSHOU_SELECTED_KEY)
    state = dict(current) if isinstance(current, dict) else {}
    accounts = state.get("accounts")
    accounts = dict(accounts) if isinstance(accounts, dict) else {}
    accounts[wanted] = name
    shared = name if not wanted else str(state.get("name") or "").strip()
    put(db, MIAOSHOU_SELECTED_KEY, {"name": shared, "accounts": accounts})
    return name


def selected_template_name(db: Session, account_id: str | None = None) -> str:
    """按四级兜底取当前选中模板名，取不到返回 ""。

    1. 该账号显式选中的（pricing.selected.accounts[accountId]）
    2. 全局选中的（pricing.selected.accounts[""]）
    3. 妙手侧记录的（settings["pricing.miaoshouSelected"]）
    4. 模板库里唯一那一套（只有 1 套时自动用它）

    为什么必须四级：少一级，用户每次发布、每次建定时任务都得重选模板，
    选漏就按原价上架 —— 这是钱的 bug，不是体验问题。老版
    `price_template.selected_template_name` 就是这套顺序。

    前三级的名字若在库里已经不存在（模板被删/改名），跳过继续往下找：
    一个指向已删除模板的选择等于没选。但**库为空时原样返回** ——
    刚连上妙手、还没同步模板时不能假装「没有选择」。
    """
    wanted = str(account_id or "").strip()
    stored = price_templates(db)
    known = {str(name) for name in stored}
    selected = selected_templates(db)

    candidates: list[str] = []
    if wanted:
        candidates.append(selected.get(wanted, ""))
    candidates.append(selected.get("", ""))
    candidates.append(_miaoshou_selected(db, wanted))

    for candidate in candidates:
        name = str(candidate or "").strip()
        if not name:
            continue
        if not known or name in known:
            return name

    if len(known) == 1:
        return next(iter(known))
    return ""


def template_available(db: Session, upstream: list | None = None, *,
                       account_id: str = "") -> list[dict[str, Any]]:
    """本地自建 + 上游模板合成一份列表，每行带 selected / source。

    source: "local" | "upstream" | "both"
    同名时合并成一行（source="both"），本地字段优先 —— 本地是运营改过的。

    为什么合并要在后端做：前端拿下拉得先调 `/system/templates` 再调
    `/sync/price-templates`，自己拼漏一次就出现「上游有 3 套、下拉只显示 1 套」，
    而运营只会以为模板丢了。合并只该有一处实现。

    `upstream` 传 None 或空数组都正常返回本地部分：断网时下拉不该整个空掉。
    """

    rows: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    def _row(name: str, data: Mapping[str, Any], source: str) -> dict[str, Any]:
        return {
            "name": name,
            "site": str(data.get("site") or "").strip().upper(),
            "profitType": data.get("profitType"),
            "profitPercent": data.get("profitPercent"),
            "currency": data.get("currency"),
            "weightSource": data.get("weightSource") or "spu",
            "source": source,
            "selected": False,
        }

    for raw_name, raw in price_templates(db).items():
        name = str(raw_name or "").strip()
        if not name:
            continue
        rows[name] = _row(name, raw if isinstance(raw, Mapping) else {}, "local")
        order.append(name)

    upstream_rows = upstream if isinstance(upstream, (list, tuple)) else []
    for raw in upstream_rows:
        if not isinstance(raw, Mapping):
            continue
        name = str(raw.get("name") or "").strip()
        if not name:
            continue
        existing = rows.get(name)
        if existing is None:
            rows[name] = _row(name, raw, "upstream")
            order.append(name)
            continue
        # 同名：本地是运营改过的，本地字段优先；只有本地缺的字段才用上游补，
        # 绝不反过来 —— 把本地利润率换成上游的，报价会悄悄变。
        existing["source"] = "both"
        for field in ("site", "profitType", "profitPercent", "currency"):
            if existing.get(field) in (None, ""):
                existing[field] = raw.get(field)

    chosen = selected_template_name(db, account_id)
    for row in rows.values():
        row["selected"] = bool(chosen) and row["name"] == chosen
    return [rows[name] for name in order]
