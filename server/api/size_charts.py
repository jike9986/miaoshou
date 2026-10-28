"""尺寸表路由：老版 `/api/size-charts` + `upload/delete-size-chart` 的等价实现。

老版只存「书签」（已经上传到图床的图片地址），新版沿用同一语义，并补上按站点
解析默认尺寸表的能力（`GET /size-charts/resolve?site=MY`），这样
`settings.workflow.defaults.sizeChartUrl` 才有消费方。

与老版对齐的三处：
  · 列表回 `defaultUrl`（老版 `SizeChartService.list()` 固定带这个字段）；
  · 上传允许「给 URL 而不是文件」（老版前端两种都支持，只是把登记拆到了
    POST /api/size-charts）；
  · 删除除了 id 还认 url（老版 delete-size-chart 的入参只有 url）。

写接口统一 require_writable（只读模式下拒绝）。
"""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import Field

from api.deps import DbSession, OperatorDep, require_permission, require_writable
from core import errors
from schemas.common import CamelModel
from services import audit_service, image_upload, size_chart_service

router = APIRouter(prefix="/size-charts", tags=["尺寸表"])


# --- 请求模型 -------------------------------------------------------------
class SizeChartCreate(CamelModel):
    name: str = Field(max_length=128)
    url: str = Field(max_length=2048)
    site: str | None = Field(default=None, max_length=8)
    content_json: Any = None


class SizeChartPatch(CamelModel):
    name: str | None = Field(default=None, max_length=128)
    url: str | None = Field(default=None, max_length=2048)
    site: str | None = Field(default=None, max_length=8)
    content_json: Any = None


# --- 读 -------------------------------------------------------------------
@router.get("", summary="尺寸表列表（可按站点过滤）")
def list_size_charts(db: DbSession, site: str | None = None) -> dict[str, Any]:
    return size_chart_service.list_charts(db, site=site)


# --- 上传本地图片 ---------------------------------------------------------
# 等价于老版 POST /api/upload-size-chart：
#   读原始字节 → 按魔数校验类型 → 传图床 → 拿到 URL → 记成书签。
# 用原始 body 而不是 multipart，是因为前端只需 PUT 一个 File，省去表单拼装；
# 类型信息走 X-File-Name 头（可选，仅用于取扩展名）。
#
# body 里是 JSON `{"url": "...", "name": "..."}` 时改走「只登记地址」分支：
# 老版上传没有 URL 形态，但新版前端把「上传」和「贴地址」放在同一个弹窗里，
# 都打这个接口；这样调用方不用先判自己手里是文件还是地址。
@router.post("/upload", status_code=201, summary="上传本地尺码图到图床并登记",
             dependencies=[require_permission("process")])
async def upload_size_chart(
    request: Request,
    db: DbSession,
    name: str | None = Query(default=None, max_length=128),
    site: str | None = Query(default=None, max_length=8),
) -> Any:
    require_writable(db)

    length_header = request.headers.get("content-length")
    if length_header:
        try:
            declared_length = int(length_header)
        except ValueError:
            raise errors.validation("请求长度不正确", field="Content-Length") from None
        if declared_length < 0:
            raise errors.validation("请求长度不正确", field="Content-Length")
        # 8 MB 上限只约束图片分支：JSON 分支只放一个地址，长度由 URL_MAX_LENGTH
        # 兜住，不该被图片上限挡掉。
        if (declared_length > image_upload.SIZE_CHART_UPLOAD_LIMIT
                and request.headers.get("content-type", "")
                .split(";", 1)[0].strip().lower() != "application/json"):
            raise errors.validation("尺码图须为不超过 8 MB 的图片", field="body")

    raw = await request.body()
    if not raw:
        raise errors.validation("图片文件为空", field="body")
    declared_type = request.headers.get("content-type", "")
    is_json_body = declared_type.split(";", 1)[0].strip().lower() == "application/json"
    if len(raw) > image_upload.SIZE_CHART_UPLOAD_LIMIT and not is_json_body:
        raise errors.validation("尺码图须为不超过 8 MB 的图片", field="body")

    # --- 分支一：body 是 JSON，只给地址不传文件 ---
    if is_json_body:
        try:
            payload = json.loads(raw)
        except ValueError:
            raise errors.validation("请求体不是合法 JSON", field="body") from None
        if not isinstance(payload, dict) or not payload.get("url"):
            raise errors.validation("请提供图片地址（url）或上传图片文件", field="url")
        result = size_chart_service.remember_url(
            db,
            url=payload.get("url"),
            name=name or payload.get("name"),
            site=site or payload.get("site"),
        )
        db.commit()
        return JSONResponse(status_code=201, content=result)

    # --- 分支二：body 是图片字节 ---
    # 声明与真实类型必须一致（老版 upload 的拒绝点）。解析声明的活放服务层，
    # 这里只负责把 HTTP 头原样递进去。
    actual_type = size_chart_service.check_content_type(raw, declared_type)

    # 再用图床侧的类型白名单兜一道，不信任 Content-Type。
    # 判不出来要转成 400，不能让它漏成 500。
    try:
        mime, extension = image_upload.image_type(raw)
    except image_upload.ImageUploadError as exc:
        raise errors.validation(str(exc), field="body") from exc
    if not mime:
        raise errors.validation("请选择 PNG、JPEG 或 WebP 图片", field="body")

    # 名称：query 没给就从 X-File-Name 里取（前端只传了文件名作参考）
    label = name or _name_from_header(request)
    if not label:
        raise errors.validation("请填写尺寸表名称", field="name")

    # 复用临时文件把字节交给 upload_image（它按路径工作）
    with tempfile.NamedTemporaryFile(suffix="." + extension, delete=False) as tmp:
        tmp.write(raw)
    try:
        try:
            url = image_upload.upload_image(tmp.name)
        except image_upload.ImageUploadError as exc:
            raise errors.validation(str(exc), field="body") from exc
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp.name)

    result = size_chart_service.create(
        db,
        name=label,
        url=url,
        site=site,
        content_json={"mime": actual_type, "bytes": len(raw)},
    )
    db.commit()
    return result


