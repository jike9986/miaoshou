"""图片翻译路由。"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import FileResponse

from api.deps import (
    CredentialsHeader,
    DbSession,
    OperatorDep,
    request_id_of,
    require_permission,
    require_writable,
)
from api.helpers import parse_bool
from core import errors
from core.config import get_settings
from core.utils import redact
from schemas.common import CamelModel
from schemas.requests import ImageTranslateRequest
from services import (
    audit_service,
    image_review_service,
    image_service,
    operator_service,
    setting_service,
)

router = APIRouter(prefix="/images", tags=["图片翻译"])

logger = logging.getLogger(__name__)

_MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
         "webp": "image/webp", "gif": "image/gif"}


@router.post("/analyze", summary="主图文字识别（本地 OCR，判断图上有没有中文）")
def analyze_images(payload: dict[str, Any], db: DbSession) -> dict[str, Any]:
    """选主图前先看图上有没有中文；只支持阿里 CDN 原图。

    请求：{"images": [url, ...], "retry": false, "limit": 20}
    """
    images = payload.get("images")
    if not isinstance(images, list) or not images:
        raise errors.validation("images 必须是非空数组", field="images")
    if len(images) > image_review_service.ANALYZE_LIMIT:
        raise errors.validation(
            f"一次最多识别 {image_review_service.ANALYZE_LIMIT} 张图",
            field="images", value=len(images))
    rows = image_review_service.analyze(
        images,
        retry=payload.get("retry") is True,
        limit=int(payload.get("limit") or image_review_service.ANALYZE_LIMIT),
    )
    return {"items": rows, "summary": image_review_service.summarize(rows)}


class ImageReviewGateRequest(CamelModel):
    """主图复核闸门的入参：给 warehouseId，或 detailId + shopId。

    与 `POST /warehouse/pre-save-gate` 的区别是这里**只跑主图 OCR**：
    前端在图片页单独复看主图时不该顺带触发标题词库判定。
    """

    warehouseId: str | None = None
    detailId: str | None = None
    shopId: str | None = None
    allowReview: bool = False
    retry: bool = False


@router.post("/review-gate", summary="主图 OCR 复核闸门（只读：不写库、不删图）")
def review_gate(payload: ImageReviewGateRequest, db: DbSession,
                credentials: CredentialsHeader = None) -> dict[str, Any]:
    """老版 `shoe_workflow.review_main_images` 的判定，只回答不执行。

    返回 {blocked, removeIndexes, needsReview, message, ...}：
      · 前 9 张主图跑 OCR，行数对不上 / 任一行 error / 任一行 action=="review"
        → blocked=true（除非 allowReview）；
      · action=="remove" 的工厂图进 removeIndexes，但**不在这里删**——
        删图是入仓流程的事，本接口只给结论；
      · 剔完一张不剩 → blocked=true，且不受 allowReview 影响（老版无条件抛错）。

    不写任何数据：OCR 缓存由 image_review_service 按 URL 自己管，这里不落库。
    """
    from services import gate_service

    info, identity = gate_service.load_info(
        db, warehouse_id=payload.warehouseId,
        detail_id=payload.detailId, shop_id=payload.shopId)
    result = gate_service.main_image_gate(
        info, allow_review=payload.allowReview is True, retry=payload.retry is True)
    # rows 是逐图的 OCR 明细，前端图片页要看；调用方不需要时忽略即可
    return {**result, "identity": identity}



@router.post("/translate", status_code=202, summary="批量提交图片翻译（幂等 + 预算）",
             dependencies=[require_permission("images")])
def translate(payload: ImageTranslateRequest, db: DbSession,
              operator: OperatorDep) -> dict[str, Any]:
    require_writable(db)
    body = payload.model_dump(by_alias=False, exclude_none=True)
    # 图片翻译是**按张扣费**的动作：谁提交的必须留痕，否则月底对账时
    # 「这两千张是谁翻的」只能靠猜。
    #
    # target 先用入参里的 batchId（可能为空——不带批次时服务层会自己生成一个），
    # 失败记录里也必须有它，否则那条 failed 就不知道是哪一批挂的。
    batch_key = str(payload.batchId or "")
    with audit_service.guard(db, operator=operator, action="image.translate",
                             target_kind="image", target_id=batch_key):
        result = image_service.translate(
            db, body, operator_id=getattr(operator, "id", None),
            owner_operator_id=(operator.id
                               if operator_service.role_of(operator)
                               == operator_service.ROLE_MEMBER else None))
    db.commit()
    audit_service.record(db, operator=operator, action="image.translate",
                         target_kind="image",
                         # 成功时才拿得到真实批次号（服务层可能新生成）
                         target_id=str(result.get("batchId") or batch_key),
                         detail={"requested": len(payload.items or []),
                                 "accepted": result.get("accepted") or 0,
                                 "reused": result.get("reused") or 0,
                                 "reservedMilliyuan": (
                                     result.get("budget") or {}).get(
                                         "reservedMilliyuan") or 0})
    db.commit()
    return result


@router.get("/jobs", summary="任务列表")
def list_jobs(
    db: DbSession,
    operator: OperatorDep,
    batchId: str | None = None,
    productId: str | None = None,
    status: str | None = None,
    page: int = Query(default=1, ge=1),
    pageSize: int = Query(default=50, ge=1, le=100),
) -> dict[str, Any]:
    owner_id = (operator.id if operator_service.role_of(operator)
                == operator_service.ROLE_MEMBER else None)
    return image_service.list_jobs(db, batch_id=batchId, product_id=productId,
                                   status=status, page=page, page_size=pageSize,
                                   operator_id=owner_id)


def _require_job_access(job_id: str, db: DbSession, operator: OperatorDep):
    job = image_service.get_job(db, job_id)
    if (operator_service.role_of(operator) == operator_service.ROLE_MEMBER
            and job.operator_id != operator.id):
        raise errors.not_found(f"图片任务 {job_id} 不存在", id=str(job_id))
    return job


@router.get("/jobs/{job_id}", summary="单个任务（refresh=true 时向上游查询）")
def get_job(job_id: str, db: DbSession,
    credentials: CredentialsHeader = None, refresh: str | None = None,
    operator: OperatorDep = None) -> dict[str, Any]:
    should_refresh = parse_bool(refresh, "refresh", False)
    if should_refresh:
        operator_service.require_role(operator, "images")
    job = _require_job_access(job_id, db, operator)
    if should_refresh:
        result = image_service.refresh(db, job_id)
        db.commit()
        return result
    return {"job": image_service.serialize(job),
            "progress": image_service._progress(job)}


@router.get("/jobs/{job_id}/file", summary="结果图（inline）")
def get_job_file(job_id: str, db: DbSession,
                 operator: OperatorDep) -> Response:
    _require_job_access(job_id, db, operator)
    path, filename = image_service.result_file(db, job_id)
    extension = filename.rsplit(".", 1)[-1].lower()
    return FileResponse(path, media_type=_MIME.get(extension, "application/octet-stream"),
                        headers={"Content-Disposition": f'inline; filename="{filename}"'})


@router.post("/jobs/{job_id}/upload", summary="把结果图传到图床，拿公网地址",
             dependencies=[require_permission("images")])
def upload_job(job_id: str, db: DbSession,
               operator: OperatorDep) -> dict[str, Any]:
    """补传公网地址。

    自动接线（translate / refresh / reconcile）已经在结果就绪时传过一次；
    这里给「当时上传失败」和「历史任务没有公网地址」两条路留一个手动重试口。
    幂等：已有 public_url 直接回现有值，不重复走 SFTP/HTTP。
    """
    require_writable(db)
    with audit_service.guard(db, operator=operator, action="image.upload",
                             target_kind="image", target_id=str(job_id)[:64]):
        result = image_service.upload_job_result(db, job_id)
    db.commit()
    audit_service.record(db, operator=operator, action="image.upload",
                         target_kind="image", target_id=str(job_id)[:64],
                         # 公网地址进审计：以后要查「这张图当时传到了哪」只有这里
                         detail={"publicUrl": str(
                             (result.get("job") or {}).get("publicUrl") or "")[:200]})
    db.commit()
    return result


@router.get("/usage", summary="限流与预算状态")
def usage(db: DbSession) -> dict[str, Any]:
    return image_service.usage(db)


@router.post("/reconcile", summary="进程重启后对账（绝不重复提交）",
             dependencies=[require_permission("images")])
def reconcile(db: DbSession,
    operator: OperatorDep,
    credentials: CredentialsHeader = None, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    require_writable(db)
    job_ids = None
    if isinstance(payload, dict):
        raw = payload.get("jobIds") or payload.get("job_ids")
        if isinstance(raw, list):
            job_ids = [str(item) for item in raw]
    with audit_service.guard(db, operator=operator, action="image.reconcile",
                             target_kind="image"):
        result = image_service.reconcile(db, job_ids)
    db.commit()
    audit_service.record(db, operator=operator, action="image.reconcile",
                         target_kind="image",
                         # reconcile 的返回是 checked/ready/pending/failed
                         detail={"checked": result.get("checked") or 0,
                                 "ready": result.get("ready") or 0,
                                 "pending": result.get("pending") or 0,
                                 "failed": result.get("failed") or 0})
    db.commit()
    return result


@router.get("/limits", summary="图片预算/限流配置")
def limits(db: DbSession) -> dict[str, Any]:
    return {"limits": setting_service.image_limits(db), "usage": image_service.usage(db)}


@router.post("/limits", summary="调整图片限流额度",
             dependencies=[require_permission("admin")])
def set_limits(payload: dict[str, Any], db: DbSession,
               operator: OperatorDep) -> dict[str, Any]:
    """改每分钟 / 每小时 / 每天张数与每日预算。

    老版可以用 TK_IMAGE_RATE_* 环境变量临时放宽，新版此前这个键只被 seed
    写过、没有任何 API 能改，限流值事实上被锁死——只能改库或改代码。
    """
    require_writable(db)
    updated = setting_service.set_image_limits(db, payload)
    db.commit()
    # 把限流额度放宽一倍，等于让**所有人**一天能多烧一倍的钱。
    # 这是共享设置里最该查「谁改的」的一条。
    audit_service.record(db, operator=operator, action="settings.change",
                         target_kind="setting", target_id="imageLimits",
                         detail={"name": "图片限流与预算",
                                 **{k: v for k, v in (updated or {}).items()
                                    if not isinstance(v, (list, dict))}})
    db.commit()
    return {"limits": updated, "usage": image_service.usage(db)}


# --- 密钥验证 -------------------------------------------------------------
# 老版有个 verify_image_api.py：带 --confirm 跑一次付费单图，验端到端链路。
# 新版把这条入口丢了——api/images.py 里 9 条路由没有一条能验密钥，用户配错
# image-key.txt 只能等批量翻译提交时才在某个 job.message 里看到 503/401。
# 这里补一对路由：GET 只查配置（免费、随便打），POST 才是那次付费冒烟。

def _key_hint(key: str) -> str:
    """只给前后各 4 位。完整密钥一旦进了响应体，就会进日志、进浏览器缓存、
    进用户截图，等于把密钥泄漏出去，而这本来只是「确认配没配」的接口。"""
    if not key:
        return ""
    if len(key) < 12:
        # 太短就不露头尾了：短密钥露 8 位基本等于露全部
        return "*" * len(key)
    return f"{key[:4]}...{key[-4:]}"


def _missing_key_message(mode: str) -> str:
    """未配置时的可行动说明：去哪放文件、文件叫什么。"""
    settings = get_settings()
    target = settings.image_key_path
    legacy_dir = settings.legacy_image_key_dir()
    if mode == "proxy":
        legacy = "image-license.txt（许可码）"
        tip = "当前配了图片代理（AOHUANG_IMAGE_PROXY），这里要写代理方发的许可码。"
    else:
        legacy = "codesonline-key.txt（服务商密钥）或 image-license.txt"
        tip = "当前是官方端点直连，这里要写服务商密钥。"
    return (
        f"图片翻译密钥未配置。{tip}"
        f"任选其一：① 把密钥写入 {target}（一行纯文本，UTF-8）；"
        f"② 设置环境变量 AOHUANG_IMAGE_KEY 后重启；"
        f"③ 沿用老版位置 {legacy_dir}\\{legacy}。"
        f"配好后打开本接口应看到 configured=true，再打 POST 做一次付费验证。"
    )


@router.get("/verify", summary="检查图片翻译密钥是否配置（不发起付费请求）")
def verify_key(db: DbSession) -> dict[str, Any]:
    """只读检查：密钥从哪儿来、当前是直连还是代理、能否被读到。

    刻意不调用 image_service._api_key()：它在未配置时抛 503，而这里恰恰需要
    在未配置时也正常返回一份「该怎么配」的说明，让用户不用提交一次批量任务
    才发现问题。
    """
    settings = get_settings()
    origin = settings.image_key_origin()
    key = settings.image_api_key()
    mode = origin.get("mode") or ("proxy" if (settings.image_proxy or "").strip()
                                 else "direct")
    if not key:
        return {
            "configured": False,
            "mode": "",
            "keyHint": "",
            "source": "",
            "path": "",
            "endpoint": settings.edits_endpoint(),
            "message": _missing_key_message(mode),
        }
    source = str(origin.get("source") or "")
    return {
        "configured": True,
        "mode": mode,
        "keyHint": _key_hint(key),
        "source": source,
        "path": str(origin.get("path") or ""),
        "endpoint": settings.edits_endpoint(),
        "message": f"密钥已配置（来源：{source}），"
                   f"当前{'代理' if mode == 'proxy' else '直连'}模式。"
                   "如需确认端到端可用，请用 POST /images/verify 发一次付费测试。",
    }


# 冒烟图边长：够上游识别成一张正常商品图，又不至于上传几兆。
_SMOKE_SIDE = 64


def _smoke_png() -> bytes:
    """现造一张极小的 PNG 当冒烟素材。

    不依赖 Pillow（打包环境里它可能没装），也不用 1x1 那种像素——太小容易被
    上游的尺寸校验直接拒掉，那就验不出密钥到底通不通了。zlib + 手写 PNG 头
    一共二十来行，比引入一个可选依赖稳。
    """
    import struct
    import zlib

    width = height = _SMOKE_SIDE
    # 每行以 filter 字节 0 开头，后面 RGB 三通道；纯色块，压缩后只有几百字节
    row = b"\x00" + bytes((0xF4, 0xF7, 0xFA)) * width
    raw = row * height

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


@router.post("/verify", summary="真发一次最小付费请求验证密钥（需 confirm）",
             dependencies=[require_permission("admin")])
def verify_key_live(request: Request, db: DbSession, operator: OperatorDep,
                    payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """付费冒烟：发一张 64x64 的纯色图，确认密钥能真的打到上游。

    这条路径会扣一次费，所以两道闸：必须显式 confirm: true（跟发布一样，
    不确认就给 400），并且必须可写（只读模式下不允许产生费用）。
    验证请求必须能在日志里认出来——排查「谁在扣费」时，只有审计日志能回答。
    所以这条路径也写审计（action=image.verify），detail 里只记状态与提示，
    **不记密钥**（keyHint 都不记：4 位前后缀配上已知前缀就是半个密钥）。
    """
    require_writable(db)
    if not isinstance(payload, dict) or payload.get("confirm") is not True:
        raise errors.AppError(
            errors.CONFIRM_REQUIRED,
            "这是付费测试：会真实提交一张图片并可能产生费用，请求体必须包含 "
            "\"confirm\": true",
            status=400,
            details={"paid": True, "unitMilliyuan": image_service.UNIT_MILLIYUAN},
        )

    request_id = request_id_of(request)
    settings = get_settings()
    key = settings.image_api_key()
    logger.info("图片密钥验证请求（付费冒烟）requestId=%s mode=%s source=%s keyHint=%s",
                request_id,
                "proxy" if (settings.image_proxy or "").strip() else "direct",
                settings.image_key_origin().get("source") or "(未配置)",
                _key_hint(key) or "(空)")
    if not key:
        # 未配置时不该走到上游再报一个看不懂的连接错误
        raise errors.AppError(
            "IMAGE_KEY_MISSING",
            _missing_key_message("proxy" if (settings.image_proxy or "").strip() else "direct"),
            status=503,
        )

    payload_out: dict[str, Any] = {
        "ok": False,
        "status": "unknown",
        "message": "",
        "reservedMilliyuan": 0,
        "keyHint": _key_hint(key),
        "endpoint": settings.edits_endpoint(),
        "requestId": request_id,
    }
    try:
        outcome = image_service._submit(_smoke_png(), "1:1",
                                       prompt=image_service.TRANSLATION_PROMPT)
    except errors.AppError as exc:
        # _submit 已经抹过密钥再抛；这里再抹一遍是给「以后有人改 _submit」
        # 留一道兜底——验证接口的响应最容易被截图外发。
        safe = redact(exc.message, [key, f"Bearer {key}"])[:300]
        payload_out["status"] = ("unknown"
                                 if exc.code == errors.UPSTREAM_UNKNOWN_RESULT
                                 else "failed")
        payload_out["message"] = f"验证失败（{exc.code}）：{safe}"
        payload_out["reservedMilliyuan"] = image_service.UNIT_MILLIYUAN
        logger.warning("图片密钥验证失败 requestId=%s code=%s：%s",
                       request_id, exc.code, safe)
        _audit_verify(db, operator, request_id, payload_out, code=exc.code)
        return payload_out
    except Exception as exc:  # noqa: BLE001 - 验证接口不能把堆栈抛给用户
        payload_out["status"] = "unknown"
        payload_out["message"] = f"验证异常（{type(exc).__name__}），结果未确认"
        payload_out["reservedMilliyuan"] = image_service.UNIT_MILLIYUAN
        logger.exception("图片密钥验证异常 requestId=%s", request_id)
        _audit_verify(db, operator, request_id, payload_out,
                      code=type(exc).__name__)
        return payload_out

    payload_out["ok"] = True
    payload_out["status"] = str(outcome.get("status") or "submitted")
    payload_out["taskId"] = str(outcome.get("taskId") or "")
    payload_out["reservedMilliyuan"] = image_service.UNIT_MILLIYUAN
    payload_out["message"] = (
        "验证通过：上游接受了这张测试图，密钥可用。"
        if payload_out["status"] == "ready"
        else "验证通过：上游已受理（异步任务），密钥可用。"
    )
    logger.info("图片密钥验证通过 requestId=%s status=%s taskId=%s",
                request_id, payload_out["status"], payload_out["taskId"] or "-")
    _audit_verify(db, operator, request_id, payload_out)
    return payload_out


def _audit_verify(db: DbSession, operator: Any, request_id: str,
                  payload_out: dict[str, Any], *, code: str = "") -> None:
    """把一次付费冒烟记进审计。

    不写 keyHint：4 位前缀 + 4 位后缀配上已知的服务商前缀，等于半个密钥，
    而审计表是全员可见的页面。
    """
    audit_service.record(
        db, operator=operator, action="image.verify", target_kind="image",
        target_id=request_id,
        result="failed" if code else "success",
        message=(f"{code}" if code else ""),
        detail={"status": payload_out.get("status") or "",
                "ok": bool(payload_out.get("ok")),
                "taskId": str(payload_out.get("taskId") or "")[:64],
                "reservedMilliyuan": payload_out.get("reservedMilliyuan") or 0})
    db.commit()
