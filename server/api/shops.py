"""店铺路由。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from api import reference
from api.deps import CredentialsHeader, DbSession
from api.helpers import parse_bool, parse_int
from core.constants import SITE_LANGUAGES, SITE_NAMES
from services import account_service, publish_service
from services.account_service import account_client, map_upstream_error
from services.miaoshou import MiaoshouError

router = APIRouter(prefix="/shops", tags=["店铺"])


@router.get("", summary="店铺列表（可回源）")
def list_shops(
    db: DbSession,
    credentials: CredentialsHeader = None,
    accountId: str | None = None,
    refresh: bool | None = None,
) -> dict[str, Any]:
    account = account_service.resolve_account(db, accountId, credentials)
    refresh_flag = parse_bool(refresh, "refresh", False)
    shops, degraded = account_service.fetch_shops(db, account, refresh=refresh_flag)
    if not degraded:
        db.commit()
    return {
        "accountId": str(account.id),
        "accountName": account.name,
        "shops": shops,
        "siteLanguages": SITE_LANGUAGES,
        "siteNames": SITE_NAMES,
        "degraded": degraded,
    }


# --- 店铺参考数据（只读） ---------------------------------------------------
# 老版 CLI 的 warehouses / brands / manufacturers / responsible 四个子命令
# （cli.py:188 / :221 / :225 / :229）。它们都是「店铺维度的上游字典」，
# 用来填发布表单的下拉框，所以挂在 /shops 下。
#
# 路由顺序：这四条全是静态路径，本文件里也没有 /shops/{something} 动态路由，
# 不存在被吃掉的风险；仍按「静态在前」的习惯放在最前面，将来加动态路由时
# 不会踩坑。全部只读：不套 require_writable，但都要账号（需要客户端）。
@router.get("/warehouses", summary="店铺仓库列表（只读）")
def shop_warehouses(
    db: DbSession,
    shopId: str | None = Query(default=None, description="店铺 id（必填）"),
    accountId: str | None = None,
    credentials: CredentialsHeader = None,
    refresh: str | None = None,
) -> dict[str, Any]:
    account = reference.resolve_account_for(db, accountId, credentials)
    wanted = reference.parse_shop_id(shopId)
    refresh_flag = parse_bool(refresh, "refresh", False)
    client = account_client(account)
    try:
        payload, cached_flag, degraded, stale = reference.cached(
            account, f"warehouses|shop={wanted}",
            lambda: client.warehouse_list([wanted]), refresh=refresh_flag)
    except reference.UpstreamUnsupported as exc:
        return reference.unsupported_envelope(account, exc.reason, shopId=wanted,
                                              warehouses=[])
    except MiaoshouError as exc:
        raise map_upstream_error(exc) from None
    finally:
        client.close()
    return {
        **reference.envelope(account, cached_flag=cached_flag, degraded=degraded,
                             stale_seconds=stale),
        "shopId": wanted,
        "warehouses": reference.warehouse_rows(payload),
    }


@router.get("/brands", summary="店铺品牌列表（只读）")
def shop_brands(
    db: DbSession,
    shopId: str | None = Query(default=None, description="店铺 id（必填）"),
    accountId: str | None = None,
    credentials: CredentialsHeader = None,
    refresh: str | None = None,
    allPages: str | None = Query(default=None, alias="all",
                                 description="true = 翻完所有页（忽略 pageToken）"),
    pageToken: str | None = Query(default=None, description="上一页返回的 nextPageToken"),
    name: str | None = Query(default=None, description="品牌名关键字（交给上游搜索）"),
    maxPages: str | None = Query(default=None,
                                 description="all=true 时的翻页保险丝，默认 20"),
) -> dict[str, Any]:
    account = reference.resolve_account_for(db, accountId, credentials)
    wanted = reference.parse_shop_id(shopId)
    refresh_flag = parse_bool(refresh, "refresh", False)
    all_flag = parse_bool(allPages, "all", False)
    token = str(pageToken or "").strip()
    keyword = str(name or "").strip()
    fuse = parse_int(maxPages, "maxPages", reference.BRAND_MAX_PAGES, low=1, high=200)

    # all=true 与 pageToken 同时给：**以 all 为准**，并把被忽略的游标回显出来。
    # 不报 400 的理由：前端「加载全部」只是一个开关，URL 里往往还留着上一页
    # 的游标；一按开关就 400，等于要求前端先手动清参才能用。也不静默吞掉 ——
    # ignoredPageToken 让调用方看得见自己传的东西没生效。
    ignored_token = bool(token and all_flag)
    if all_flag:
        token = ""

    client = account_client(account)
    try:
        if all_flag:
            def load_all() -> dict[str, Any]:
                return client.brand_list_all(wanted, max_pages=fuse, name=keyword)

            payload, cached_flag, degraded, stale = reference.cached(
                account,
                reference.brand_cache_key(wanted, all_pages=True, name=keyword),
                load_all, refresh=refresh_flag,
                # 翻到一半失败的结果（truncated 且带 error）不能进缓存：
                # 缓存 5 分钟等于把「只翻了两页」当成「这个店就这些品牌」，
                # 用户刷新也不会好，只有重启进程才能恢复。
                cacheable=lambda value: not (isinstance(value, dict)
                                             and value.get("truncated")
                                             and value.get("error")))
        else:
            def load_page() -> dict[str, Any]:
                return client.brand_list_page(wanted, page_token=token, name=keyword)

            payload, cached_flag, degraded, stale = reference.cached(
                account,
                reference.brand_cache_key(wanted, page_token=token, name=keyword),
                load_page, refresh=refresh_flag)
    except reference.UpstreamUnsupported as exc:
        return reference.unsupported_envelope(account, exc.reason, shopId=wanted,
                                              brands=[], nextPageToken=None,
                                              totalCount=None, truncated=False,
                                              pages=0, all=all_flag,
                                              pageToken=token or None,
                                              name=keyword or None,
                                              complete=None, error=None,
                                              ignoredPageToken=ignored_token)
    except MiaoshouError as exc:
        raise map_upstream_error(exc) from None
    finally:
        client.close()

    view = reference.brand_page_view(payload, all_pages=all_flag)
    return {
        **reference.envelope(account, cached_flag=cached_flag, degraded=degraded,
                             stale_seconds=stale),
        "shopId": wanted,
        "brands": view["brands"],
        # 品牌是分页接口：上游一次只回一页（实测 100 条 / 总数 10000），
        # 不把游标和总数带出来，前端会以为「这个店只有 100 个品牌」
        "paging": {
            "totalCount": view["totalCount"],
            "returned": len(view["brands"]),
            "nextPageToken": view["nextPageToken"],
            "truncated": view["truncated"],
        },
        # 下面这几个是分页新增的顶层字段：老前端只读 brands / paging 不受影响，
        # 新前端可以直接拿 nextPageToken 做「加载更多」。
        "nextPageToken": view["nextPageToken"],
        "totalCount": view["totalCount"],
        "truncated": view["truncated"],
        "pages": view["pages"],
        "all": all_flag,
        "pageToken": token or None,
        "name": keyword or None,
        "complete": view["complete"],
        "error": view["error"],
        "ignoredPageToken": ignored_token,
    }


@router.get("/manufacturers", summary="店铺制造商列表（只读）")
def shop_manufacturers(
    db: DbSession,
    shopId: str | None = Query(default=None, description="店铺 id（必填）"),
    accountId: str | None = None,
    credentials: CredentialsHeader = None,
    refresh: str | None = None,
) -> dict[str, Any]:
    account = reference.resolve_account_for(db, accountId, credentials)
    wanted = reference.parse_shop_id(shopId)
    refresh_flag = parse_bool(refresh, "refresh", False)
    client = account_client(account)
    try:
        # refresh 是上游自己的缓存开关（0/1），与本地 refresh 同源：
        # 用户点「刷新」时两边的缓存都该作废，否则刷完还是旧名单。
        payload, cached_flag, degraded, stale = reference.cached(
            account, f"manufacturers|shop={wanted}",
            lambda: client.manufacturer_list(wanted, refresh=1 if refresh_flag else 0),
            refresh=refresh_flag)
    except reference.UpstreamUnsupported as exc:
        return reference.unsupported_envelope(account, exc.reason, shopId=wanted,
                                              manufacturers=[])
    except MiaoshouError as exc:
        raise map_upstream_error(exc) from None
    finally:
        client.close()
    data = payload.get("data") if isinstance(payload, dict) else {}
    rows = reference.manufacturer_rows(payload)
    return {
        **reference.envelope(account, cached_flag=cached_flag, degraded=degraded,
                             stale_seconds=stale),
        "shopId": wanted,
        "manufacturers": rows,
        # 上游对没有制造商的账号回空数组而不是报错：明确告诉前端这是「空」，
        # 不是「没查」（见 reference.UpstreamUnsupported 的注释）
        "empty": not rows,
        "upstreamData": data if isinstance(data, dict) else {},
    }


@router.get("/responsible", summary="店铺负责人列表（只读）")
def shop_responsible(
    db: DbSession,
    shopId: str | None = Query(default=None, description="店铺 id（必填）"),
    accountId: str | None = None,
    credentials: CredentialsHeader = None,
    refresh: str | None = None,
) -> dict[str, Any]:
    account = reference.resolve_account_for(db, accountId, credentials)
    wanted = reference.parse_shop_id(shopId)
    refresh_flag = parse_bool(refresh, "refresh", False)
    client = account_client(account)
    try:
        payload, cached_flag, degraded, stale = reference.cached(
            account, f"responsible|shop={wanted}",
            lambda: client.responsible_person_list(
                wanted, refresh=1 if refresh_flag else 0),
            refresh=refresh_flag)
    except reference.UpstreamUnsupported as exc:
        return reference.unsupported_envelope(account, exc.reason, shopId=wanted,
                                              responsible=[])
    except MiaoshouError as exc:
        raise map_upstream_error(exc) from None
    finally:
        client.close()
    rows = reference.responsible_rows(payload)
    return {
        **reference.envelope(account, cached_flag=cached_flag, degraded=degraded,
                             stale_seconds=stale),
        "shopId": wanted,
        "responsible": rows,
        "empty": not rows,
    }


@router.get("/languages", summary="站点语言映射")
def site_languages() -> dict[str, Any]:
    return {"siteLanguages": SITE_LANGUAGES, "siteNames": SITE_NAMES}


@router.get("/publishable", summary="可用于发布的店铺（含站点语言）")
def publishable(db: DbSession,
    credentials: CredentialsHeader = None, accountId: str | None = None) -> dict[str, Any]:
    account = account_service.resolve_account(db, accountId, credentials)
    try:
        shops = publish_service.available_shops(db, account)
    except Exception as exc:  # noqa: BLE001
        from services.miaoshou import MiaoshouError

        if isinstance(exc, MiaoshouError):
            raise map_upstream_error(exc) from None
        raise
    db.commit()
    return {"accountId": str(account.id), "shops": shops}
