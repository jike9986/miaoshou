"""系统路由：健康检查、版本、更新。"""
from __future__ import annotations

import collections
from typing import Any

from fastapi import APIRouter, Request

from api import reference
from api.deps import CredentialsHeader, DbSession, OperatorDep, require_permission, require_writable
from api.helpers import parse_bool
from core import errors
from core.config import get_settings
from core.constants import (
    APP_VERSION,
    LANGUAGE_LABELS,
    SUPPORTED_LANGUAGES,
    UPDATE_HOST_WHITELIST,
)
from schemas.requests import PricePreviewRequest
from services import (
    account_service,
    audit_service,
    batch_service,
    operator_service,
    setting_service,
    system_service,
    warehouse_service,
)

# 路由函数叫 update_check，模块必须换个别名，否则函数一定义就把模块名盖掉了
from services import update_check as update_service
from services.account_service import account_client, map_upstream_error
from services.miaoshou import MiaoshouError

router = APIRouter(prefix="/system", tags=["系统"])
# 免鉴权路由：Electron 探活必须在拿到 token 之前就能调
health_router = APIRouter(prefix="/system", tags=["系统"])


@health_router.get("/health", summary="健康检查（无鉴权，供 Electron 探活）")
def health(db: DbSession) -> dict[str, Any]:
    state = system_service.db_state(db)
    return system_service.health(db_ok=state == "ok", db_state=state)


@router.get("/version", summary="本地版本 + 更新清单比对")
def version(db: DbSession, refresh: str | None = None) -> dict[str, Any]:
    refresh_flag = parse_bool(refresh, "refresh", True)
    channel = setting_service.update_channel(db)
    manifest = str(channel.get("manifestUrl") or "") if isinstance(channel, dict) else ""
    return system_service.version_payload(refresh=refresh_flag, manifest_url=manifest)


