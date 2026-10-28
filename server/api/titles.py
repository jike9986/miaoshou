"""标题工具路由：清理预览 + 单标题保存。

对应老版「标题清理预览」与 `/api/save-title` 两条能力：

* `POST /titles/preview` —— 只算不改：按本地违规词库算出清理后的标题与命中词，
  不写库、不调妙手。`titles` 与 `warehouseIds` 二选一（同时给时以 `titles` 为准）。
* `PATCH /titles/{ident}` —— 保存单个商品标题，写本地库；
  **明确不回写妙手草稿**，妙手侧标题不会因此变化。

`ident` 既接受数据库 id，也接受 `{detailId}:{shopId}` 形式的 warehouseKey。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import Field

from api.deps import DbSession, OperatorDep, require_writable
from core import errors
from schemas.common import CamelModel
from services import audit_service, title_service

router = APIRouter(prefix="/titles", tags=["标题清理"])


# --- 请求模型 -------------------------------------------------------------
class TitlePreviewRequest(CamelModel):
    """预览入参：`titles` 与 `warehouseIds` 二选一。"""

    titles: list[str] = Field(default_factory=list)
    warehouse_ids: list[str] = Field(default_factory=list)
    limit: int | None = None
    # 把过时的采集年份换成今年（老版 transforms.refresh_title_years）。
    # 默认关：改标题有风险，必须显式要求。
    refresh_years: bool = False


class TitlePatchRequest(CamelModel):
    """保存入参：`clean=true` 时先按违规词库清理再保存。"""

    title: str | None = None
    clean: bool = False


class TitleSaveRequest(CamelModel):
    """写回妙手草稿的入参（老版 title_editor.save）。

    几项都是硬要求，缺一不可：
      · confirm=true：写错标题不可撤销，必须显式确认
      · revision：读快照时拿到的版本号；不一致说明别人改过，拒绝覆盖
      · reviewConfirmed：命中复核词时必须显式确认已人工看过
    """

    accountId: str | None = None
    detailId: str | None = None
    shopId: str | None = None
    title: str = ""
    confirm: bool = False
    reviewConfirmed: bool = False
    revision: str = ""


# --- 预览 -----------------------------------------------------------------
@router.post("/preview", summary="标题清理预览（不写库、不调妙手）")
def preview_titles(payload: TitlePreviewRequest, db: DbSession) -> dict[str, Any]:
    """按违规词库算出清理后的标题；只读接口，不写库也不调上游。"""
    return title_service.preview(db, titles=payload.titles,
                                 warehouse_ids=payload.warehouse_ids,
                                 limit=payload.limit,
                                 refresh_years=payload.refresh_years)


# --- 保存 -----------------------------------------------------------------
@router.post("/save-to-miaoshou", summary="把标题写回妙手草稿并回读确认")
def save_to_miaoshou(payload: TitleSaveRequest, db: DbSession,
                     operator: OperatorDep) -> dict[str, Any]:
    """真正改妙手侧标题。

    与 `PATCH /titles/{ident}` 的区别：那个只动本地库，妙手侧不变；
    这个会调 `save_shop` 写上游，所以五道闸门一道都不能省
    （确认、长度、待清理词、复核词、版本号），并且保存前备份、
    保存后回读。
    """
    require_writable(db)
    from core.utils import product_key
    from services import title_save_service

    if not payload.detailId or not payload.shopId:
        raise errors.validation("需要 detailId 与 shopId", field="detailId")
    target = product_key(payload.detailId, payload.shopId)
    with audit_service.guard(db, operator=operator, action="title.save",
                             target_kind="product", target_id=target,
                             shop_id=str(payload.shopId),
                             detail={"detailId": str(payload.detailId)}):
        result = title_save_service.save_title(
            db,
            account_id=payload.accountId,
            detail_id=payload.detailId,
            shop_id=payload.shopId,
            title=payload.title,
            confirm=payload.confirm is True,
            review_confirmed=payload.reviewConfirmed is True,
            expected_revision=payload.revision,
        )
        db.commit()
    audit_service.record(db, operator=operator, action="title.save",
                         target_kind="product", target_id=target,
                         shop_id=str(payload.shopId),
                         detail={"detailId": str(payload.detailId),
                                 "title": str(payload.title or "")[:200]})
    db.commit()
    return result


@router.patch("/{ident}", summary="保存单个商品标题（只改本地，不回写妙手草稿）")
def save_title(ident: str, payload: TitlePatchRequest, db: DbSession,
               operator: OperatorDep) -> dict[str, Any]:
    """保存单个商品标题：ident 为数据库 id 或 `{detailId}:{shopId}`。

    `clean=true` 时先按违规词库清理（remove 类删除、review 类只记录）再落库。
    **只改本地库，不回写妙手草稿**——妙手侧商品标题不会随之变化。
    """
    require_writable(db)
    if payload.title is None:
        raise errors.validation("请提供要保存的标题", field="title")
    with audit_service.guard(db, operator=operator, action="title.save",
                             target_kind="product", target_id=str(ident)):
        result = title_service.save(db, ident, title=payload.title,
                                    clean=payload.clean)
        db.commit()
    audit_service.record(db, operator=operator, action="title.save",
                         target_kind="product", target_id=str(ident),
                         detail={"title": str(payload.title or "")[:200],
                                 "clean": bool(payload.clean)})
    db.commit()
    return result
