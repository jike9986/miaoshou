"""同步路由：把老版两条「同步类」接口补齐。

  · `POST /sync/price-templates` ← 老版 `/api/price-sync`
      从妙手拉定价模板列表给前端做下拉。只读，**不落库**：
      本地模板库（`settings['pricing.templates']`）由 `setting_service` 单独管，
      回源结果混进去会让「自建模板」和「上游快照」分不清。

  · `POST /sync/source-links` ← 老版 `/api/sync-source-links`
      从已入仓的成品 payload 里回收 1688 货源链接，塞回链接池
      （`settings['carrier.links']`），供载体重建取料。写操作，过 require_writable。

两条都挂在 `/sync` 前缀下，鉴权走 `api/__init__.py` 的统一 token 守卫。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import Field

from api.deps import CredentialsHeader, DbSession, OperatorDep, require_writable
from core import errors
from schemas.common import CamelModel
from services import account_service, audit_service, price_template, sync_service
from services.account_service import map_upstream_error
from services.miaoshou import MiaoshouError

router = APIRouter(prefix="/sync", tags=["同步"])


class PriceTemplateRequest(CamelModel):
    """定价模板回源参数。

    这里刻意不加 pydantic 的 ge/le 约束：区间校验统一在 sync_service 里做，
    报错文案才是中文，错误码也统一走 core/errors.py 的 VALIDATION_ERROR。
    """

    site: str | None = Field(default=None, description="站点代码，默认 MY，自动转大写")
    name: str | None = Field(default=None, description="模板名关键字，空表示不过滤")
    pageNo: int | str | None = Field(default=None, description="页码，从 1 起")
    pageSize: int | str | None = Field(default=None, description="每页数量，1–20（上游限制）")
    accountId: str | None = Field(default=None, description="用哪个已保存账号的凭据")
    templateName: str | None = Field(
        default=None,
        description="同步时选哪一套上游模板；妙手有多套且不指定会报错并列名字")
    save: bool = Field(
        default=False,
        description="true 时把合并结果落库并设为当前模板；默认 false 只预览不落库")


class SourceLinkRequest(CamelModel):
    """货源链接回收范围：warehouseIds 与 all=true 二选一。"""

    warehouseIds: list[str | int] | None = Field(
        default=None, description="要扫描的仓库 id（或 {detailId}:{shopId} 键）")
    all: bool = Field(default=False, description="扫描全部未删除商品，与 warehouseIds 互斥")
    limit: int | str | None = Field(default=None, description="最多扫描多少件，默认 500，上限 2000")


@router.post("/price-templates", summary="从妙手拉取定价模板列表（只读，不落库）")
def sync_price_templates(
    payload: PriceTemplateRequest,
    db: DbSession,
    credentials: CredentialsHeader = None,
) -> dict[str, Any]:
    """等价老版 `/api/price-sync`：上游模板名 / 站点 / 币种原样返回给前端。

    `save=true` 时改走 `sync_price_templates`（三级合并 + 落库 + 设为当前），
    这是老版 `service_price.sync` 的完整链路。默认 false 保持原有只读语义 ——
    已有的调用方和前端下拉都靠这个「不落库」的承诺。
    """
    account = account_service.resolve_account(db, payload.accountId, credentials)
    if payload.save:
        # 落库是写操作，必须先过只读闸；预览路径不写库所以不拦。
        require_writable(db)
        try:
            return sync_service.sync_price_templates(
                db, site=payload.site, account_id=account.id,
                template_name=payload.templateName or "", save=True,
            )
        except price_template.PriceTemplateError as exc:
            raise errors.unprocessable(str(exc)) from None
        except MiaoshouError as exc:
            raise map_upstream_error(exc) from None
    client = account_service.account_client(account)
    try:
        return sync_service.fetch_price_templates(
            client,
            site=payload.site,
            name=payload.name,
            page_no=payload.pageNo,
            page_size=payload.pageSize,
        )
    except MiaoshouError as exc:
        raise map_upstream_error(exc) from None
    finally:
        client.close()


@router.get("/price-templates/preview", summary="定价模板同步预览（只读）")
def preview_price_templates(
    db: DbSession,
    site: str | None = None,
    accountId: str | None = None,
    templateName: str | None = None,
    credentials: CredentialsHeader = None,
) -> dict[str, Any]:
    """先看再选：返回归一化后的上游模板列表 + 三级合并结果，不落库。

    妙手同一站点可能有多套模板（「马来」和「马来 2.0」利润率不同）。前端要先
    把名字列出来让运营选，选完再带 `templateName` 去落库。多套且没指定时
    `pick_upstream_template` 会抛错，这里把错误也当正常结果返回 —— 让前端
    能显示「请先选择」而不是弹一个 500。
    """
    account = account_service.resolve_account(db, accountId, credentials)
    try:
        return sync_service.sync_price_templates(
            db, site=site, account_id=account.id,
            template_name=templateName or "", save=False,
        )
    except price_template.PriceTemplateError as exc:
        # 多套模板未指定是「需要人做决定」，不是故障
        return {"synced": False, "message": str(exc), "templates": [],
                "merged": None, "selected": "", "saved": False}
    except MiaoshouError as exc:
        raise map_upstream_error(exc) from None


@router.post("/source-links", summary="从已有成品回收 1688 货源链接进池子")
def sync_source_links(payload: SourceLinkRequest, db: DbSession,
                      operator: OperatorDep) -> dict[str, Any]:
    """等价老版 `/api/sync-source-links`：按仓库 id 或全量扫描，回收 offer 链接。"""
    # 先报参数错误、再拦只读：参数缺失时给出的是「该补什么」而不是「只读模式」，
    # 调用方不用先关只读才知道 body 写错了。
    scope = sync_service.plan_source_scan(
        warehouse_ids=payload.warehouseIds,
        scan_all=bool(payload.all),
        limit=payload.limit,
    )
    require_writable(db)
    with audit_service.guard(db, operator=operator, action="system.update",
                             target_kind="setting", target_id="carrier"):
        result = sync_service.harvest_source_links(db, scope)
    db.commit()
    # 和 POST /carrier/links 改的是同一份共享货源池，action 也保持一致
    audit_service.record(db, operator=operator, action="system.update",
                         target_kind="setting", target_id="carrier",
                         detail={"verb": "carrier.sync",
                                 "scanned": result.get("scanned") or 0,
                                 "added": result.get("added") or 0})
    db.commit()
    return result