@router.post("/update-check", summary="检查更新（等价 GET /system/version）")
def update_check(db: DbSession, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    refresh_flag = True
    if isinstance(payload, dict) and "refresh" in payload:
        refresh_flag = parse_bool(payload.get("refresh"), "refresh", True)
    channel = setting_service.update_channel(db)
    manifest = str(channel.get("manifestUrl") or "") if isinstance(channel, dict) else ""
    return system_service.version_payload(refresh=refresh_flag, manifest_url=manifest)


@router.get("/update", summary="查询是否有新版本（只查询，不下载）")
def update_status(db: DbSession) -> dict[str, Any]:
    """GET 只回答「有没有新版本、装不装得了」，绝不落盘。

    查询动作本身是只读的，所以不套 require_writable；但要把 readOnly / 是否
    真能装一并回给界面，否则只读模式下用户点了「更新」才被 403 拦住。
    """
    channel = setting_service.update_channel(db)
    manifest = str(channel.get("manifestUrl") or "") if isinstance(channel, dict) else ""
    payload = system_service.version_payload(refresh=True, manifest_url=manifest)
    last = update_service.last_downloaded()
    return {
        "ok": bool(payload.get("ok")),
        "current": payload.get("current"),
        "latest": payload.get("latest") or "",
        "updateAvailable": bool(payload.get("updateAvailable")),
        "url": payload.get("url"),
        "sha256": payload.get("sha256"),
        "sizeBytes": payload.get("sizeBytes"),
        "notes": payload.get("notes") or "",
        "error": payload.get("error"),
        "downloaded": False,
        "canInstall": update_service.can_install(),
        "readOnly": setting_service.is_read_only(db),
        # 顺带告诉界面上次下的包还在不在，省得重复下一遍几十兆
        "pendingFile": last,
        "checkedAt": payload.get("checkedAt"),
    }


@router.post("/update", summary="下载更新包（校验后落盘，可按需安装）",
             dependencies=[require_permission("admin")])
def update(payload: dict[str, Any] | None = None, db: DbSession = None) -> dict[str, Any]:
    """POST 才真正下载；`install: true` 时在 Windows 上直接拉起安装包。

    老版这里被写成「只算路径不下载」，`downloaded` 恒为 False，更新按钮是死的。
    现在真的下：https + 公网校验 → .part 原子落盘 → sha256 校验 → 可选安装。

    只读模式与共享模式一律不下载：共享服务器上下载几十兆的安装包没有意义，
    真正该装的是操作者本机。
    """
    require_writable(db)
    body = payload or {}
    settings = get_settings()
    channel = setting_service.update_channel(db)
    manifest = str(channel.get("manifestUrl") or "") if isinstance(channel, dict) else ""

    # 地址可以不传：不传就按清单的 url + sha256 来，界面不必自己拼地址
    url = str(body.get("url") or "").strip()
    sha256 = str(body.get("sha256") or "").strip().lower()
    version = str(body.get("version") or "").strip()
    if not url:
        info = system_service.version_payload(refresh=True, manifest_url=manifest)
        url = str(info.get("url") or "").strip()
        sha256 = sha256 or str(info.get("sha256") or "").strip().lower()
        version = version or str(info.get("latest") or "").strip()
        if not url:
            raise errors.validation("没有可用的更新包地址，请先检查更新", field="url")

    # 请求体带来的地址必须落在白名单域；清单里取来的地址由清单域背书，只查 https/内网
    whitelist = UPDATE_HOST_WHITELIST if body.get("url") else None
    if settings.shared_mode:
        return {
            "ok": True,
            "accepted": False,
            "reason": "shared-mode",
            "url": url,
            "sha256": sha256,
            "downloaded": False,
            "canInstall": False,
        }

    result = update_service.download_and_install(url, sha256, host_whitelist=whitelist)
    response: dict[str, Any] = {
        "ok": True,
        "accepted": True,
        "version": version,
        "url": url,
        "file": result["path"],
        "path": result["path"],
        "sizeBytes": result["size"],
        "sha256": result["sha256"],
        "sha256Verified": result["sha256Verified"],
        "downloaded": True,
        "fileExists": True,
        "canInstall": result["canInstall"],
        "willRestart": bool(body.get("restart") is not False),
    }
    if parse_bool(body.get("install"), "install", False):
        # 安装失败不该把「已经下好了」这件事一起丢掉：把结果挂在错误详情里
        try:
            response["installed"] = update_service.install(
                result["path"],
                silent=parse_bool(body.get("silent"), "silent", True))
        except errors.AppError as exc:
            exc.details = {**exc.details, "download": response}
            raise
    return response


@router.get("/bootstrap", summary="首屏聚合：一次拿齐启动所需的状态")
def system_bootstrap(db: DbSession, credentials: CredentialsHeader = None) -> dict[str, Any]:
    """等价老版 `/api/bootstrap`：一次请求拿齐首屏要的所有状态。

    老版是单进程内存态，一个 bootstrap 就够；新版拆成了十几个接口，
    前端首屏要并发发七八个请求才能拼出完整界面。冷启动时这些请求排队，
    界面会先闪一下空态再填上。这里做一次聚合，前端一次拿齐。

    刻意**不**回源妙手：bootstrap 要快，上游任何一个慢调用都会把首屏拖住。
    账号 / 店铺 / 模板都取本地缓存，需要新鲜数据的地方各自有 refresh 参数。

    任何一段失败都不让整体失败 —— 首屏宁可少一块，也不能整页白屏。
    """
    settings = get_settings()
    view: dict[str, Any] = {
        "application": "aohuang-shangpin",
        "version": APP_VERSION,
        "readOnly": setting_service.is_read_only(db),
        "publishEnabled": setting_service.publish_enabled(db),
        "sharedMode": bool(settings.shared_mode),
        "dataDir": str(settings.data_dir),
        "imageKeyConfigured": bool(settings.image_api_key()),
        "languages": list(SUPPORTED_LANGUAGES),
        "languageOptions": [
            {"code": code, "label": LANGUAGE_LABELS.get(code, code)}
            for code in SUPPORTED_LANGUAGES
        ],
        "workflowDefaults": setting_service.workflow_defaults(db),
        "imageLimits": setting_service.image_limits(db),
    }
    # 下面每段单独兜：数据库里某张表读失败（例如老库缺列）不该让首屏全挂。
    # 注意 accounts 必须用 account_summaries —— list_accounts 返回的是 ORM 对象，
    # 直接塞进响应会 PydanticSerializationError 整页 500。
    for key, loader in (
        ("accounts", lambda: account_service.account_summaries(db)),
        ("templates", lambda: _bootstrap_templates(db)),
        ("stats", lambda: warehouse_service.stats(db)),
        ("batches", lambda: batch_service.list_batches(db, limit=5)),
    ):
        try:
            view[key] = loader()
        except Exception as exc:  # noqa: BLE001 - 首屏聚合不能因为一段失败整页空
            view[key] = None
            view.setdefault("degraded", []).append({"section": key,
                                                    "error": type(exc).__name__})
    return view


def _bootstrap_templates(db: DbSession) -> dict[str, Any]:
    """模板库 + 当前选中名，单独成函数便于上面那段 try/except 包住。"""
    stored = setting_service.price_templates(db)
    items = []
    for name in stored:
        items.append({"name": name})
    # selectedName 走四级兜底：首屏拿到的就是「这次会用哪套模板」，
    # 前端不必自己去猜 accounts[""] / 妙手侧记录 / 唯一那一套。
    return {"items": items, "total": len(items),
            "selected": setting_service.selected_templates(db),
            "selectedName": setting_service.selected_template_name(db)}


def _validate_workflow_defaults(raw: dict[str, Any]) -> dict[str, Any]:
    """工作流默认值的白名单校验（POST /system/settings 的 workflowDefaults 分支）。

    前端处理设置面板改重量/包裹尺寸/尺码图后，经这里落到 workflow.defaults，
    入仓时 save_from_collect_box 的 _workflow_setting 会读到同一份值——
    这就是「界面上调了参数，处理时真的生效」的那条链路。
    """
    cleaned: dict[str, Any] = {}
    if "weightIncrement" in raw:
        try:
            weight = float(raw["weightIncrement"])
        except (TypeError, ValueError):
            raise errors.validation("重量增量须为数字", field="weightIncrement") from None
        if not 0 <= weight <= 5:
            raise errors.validation("重量增量须在 0–5 kg 之间", field="weightIncrement")
        cleaned["weightIncrement"] = weight
    if "packageDimensions" in raw:
        # 复用服务层的同一套校验与键名规则（packageLength/Width/Height，0–300）
        cleaned["packageDimensions"] = warehouse_service.normalize_package_dimensions(
            raw["packageDimensions"])
    if "sizeChartUrl" in raw:
        cleaned["sizeChartUrl"] = warehouse_service.normalize_size_chart_url(
            raw["sizeChartUrl"])
    return cleaned


@router.get("/settings", summary="只读模式与发布开关")
def system_settings(db: DbSession) -> dict[str, Any]:
    settings = get_settings()
    return {
        "readOnly": setting_service.is_read_only(db),
        "publishEnabled": setting_service.publish_enabled(db),
        "dataDir": str(settings.data_dir),
        "dbPath": str(settings.db_path),
        "imageKeyConfigured": bool(settings.image_api_key()),
        "sharedMode": bool(settings.shared_mode),
        "workflowDefaults": setting_service.workflow_defaults(db),
        "imageLimits": setting_service.image_limits(db),
    }


@router.post("/settings", summary="切换只读模式 / 发布开关",
             dependencies=[require_permission("admin")])
def patch_system_settings(payload: dict[str, Any], db: DbSession,
                          operator: OperatorDep) -> dict[str, Any]:
    # 改系统设置是管理员专属：只读模式能让所有人立刻写不进去，
    # 发布开关能控制全员的发布能力——普通操作员不该有这个能力。
    operator_service.require_role(operator, "settings")
    # 关闭只读模式本身属于「恢复可写」，不能反过来被只读守卫拦住
    turning_off_readonly = ("securityReadOnly" in payload
                            and parse_bool(payload.get("securityReadOnly"),
                                           "securityReadOnly") is False)
    if not turning_off_readonly:
        require_writable(db)
    changed: dict[str, Any] = {}
    if "securityReadOnly" in payload:
        value = parse_bool(payload.get("securityReadOnly"), "securityReadOnly")
        setting_service.set_read_only(db, value)
        changed["securityReadOnly"] = bool(value)
    if "publishEnabled" in payload:
        value = parse_bool(payload.get("publishEnabled"), "publishEnabled")
        setting_service.set_publish_enabled(db, value)
        changed["publishEnabled"] = bool(value)
    if "workflowDefaults" in payload:
        raw = payload.get("workflowDefaults")
        if not isinstance(raw, dict):
            raise errors.validation("workflowDefaults 必须是对象", field="workflowDefaults")
        # 只接受白名单键，做服务端校验：非法值在这里就 422，
        # 不能等到入仓时才让 normalize_package_dimensions 抛「请填写包裹长宽高」。
        cleaned = _validate_workflow_defaults(raw)
        setting_service.save_workflow_defaults(db, cleaned)
        changed["workflowDefaults"] = sorted(cleaned)
    db.commit()
    if changed:
        # 共享设置一改全员受影响（只读模式能让所有人立刻写不进去），
        # 这类改动最该答得出「谁在什么时候关的」。
        audit_service.record(db, operator=operator, action="settings.change",
                             target_kind="setting",
                             target_id=",".join(sorted(changed)),
                             detail={"name": "共享设置", **changed})
        db.commit()
    return system_settings(db)


@router.get("/price-defaults", summary="站点默认定价（只读，回源妙手）")
def price_defaults(db: DbSession,
                   site: str | None = None,
                   accountId: str | None = None,
                   credentials: CredentialsHeader = None,
                   refresh: str | None = None) -> dict[str, Any]:
    """老版 `price-defaults` 子命令（cli.py:154-158）的等价物。

    为什么挂在 /system：它回答的是「这个站点的默认佣金/运费/费率是多少」，
    属于全局定价基线，跟 /system/templates（本地模板库）是一组东西。

    `site` 只用于回显与挑出该站点那一段：上游 `get_site_default_setting`
    不吃 site 参数（老版传的是空 body），它一次返回**所有**站点的费率表，
    本地按 site 切出来给界面用，顺带把整表也带上（切换站点不必再请求）。
    """
    account = reference.resolve_account_for(db, accountId, credentials)
    site_code = (site or (getattr(account, "site", "") or "") or "MY").upper()
    refresh_flag = parse_bool(refresh, "refresh", False)
    client = account_client(account)
    try:
        payload, cached_flag, degraded, stale = reference.cached(
            account, "price-defaults", lambda: client.price_site_defaults(),
            refresh=refresh_flag)
    except reference.UpstreamUnsupported as exc:
        return reference.unsupported_envelope(account, exc.reason, site=site_code,
                                              defaults=None)
    except MiaoshouError as exc:
        raise map_upstream_error(exc) from None
    finally:
        client.close()
    return {
        **reference.envelope(account, cached_flag=cached_flag, degraded=degraded,
                             stale_seconds=stale),
        "site": site_code,
        **reference.price_defaults_view(payload, site_code),
    }


@router.get("/templates", summary="定价模板列表")
def templates(db: DbSession, accountId: str | None = None) -> dict[str, Any]:
    """本地模板库 + 合并视图。

    `templates` / `total` / `selected` 是原有字段，一个都不动（前端已在用）。
    新增的 `available` 是「本地 + 上游」的合并视图，`selectedName` 是四级兜底
    的结果 —— 老版 `service_price.available`（:44）返回的 `current` 就是它。
    没有上游调用：这个接口必须离线可用，上游那一半在 /templates/available。
    """
    from services import price_template

    stored = setting_service.price_templates(db)
    items = []
    for name, raw in stored.items():
        try:
            normalized = price_template.normalize_template(raw)
        except price_template.PriceTemplateError as exc:
            items.append({"name": name, "invalid": True, "error": str(exc)})
            continue
        items.append({"name": name, "template": normalized})
    account = str(accountId or "").strip()
    return {"templates": items, "total": len(items),
            "selected": setting_service.selected_templates(db),
            "available": setting_service.template_available(db, None,
                                                            account_id=account),
            "selectedName": setting_service.selected_template_name(db, account)}


@router.get("/templates/available", summary="定价模板合并视图（本地 + 上游）")
def templates_available(db: DbSession,
                        site: str | None = None,
                        accountId: str | None = None,
                        refresh: str | None = None,
                        credentials: CredentialsHeader = None) -> dict[str, Any]:
    """把上游模板与本地自建模板合成一份下拉用的列表。

    为什么合并放在后端：前端要自己调 `/system/templates` + `/sync/price-templates`
    再拼，拼漏一次就出现「上游有 3 套、下拉只显示 1 套」。合并只该有一处实现。

    上游失败时**不报错**，只返回本地 + `upstreamError`：断网时前端下拉不该整个
    空掉（老版 `service_price.available` 就是这么处理 MiaoshouApiError 的）。
    `refresh=true` 跳过缓存，等价老版的 `--refresh`。
    """
    from services import sync_service

    account = str(accountId or "").strip()
    refresh_flag = parse_bool(refresh, "refresh", False)
    upstream, upstream_error = sync_service.upstream_template_rows(
        db, site=site, account_id=account or None, credentials=credentials,
        refresh=refresh_flag)
    available = setting_service.template_available(db, upstream, account_id=account)
    result: dict[str, Any] = {
        "available": available,
        "selectedName": setting_service.selected_template_name(db, account),
        "total": len(available),
        "site": (site or "").strip().upper(),
        "accountId": account,
        "upstreamCount": len(upstream),
    }
    if upstream_error:
        result["upstreamError"] = upstream_error
    return result


@router.post("/price-preview", summary="未入仓商品的定价试算（手填采购价 + 重量）")
def price_preview(payload: PricePreviewRequest, db: DbSession) -> dict[str, Any]:
    """没入仓、没有 skuMap 的商品也能直接算展示价。

    `apply_template` 要求完整 item（含 skuMap），未入仓商品套不上；
    这里走 `price_template.preview_price`（老版 `service_price.preview` :130 的
    等价物），只吃采购价 + 重量 + 模板。

    `templateName` 为空时走 `selected_template_name` 的四级兜底：
    前端不传模板名也该算出一个价，而不是让用户先回去选模板。
    """
    from decimal import Decimal, InvalidOperation

    from services import price_template

    def _number(value: Any, field: str, label: str) -> Decimal:
        """采购价/重量/汇率统一在这里转，错误文案是中文且指名字段。"""
        if value in (None, ""):
            raise errors.validation(f"请填写{label}", field=field)
        if isinstance(value, bool):
            raise errors.validation(f"{label}必须是数字", field=field, value=value)
        try:
            number = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            raise errors.validation(f"{label}不是有效数字", field=field,
                                    value=value) from None
        if not number.is_finite():
            raise errors.validation(f"{label}不是有效数字", field=field, value=value)
        return number

    purchase = _number(payload.purchaseCny, "purchaseCny", "产品采购价")
    if purchase <= 0:
        raise errors.validation("产品采购价必须大于 0", field="purchaseCny",
                                value=str(purchase))
    weight = _number(payload.weightKg, "weightKg", "产品包裹重量")
    if weight <= 0:
        raise errors.validation("产品包裹重量必须大于 0", field="weightKg",
                                value=str(weight))
    rate: Any = None
    if payload.rate not in (None, ""):
        rate_d = _number(payload.rate, "rate", "汇率")
        if rate_d <= 0:
            raise errors.validation("汇率必须大于 0", field="rate", value=str(rate_d))
        rate = rate_d

    account = str(payload.accountId or "").strip()
    name = str(payload.templateName or "").strip()
    stored = setting_service.price_templates(db)
    pt_defaults = price_template.DEFAULT_TEMPLATE
    if not name:
        # 四级兜底：前端不传模板名也得算得出价，否则「选漏了按原价上架」。
        name = setting_service.selected_template_name(db, account)
    if name and name not in stored:
        raise errors.not_found(f"定价模板「{name}」不存在", template=name)
    if not name:
        # 库是空的：用内置默认模板算，并在返回里标明用的是默认值
        raw_template: dict[str, Any] = dict(pt_defaults)
    else:
        raw_template = stored.get(name) if isinstance(stored.get(name), dict) else {}
    try:
        # 先归一化一次只为校验（坏模板该给 422 而不是算出个奇怪的价），
        # 但交给 preview_price 的是**原始**模板：归一化会把 None 费率折成 0，
        # 「这项没填」的估算信号就丢了。
        template = price_template.normalize_template(raw_template)
    except price_template.PriceTemplateError as exc:
        raise errors.AppError(errors.TEMPLATE_INVALID, f"定价模板不可用：{exc}",
                              status=422, details={"template": name}) from None
    try:
        result = price_template.preview_price(purchase, weight, raw_template, rate=rate)
    except price_template.PriceTemplateError as exc:
        raise errors.unprocessable(str(exc)) from None
    return {
        "price": result["price"],
        "currency": result["currency"],
        "lines": result["lines"],
        "linesCny": result["linesCny"],
        "rate": result["rate"],
        "rateSource": result["rateSource"],
        "templateName": result.get("templateName") or template.get("name"),
        "missingRateFields": result.get("missingRateFields", []),
        "isEstimate": bool(result.get("isEstimate")),
        "site": (payload.site or template.get("site") or "").strip().upper(),
        "purchasePriceCny": result.get("purchasePriceCny"),
        "packageWeightKg": result.get("packageWeightKg"),
    }



@router.post("/templates/select", summary="记住某个账号当前用的是哪套定价模板",
             dependencies=[require_permission("process")])
def select_template(payload: dict[str, Any], db: DbSession,
                    operator: OperatorDep) -> dict[str, Any]:
    """按账号记当前模板（老版 /api/price-select 的等价物）。

    老版把这个存在妙手侧，切换账号时自动带出该账号的模板。
    新版本地也能存（setting_service.select_template），但一直没有接口调它，
    结果 `selected` 永远是空的——用户每次发布、每次建定时任务都得重选一遍模板，
    选漏了就是按原价上架。
    """
    require_writable(db)
    name = str(payload.get("name") or "").strip()
    if not name:
        raise errors.validation("请选择模板", field="name")
    if name not in setting_service.price_templates(db):
        raise errors.not_found(f"模板「{name}」不存在", name=name)
    account = str(payload.get("account") or payload.get("accountId") or "").strip()
    setting_service.select_template(db, name, account=account)
    db.commit()
    # 模板决定「按什么价上架」，选错了是直接亏钱的那种错，必须留痕
    audit_service.record(db, operator=operator, action="template.select",
                         target_kind="template", target_id=name,
                         detail={"name": name, "account": account})
    db.commit()
    return {"account": account, "name": name,
            "selected": setting_service.selected_templates(db)}


@router.post("/templates", summary="保存定价模板（校验后入库）",
             dependencies=[require_permission("process")])
def save_template(payload: dict[str, Any], db: DbSession,
                  operator: OperatorDep) -> dict[str, Any]:
    require_writable(db)
    from services import price_template

    raw = payload.get("template") if isinstance(payload.get("template"), dict) else payload
    with audit_service.guard(db, operator=operator, action="template.save",
                             target_kind="template",
                             target_id=str(raw.get("name") or "")
                             if isinstance(raw, dict) else ""):
        try:
            normalized = price_template.normalize_template(raw)
        except price_template.PriceTemplateError as exc:
            raise errors.AppError(errors.TEMPLATE_INVALID, f"定价模板不可用：{exc}",
                                  status=422, details={"field": "template"}) from None
        setting_service.save_template(db, normalized)
        db.commit()
    audit_service.record(db, operator=operator, action="template.save",
                         target_kind="template",
                         target_id=str(normalized.get("name") or ""),
                         detail={"name": normalized.get("name"),
                                 "version": normalized.get("version")})
    db.commit()
    return {"template": normalized, "templates": templates(db)["templates"]}


@router.delete("/templates/{name}", summary="删除定价模板",
               dependencies=[require_permission("process")])
def delete_template(name: str, db: DbSession,
                    operator: OperatorDep) -> dict[str, Any]:
    require_writable(db)

    if not setting_service.delete_template(db, name):
        raise errors.not_found(f"定价模板「{name}」不存在", template=name)
    db.commit()
    audit_service.record(db, operator=operator, action="template.delete",
                         target_kind="template", target_id=name,
                         detail={"name": name})
    db.commit()
    return {"deleted": True, "name": name,
            "templates": templates(db)["templates"]}


@router.get("/logs/tail", summary="服务端运行日志尾部（排查用）")
def logs_tail(limit: str | None = None) -> dict[str, Any]:
    """返回 server.log 最后 N 行。

    老版把运行日志直接铺在界面上（index.html#logs），出问题能当场看到。
    新版日志只落文件，用户拿不到；这里补一个只读窗口。
    """
    count = 200
    if limit:
        try:
            count = max(1, min(2000, int(limit)))
        except (TypeError, ValueError):
            raise errors.validation("limit 必须是整数", field="limit") from None
    path = get_settings().logs_dir / "server.log"
    if not path.exists():
        return {"lines": [], "path": str(path), "missing": True}
    # 只读尾部：日志可能很大，整文件读进来会卡住请求
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            tail = collections.deque(handle, maxlen=count)
        return {"lines": [line.rstrip() for line in tail], "path": str(path)}
    except OSError as exc:
        raise errors.AppError(errors.INTERNAL, f"读取日志失败：{exc}", status=500) from None


@router.get("/maintenance/backfill", summary="清点仓库行缺失的账号归属")
def backfill_survey(db: DbSession) -> dict[str, Any]:
    """只统计：多少行缺账号、能修多少、哪些店铺对不上。"""
    from services import backfill_service

    return backfill_service.survey(db)


@router.post("/maintenance/backfill", summary="按账号店铺清单回填仓库账号",
             dependencies=[require_permission("admin")])
def backfill_apply(db: DbSession, operator: OperatorDep,
                   payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """默认只出报告；`{"apply": true}` 才写库。

    只补 account_id 为空的行，已有的不动——覆盖人工修正过的归属
    会把下一次操作发到错的连接上。

    写库那一支要留痕：回填会把一批商品的 account_id 指向某个连接，
    事后「为什么这些商品跑到这个账号下了」只有这条审计答得出来。
    """
    from services import backfill_service

    body = payload or {}
    dry_run = not parse_bool(body.get("apply"), "apply", False)
    if not dry_run:
        require_writable(db)
    with audit_service.guard(db, operator=operator, action="system.update",
                             target_kind="setting", target_id="backfill"):
        result = backfill_service.apply(db, dry_run=dry_run)
    if not dry_run:
        db.commit()
        audit_service.record(db, operator=operator, action="system.update",
                             target_kind="setting", target_id="backfill",
                             detail={"verb": "backfill",
                                     "fixed": result.get("fixed") or 0,
                                     "missing": result.get("missing") or 0})
        db.commit()
    return result


@router.get("/maintenance/prune", summary="清点可清理的运行时缓存")
def prune_survey(days: float = 7.0) -> dict[str, Any]:
    """只出报告，不删任何文件。"""
    from services import prune_service

    if days < 0:
        raise errors.validation("天数不能为负", field="days")
    report = prune_service.prune(get_settings().data_dir, days, confirm=False)
    report.pop("deletable", None)
    return report


@router.post("/maintenance/prune", summary="清理运行时缓存（只删图片缓存，不动记录）",
             dependencies=[require_permission("admin")])
def prune_run(payload: dict[str, Any] | None = None, db: DbSession = None,
              operator: OperatorDep = None) -> dict[str, Any]:
    """`{"days": 7, "confirm": true}` 才真删。

    保护线由 prune_service 保证：记录类文件任何开关都不删；
    图片缓存里被运行记录按哈希引用的也放过。

    真删了才记审计：confirm=false 只是出报告，目录里一个文件都没动。
    """
    from services import prune_service

    require_writable(db)
    body = payload or {}
    try:
        days = float(body.get("days", 7))
    except (TypeError, ValueError):
        raise errors.validation("天数必须是数字", field="days") from None
    if days < 0:
        raise errors.validation("天数不能为负", field="days")
    confirm = parse_bool(body.get("confirm"), "confirm", False)
    result = prune_service.prune(get_settings().data_dir, days, confirm=confirm)
    if confirm and db is not None:
        audit_service.record(
            db, operator=operator, action="system.update",
            target_kind="setting", target_id="prune",
            detail={"verb": "prune", "days": days,
                    "files": result.get("deleted") or 0,
                    "bytes": result.get("bytes") or 0})
        db.commit()
    return result


# --- 局域网共享部署的运维面 -------------------------------------------------
# 追加在文件末尾，不改动上面任何一条已有路由的注册顺序：FastAPI 按注册顺序
# 匹配，插在中间会让 `/system/share` 与将来的 `/system/{something}` 互相吃掉。
# 详见 docs/shared-deployment.md。


@health_router.get("/share/health", summary="共享服务端探活（无鉴权，客户端拿 token 前用）")
def share_health(request: Request) -> dict[str, Any]:
    """免鉴权探活：客户端还没拿到 token 时就要先问「服务器活着吗」。

    为什么不复用 `/system/health`：那个只回答「库能连」，客户端在共享部署里
    还需要知道「是不是共享模式、别人该连哪个地址、现在几个人在线、版本对不对」。
    版本对不上时客户端要弹提示而不是白屏，所以 `serverVersion` 必须在这里。

    返回里刻意**没有** dbPath / dataDir：这是整个网段都能打的地址，
    把服务器目录结构暴露出去没有任何好处。
    """
    from services import share_service

    share_service.note_client(request, authenticated=False)
    return share_service.health_view()


@router.get("/share", summary="共享状态：监听地址、本机 IP、在线客户端、库健康")
def share_status(request: Request, db: DbSession) -> dict[str, Any]:
    """运维看一眼就知道「现在能不能把地址给别人」。

    `lanAddresses` 是本机所有可对外的 IPv4（过滤 127.x / 169.254.x / 组播），
    按「私网优先」排序 —— 第一个通常就是该发给同事的那个。
    本机只有回环时返回空数组，并在 `warnings` 里说明，而不是编一个地址出来。

    `bindHost` 是**配置值**（重启后生效的那个），不是「此刻内核真在听什么」：
    两者在「刚点了共享但没重启」时不一致，这正是要暴露出来的状态。
    """
    from services import share_service

    share_service.note_client(request, authenticated=True)
    return share_service.share_view(db)


@router.post("/share", summary="切换共享模式（落库，需重启生效）",
             dependencies=[require_permission("admin")])
def set_share(request: Request, payload: dict[str, Any], db: DbSession,
              operator: OperatorDep) -> dict[str, Any]:
    """`{"sharedMode": true}` —— 写进 settings 表，不只改内存。

    为什么不改内存就完事：`bind_host` 是 uvicorn 的启动参数，进程活着改不了；
    只改内存的结果是「界面说共享了，同事还是连不上」，重启后内存态又丢，
    等于这个开关从来没生效过。所以落库 + 明说 `restartRequired`。

    这个开关不套 `require_writable`：只读模式（security.readOnly）是防止误改
    业务数据的，而共享开关属于部署配置 —— 恰恰是最需要「出事时还能改」的那类。
    """
    from services import share_service

    share_service.note_client(request, authenticated=True)
    raw = payload.get("sharedMode") if isinstance(payload, dict) else None
    if raw is None:
        raise errors.validation("缺少 sharedMode", field="sharedMode")
    value = parse_bool(raw, "sharedMode")
    result = share_service.set_shared_mode(db, value)
    db.commit()
    # 共享开关决定「别人能不能连进来」：谁的电脑、什么时候开的，运维要查得到
    audit_service.record(db, operator=operator, action="system.update",
                         target_kind="setting", target_id="sharedMode",
                         detail={"verb": "share", "sharedMode": bool(value),
                                 "restartRequired": bool(
                                     result.get("restartRequired"))})
    db.commit()
    return result
