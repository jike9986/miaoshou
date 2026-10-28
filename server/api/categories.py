"""类目路由：树查询与同步。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from api import reference
from api.deps import CredentialsHeader, DbSession, OperatorDep, require_writable
from api.helpers import parse_bool
from core import errors
from schemas.requests import CategorySyncRequest
from services import account_service, audit_service, category_service, product_service
from services.account_service import account_client, map_upstream_error
from services.miaoshou import MiaoshouError

router = APIRouter(prefix="/categories", tags=["类目"])


@router.get("", summary="类目树（默认展开 2 层）")
def list_categories(
    db: DbSession,
    site: str | None = None,
    accountId: str | None = None,
    rootCid: str | None = None,
    depth: int = Query(default=2, ge=1, le=6),
    leafOnly: str | None = None,
    refresh: str | None = None,
) -> dict[str, Any]:
    account = account_service.get_account(db, accountId) if accountId else None
    site_code = (site or (account.site if account else "") or "MY").upper()
    leaf_flag = parse_bool(leafOnly, "leafOnly", False)
    refresh_flag = parse_bool(refresh, "refresh", False)

    rows, scope = category_service.scope_rows(db, site_code, account.id if account else None)
    stale = False
    if not rows or refresh_flag:
        target = account or account_service.current_account(db)
        if target is None:
            if not rows:
                raise errors.not_connected("尚未连接妙手，且本地没有该类目缓存")
        else:
            client = account_client(target)
            try:
                category_service.sync(db, client, site=site_code, account_id=target.id)
                db.commit()
            except MiaoshouError as exc:
                if not rows:
                    raise map_upstream_error(exc) from None
                stale = True
            finally:
                client.close()
            rows, scope = category_service.scope_rows(db, site_code, target.id)

    if not rows:
        raise errors.not_found(f"站点 {site_code} 还没有类目缓存，请先同步", site=site_code)
    synced_at = max((row.synced_at or 0) for row in rows) if rows else None
    # 统计必须跟返回的树同一个 scope：类目树按账号缓存，回落到全局哨兵时也照此计
    leaf_total = sum(1 for row in rows if row.is_leaf)
    return {
        "site": site_code,
        "rootCid": rootCid or None,
        "syncedAt": synced_at,
        "stale": stale,
        "tree": category_service.build_tree(rows, root_cid=rootCid, depth=depth,
                                            leaf_only=leaf_flag),
        "stats": {"total": len(rows), "leaf": leaf_total, "cached": True,
                  "scope": str(scope), "accountId": str(account.id) if account else None},
    }


@router.post("/sync", summary="从妙手拉类目树入库")
def sync_categories(payload: CategorySyncRequest, db: DbSession,
    operator: OperatorDep,
    credentials: CredentialsHeader = None,
) -> dict[str, Any]:
    require_writable(db)
    account = account_service.resolve_account(db, payload.accountId, credentials)
    site_code = (payload.site or account.site or "MY").upper()
    client = account_client(account)
    try:
        with audit_service.guard(db, operator=operator, action="system.update",
                                 target_kind="category",
                                 target_id=str(site_code)[:16],
                                 account_id=account.id if account else None):
            result = category_service.sync(db, client, site=site_code,
                                          account_id=account.id)
    except MiaoshouError as exc:
        raise map_upstream_error(exc) from None
    finally:
        client.close()
    db.commit()
    # 类目树是全店共用的：同步一次会整体替换该站点的类目，别人正在用的
    # cid 可能就此失效。谁在什么时候同步的，得查得到。
    audit_service.record(db, operator=operator, action="system.update",
                         target_kind="category", target_id=str(site_code)[:16],
                         account_id=account.id if account else None,
                         detail={"verb": "category.sync",
                                 "site": site_code,
                                 "synced": result.get("synced") or 0,
                                 "total": result.get("total") or 0})
    db.commit()
    return result


@router.get("/{cid}/path", summary="类目面包屑")
def category_path(cid: str, db: DbSession,
    credentials: CredentialsHeader = None, site: str | None = None,
                  accountId: str | None = None) -> dict[str, Any]:
    account = account_service.get_account(db, accountId) if accountId else None
    site_code = (site or (account.site if account else "") or "MY").upper()
    row = category_service.find_by_cid(db, cid, site=site_code,
                                      account_id=account.id if account else None)
    if row is None:
        raise errors.not_found(f"类目 {cid} 不存在", cid=cid)
    return {
        "cid": row.cid,
        "parentCid": row.parent_cid,
        "name": row.name,
        "nameZh": row.name_cn,
        "path": row.path,
        "depth": row.depth,
        "isLeaf": bool(row.is_leaf),
        "syncedAt": row.synced_at,
    }


# --- 类目三档兜底 + 必填属性（只读） ---------------------------------------
# 两个 P0 缺口的入口：
#   P0-1 采集箱商品没 cid 时，运营不知道要去妙手手选类目，整批入不了仓；
#   P0-2 类目要求包裹尺寸 / 必填属性的商品照样入仓，上架被平台拒。
#
# 路由顺序：`/match` 是单段静态路径，`/{cid}/requirements` 是「动态段 + 静态
# 后缀」，与已有的 `/{cid}/path`、`/{cid}/metadata` 段数都不同，FastAPI 不会
# 互相吃掉；本文件也没有 `/{cid}` 单段动态路由，所以静态段放哪都安全。
@router.get("/match", summary="三档兜底解析类目 cid（只读）")
def match_category(
    db: DbSession,
    detailId: str | None = Query(default=None, description="采集箱明细 id（必填）"),
    shopId: str | None = Query(default=None, description="目标店铺 id（必填）"),
    site: str | None = None,
    accountId: str | None = None,
    credentials: CredentialsHeader = None,
) -> dict[str, Any]:
    """把「这件商品该归哪个类目」按老版三段式定下来。

    判定顺序（老版 product_workflow._resolve_category :555-602）：
      1. 商品已有 cid → source="existing"，不回源；
      2. 妙手 AI 匹配（ai_match_cid_by_shop_ids）且结果唯一 → source="ai"；
      3. 本地类目树名字反查命中唯一 → source="library"；
      4. 拖鞋关键词 + 「运动拖鞋」唯一命中 → source="keyword"；
      5. 全失败 → cid 为空 + 老版原始提示语（**不报错**）。

    为什么不抛错：这句提示语是要原样显示给运营的操作指引，抛成 4xx/5xx
    会被上层包成通用错误，提示语就丢了。
    """
    account = reference.resolve_account_for(db, accountId, credentials)
    wanted_detail = reference.parse_shop_id(detailId)  # 同一个「必须是正整数」校验
    wanted_shop = reference.parse_shop_id(shopId)
    site_code = (site or (getattr(account, "site", "") or "") or "MY").upper()

    # 先取明细：cid 已经存在的商品根本不需要回源 AI 匹配（省一次 1.5s 节流）。
    client = account_client(account)
    try:
        snapshot = client.shop_detail(wanted_detail, wanted_shop)
    except MiaoshouError as exc:
        raise map_upstream_error(exc) from None
    finally:
        client.close()
    info = product_service.detail_block(snapshot)["info"]

    client = account_client(account)
    try:
        result = category_service.resolve_category(
            db, client=client, detail_id=wanted_detail, shop_id=wanted_shop,
            info=info, account_id=account.id, site=site_code)
    except MiaoshouError as exc:
        # resolve_category 内部已把 AI 匹配失败降级成下一档；走到这里说明是
        # 取明细之后的本地上库查询出了问题，照实报上游错误。
        raise map_upstream_error(exc) from None
    finally:
        client.close()

    return {
        "accountId": str(account.id),
        "detailId": str(wanted_detail),
        "shopId": str(wanted_shop),
        "site": site_code,
        "cid": result.get("cid") or "",
        "source": result.get("source") or "",
        "message": result.get("message") or "",
        # 以下三个是排查用的补充信息，前端可以忽略
        "name": result.get("name") or "",
        "nameChinese": result.get("nameChinese") or "",
        "reason": result.get("reason") or "",
    }


@router.get("/{cid}/requirements", summary="类目必填项摘要（只读）")
def category_requirements(cid: str, db: DbSession,
    credentials: CredentialsHeader = None, site: str | None = None,
                          shopId: str | None = None,
                          shopIds: str | None = None,
                          accountId: str | None = None,
                          refresh: str | None = None) -> dict[str, Any]:
    """类目要求什么才能发布：包裹尺寸是否必填、哪些属性必填。

    与 `/{cid}/metadata` 的区别：metadata 是整份上游响应（给表单渲染下拉选项
    用），requirements 只回答「保存前校验需要知道的那两件事」，正好是
    `category_service.extract_requirements` 的输出，可以直接喂给
    `pre_save_check.pre_save_issues(requirements=...)`。

    上游必须带 shopIds（同一 cid 在不同店铺下的必填项可能不同），
    `shopId` 单数写法也认，方便前端从采集箱页直接带过来。
    """
    account = reference.resolve_account_for(db, accountId, credentials)
    wanted_cid = reference.parse_cid(cid)
    wanted_shops = reference.parse_shop_ids(shopIds or shopId)
    site_code = (site or (getattr(account, "site", "") or "") or "MY").upper()
    refresh_flag = parse_bool(refresh, "refresh", False)
    client = account_client(account)
    try:
        payload, cached_flag, degraded, stale = reference.cached(
            account, f"requirements|site={site_code}|cid={wanted_cid}"
                     f"|shops={','.join(str(s) for s in wanted_shops)}",
            lambda: client.category_metadata(site_code, wanted_cid, wanted_shops),
            refresh=refresh_flag)
    except reference.UpstreamUnsupported as exc:
        return reference.unsupported_envelope(
            account, exc.reason, cid=str(wanted_cid), site=site_code,
            shopIds=wanted_shops, packageDimensionRequired=False,
            requiredAttributes=[])
    except MiaoshouError as exc:
        raise map_upstream_error(exc) from None
    finally:
        client.close()
    requirements = category_service.extract_requirements(payload)
    return {
        **reference.envelope(account, cached_flag=cached_flag, degraded=degraded,
                             stale_seconds=stale),
        "cid": str(wanted_cid),
        "site": site_code,
        "shopIds": wanted_shops,
        **requirements,
        # raw 留着给排查用：上游把必填项藏在别的字段名下时，能直接看原始结构
        "raw": payload,
    }
# 老版 `metadata` 子命令（cli.py:110-141）。/categories/{cid}/path 只回答
# 「这个类目在树的哪个位置」，回答不了「发布它要填哪些字段」——那是另一个
# 上游接口（get_category_metadata），必须带 shopIds，因为同一 cid 在不同
# 店铺下的必填项可能不同（平台按店铺授权/类目资质下发）。
#
# 路由顺序：与 /{cid}/path 并列，都是「动态段 + 静态后缀」，互不冲突。
@router.get("/{cid}/metadata", summary="类目发布要求（只读）")
def category_metadata(cid: str, db: DbSession,
    credentials: CredentialsHeader = None, site: str | None = None,
                      shopIds: str | None = None,
                      accountId: str | None = None,
                      refresh: str | None = None) -> dict[str, Any]:
    account = reference.resolve_account_for(db, accountId, credentials)
    wanted_cid = reference.parse_cid(cid)
    wanted_shops = reference.parse_shop_ids(shopIds)
    refresh_flag = parse_bool(refresh, "refresh", False)
    site_code = (site or (getattr(account, "site", "") or "") or "MY").upper()
    client = account_client(account)
    try:
        payload, cached_flag, degraded, stale = reference.cached(
            account, f"metadata|site={site_code}|cid={wanted_cid}"
                     f"|shops={','.join(str(s) for s in wanted_shops)}",
            lambda: client.category_metadata(site_code, wanted_cid, wanted_shops),
            refresh=refresh_flag)
    except reference.UpstreamUnsupported as exc:
        return reference.unsupported_envelope(account, exc.reason,
                                              cid=str(wanted_cid), site=site_code,
                                              shopIds=wanted_shops, metadata=None)
    except MiaoshouError as exc:
        raise map_upstream_error(exc) from None
    finally:
        client.close()
    return {
        **reference.envelope(account, cached_flag=cached_flag, degraded=degraded,
                             stale_seconds=stale),
        "cid": str(wanted_cid),
        "site": site_code,
        "shopIds": wanted_shops,
        **reference.metadata_summary(payload),
    }