def _name_from_header(request: Request) -> str:
    """从 X-File-Name 取默认名称（去扩展名）；没有就返回空串。

    老版前端「名称留空则用文件名」就是这条规则，新版原样保留。
    """
    from urllib.parse import unquote

    raw = unquote(request.headers.get("x-file-name", "")).strip()
    if not raw:
        return ""
    base = raw.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    stem = base.rsplit(".", 1)[0] if "." in base else base
    return stem.strip()[:128]


# 注意：/resolve 必须声明在 /{chart_id} 之前，否则会被当成 id 吃掉
@router.get("/resolve", summary="取该站点该用的尺寸表地址")
def resolve_size_chart(db: DbSession, site: str | None = None) -> dict[str, Any]:
    return size_chart_service.resolve_for_site(db, site)


@router.get("/{chart_id}", summary="尺寸表详情")
def size_chart_detail(chart_id: str, db: DbSession) -> dict[str, Any]:
    return size_chart_service.detail(db, chart_id)


# --- 写 -------------------------------------------------------------------
@router.post("", status_code=201, summary="新建尺寸表",
             dependencies=[require_permission("process")])
def create_size_chart(payload: SizeChartCreate, db: DbSession,
                      operator: OperatorDep) -> dict[str, Any]:
    require_writable(db)
    with audit_service.guard(db, operator=operator, action="system.update",
                             target_kind="size_chart",
                             target_id=str(payload.name or "")[:64]):
        result = size_chart_service.create(
            db, name=payload.name, url=payload.url, site=payload.site,
            content_json=payload.content_json)
    db.commit()
    # 尺寸表会被绑到商品上对外展示：量错一次是全店退货。
    # 挂在 system.update 而不是新造动作，是因为前端字典里没有 size_chart.*，
    # 未收录的标识会原样显示，反而更难认。
    audit_service.record(db, operator=operator, action="system.update",
                         target_kind="size_chart",
                         target_id=str((result or {}).get("id") or ""),
                         detail={"verb": "size_chart.create",
                                 "name": str(payload.name or "")[:64],
                                 "site": str(payload.site or "")[:16]})
    db.commit()
    return result


@router.patch("/{chart_id}", summary="改名 / 换图 / 换站点",
              dependencies=[require_permission("process")])
def patch_size_chart(chart_id: str, payload: SizeChartPatch, db: DbSession,
                     operator: OperatorDep) -> dict[str, Any]:
    require_writable(db)
    body = payload.model_dump(exclude_unset=True, by_alias=False)
    with audit_service.guard(db, operator=operator, action="system.update",
                             target_kind="size_chart", target_id=str(chart_id)):
        result = size_chart_service.update(db, chart_id, body)
    db.commit()
    audit_service.record(db, operator=operator, action="system.update",
                         target_kind="size_chart", target_id=str(chart_id),
                         detail={"verb": "size_chart.patch",
                                 "fields": sorted(body)})
    db.commit()
    return result


@router.delete("/{chart_id}", summary="删除尺寸表（只忘书签，图片保留）",
               dependencies=[require_permission("process")])
def delete_size_chart(chart_id: str, db: DbSession,
                      operator: OperatorDep) -> dict[str, Any]:
    require_writable(db)
    with audit_service.guard(db, operator=operator, action="system.update",
                             target_kind="size_chart", target_id=str(chart_id)):
        result = size_chart_service.delete(db, chart_id)
    db.commit()
    audit_service.record(db, operator=operator, action="system.update",
                         target_kind="size_chart", target_id=str(chart_id),
                         detail={"verb": "size_chart.delete"})
    db.commit()
    return result


# 老版删除接口的入参是 `{"url": ...}`，没有 id（前端尺码图库就是拿地址删的）。
# 单独开一条 DELETE /size-charts（无路径段）承接这个形态：放成 /{chart_id}
# 的 body 分支会跟「id 必须是数字」的校验打架，两条路径各自干净。
@router.delete("", summary="按地址删除尺寸表（老版 delete-size-chart 形态）",
               dependencies=[require_permission("process")])
async def delete_size_chart_by_url(request: Request, db: DbSession,
                                   operator: OperatorDep) -> dict[str, Any]:
    require_writable(db)
    payload: Any = None
    # 地址也可能是 query（老版 /api/delete-size-chart?url=... 的用法），一并认
    url = request.query_params.get("url")
    if not url:
        raw = await request.body()
        if raw:
            try:
                payload = json.loads(raw)
            except ValueError:
                raise errors.validation("请求体不是合法 JSON", field="url") from None
            if isinstance(payload, dict):
                url = payload.get("url")
    if not url:
        raise errors.validation("请选择要删除的尺码图", field="url")
    with audit_service.guard(db, operator=operator, action="system.update",
                             target_kind="size_chart", target_id=str(url)[:64]):
        result = size_chart_service.delete_by_url(db, url)
    db.commit()
    audit_service.record(db, operator=operator, action="system.update",
                         target_kind="size_chart", target_id=str(url)[:64],
                         detail={"verb": "size_chart.delete_by_url"})
    db.commit()
    return result
