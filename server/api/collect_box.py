"""采集箱路由：分页查询、单件详情、认领。"""
from __future__ import annotations

import logging
import secrets
import threading
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from api.deps import CredentialsHeader, DbSession, OperatorDep, require_permission, require_writable
from core import errors
from core.utils import now_ts, product_key
from schemas.requests import (
    AssignSiteItemsRequest,
    BatchClaimRequest,
    ClaimRequest,
    ProcessRequest,
)
from services import account_service, audit_service, collect_box_service

logger = logging.getLogger("aohuang.api")

router = APIRouter(prefix="/collect-box", tags=["采集箱"])

# 图片后台线程「写回妙手」的并发信号量。
#
# ⚠️ 这管的是**翻译完写回**，不是发出去翻译（那是
# `image_service.UPSTREAM_IMAGE_CONCURRENCY=500` / 每学员 50）。
# 两件事完全不同，别混。
#
# 负责人口径（2026-11-21）：
#   「老师图片翻译完成写回来是最高 5 个，学员都是默认 1 个」
#
# 所以上限**按身份**算，不再由前端传的 itemConcurrency 决定：
#   · 老师 / 管理员 → 5
#   · 学员           → 1
# 前端那次的 itemConcurrency 只作为「本批次想并行几件」的意愿值，
# 还要受身份上限约束（取两者较小），防止学员传 5 把上游写回打爆。
_image_semaphores: dict[int, threading.Semaphore] = {}
_image_semaphore_lock = threading.Lock()
# 每个闸门当前的上限（Python 3.10 的 Semaphore 不暴露该值，自己记）
_image_semaphore_limits: dict[int, int] = {}

# 启动恢复时一次最多拉起几个卡住的图片任务（2026-11-28）。
#
# 原来不限量，8 个 queued 就 8 个一起跑，内存冲顶 OOM，重启后又全拉起来，
# 死循环。改成一次只拉 2 个：图片翻译每件要下载/OCR/翻译十几到几十张图，
# 2 件已经是这台 8 GB 机器的安全上限（实测 8 件并行时 anon-rss 6.5 GB 被杀）。
# 剩下的等它们跑完由 `_pump_stuck_image_jobs()` 续上。
IMAGE_RESUME_BATCH = 2

# 全服务器同时运行的图片翻译任务数上限（2026-11-28 修 OOM）。
#
# 三次 OOM 的 anon-rss 都是 6.5 GB（12:27 / 12:53 / 14:23），说明不是偶发，
# 是稳定的并发超载：每件商品的图片任务都要把十几到几十张图下载进内存、
# 转 numpy 跑 OCR、再提交翻译。20 件同时跑，无论单件内部怎么限流，
# 总量都会把 8 GB 机器撑爆。
#
# 2 是这台机器的安全值（实测跑 2 件时内存稳定在 3.7 GB 左右，
# 跑 8 件时 6.5 GB 被杀）。其余任务排队等前面的跑完。
_GLOBAL_IMAGE_TASK_SLOTS = threading.BoundedSemaphore(2)

# 全服务器同时处理的商品件数上限（2026-11-28 修 OOM）。
#
# 每件商品在 `/process` 里要：拉详情 → 下载几十张图转存 → OCR → 提交翻译。
# 20 件同时进来时，单是图片转存就把 8 GB 机器吃到 6.5 GB 被内核杀掉
# （实测 12:27 / 12:53 / 14:23 三次，anon-rss 恒为 6.5 GB）。
# 排队等前面的处理完，比服务崩溃让整批商品重来划算得多。
#
# 3 是实测安全值：本机 4 核 8 GB，跑 3 件时内存稳定在 4 GB 上下。
_PROCESS_SLOTS = threading.BoundedSemaphore(3)


def _limited_process_slot(func):
    """给 `/process` 套一层全服务器并发闸门。"""
    import functools

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with _PROCESS_SLOTS:
            return func(*args, **kwargs)

    return wrapper


def _acquire_global_image_slot(timeout: float = 1800.0) -> bool:
    """拿一个全局图片任务许可；超时返回 False（放弃这一件，不阻塞线程池）。"""
    return _GLOBAL_IMAGE_TASK_SLOTS.acquire(timeout=timeout)


def _release_global_image_slot() -> None:
    try:
        _GLOBAL_IMAGE_TASK_SLOTS.release()
    except ValueError:  # 已经释放过
        pass


def _writeback_limit(concurrency: int | None, *, privileged: bool) -> int:
    """本批次写回妙手最多并行几件。

    身份上限优先（老师 5 / 学员 1，负责人定的），批次意愿值只能在
    上限之内下调，不能超过。
    """
    from services import image_service

    cap = (image_service.TEACHER_WRITEBACK_CONCURRENCY if privileged
           else image_service.MEMBER_WRITEBACK_CONCURRENCY)
    # 0 或空 = 不指定意愿，直接用身份上限。
    # 不能写成 `int(concurrency or cap)` —— 传 0 时会退成 1，把老师从 5
    # 错降到 1（重启恢复路径就是这条，2026-11-21 踩过）。
    raw = int(concurrency or 0)
    if raw <= 0:
        return cap
    return max(1, min(cap, raw))


def _reset_image_semaphore(concurrency: int, *, privileged: bool = False,
                           key: int | None = None) -> int:
    """为这一批写回线程设定并发上限，返回实际生效值。

    ## 为什么同名但不再"重置"（2026-11-28 修 OOM 死循环）

    原来是**每次都新建一个 Semaphore 覆盖掉旧的**：

        _image_semaphores[key] = threading.Semaphore(limit)

    可 `/process` 是**每件商品一次请求**，20 件就是 20 次调用。每次都把
    闸门换成新的，等于把正在跑的许可全扔掉重发 —— 并发瞬间失控。

    实测 14:23 服务 OOM（第三次，anon-rss 恒定 6.5 GB）被杀，
    负责人那批 20 件全报「处理过程中断（服务重启）」。
    20 件同时各拿 5 个许可 = 100 个图片任务并行下载/OCR/翻译。

    现在语义改成「首次创建，之后只放宽不放窄」：
      · 闸门已存在且新上限不大于旧值 → 原样保留（绝不覆盖正在跑的许可）；
      · 身份从学员升到老师（1 → 5）时才重建。

    上限自己记在 `_image_semaphore_limits` 里 —— Python 3.10 的
    `threading.Semaphore` 没有 `_initial_value` 属性（3.11 才有），
    直接读会 AttributeError。
    """
    limit = _writeback_limit(concurrency, privileged=privileged)
    slot_key = int(key or 0)
    with _image_semaphore_lock:
        existing = _image_semaphores.get(slot_key)
        current = _image_semaphore_limits.get(slot_key)
        if existing is None or current is None or limit > current:
            _image_semaphores[slot_key] = threading.Semaphore(limit)
            _image_semaphore_limits[slot_key] = limit
            return limit
        return current


def _image_semaphore_for(key: int | None) -> threading.Semaphore:
    with _image_semaphore_lock:
        return _image_semaphores.setdefault(int(key or 0), threading.Semaphore(1))


@router.get("", summary="分页查询采集箱（扫描即查询）")
def list_collect_box(
    db: DbSession,
    page: int = Query(default=1, ge=1),
    pageSize: int = Query(default=20, ge=1, le=100),
    status: str = "notPublished",
    keyword: str = "",
    shopId: str | None = None,
    site: str | None = None,
    sort: str = "default",
    refresh: bool = False,
    accountId: str | None = None,
    credentials: CredentialsHeader = None,
) -> dict[str, Any]:
    account = account_service.resolve_account(db, accountId, credentials)
    return collect_box_service.list_items(
        db, account, page=page, page_size=pageSize, status=status,
        keyword=keyword or "", shop_id=shopId, site=site, sort=sort,
        force_refresh=refresh)


@router.post("/site/assign", summary="把采集箱里所有未分配店铺的商品认领到目标店铺",
             dependencies=[require_permission("process")])
def assign_site_items(
    payload: AssignSiteItemsRequest,
    db: DbSession,
    operator: OperatorDep,
    credentials: CredentialsHeader = None,
    accountId: str | None = None,
) -> dict[str, Any]:
    """把 site 模式（还没分配店铺）的商品批量认领到目标店铺。

    ## 负责人要求（2026-11-21）

    「打开这个软件如果有未分配的自动分配，在还没有开始自动化处理就把这些
    产品全部自动弄好」

    也就是软件启动时先把「没店」的商品一次分好，用户打开采集箱看到的
    就是全部可处理状态，而不是等点了「开始自动化」才逐件认领。

    ## 为什么不在 GET /collect-box 里做

    列表是**读**接口，会在用户翻页、刷新、切筛选时反复调用。在里面写
    上游数据（认领是不可撤销的）会让一次普通浏览变成一连串写操作 ——
    用户翻个页就分了几十件，还没想好要哪家店。

    所以做成独立的 POST：前端在**软件启动（连接成功）后**主动调用一次。

    ## 幂等

    已经分好店的商品不在 site 列表里，天然跳过。重复调用只会返回
    `total=0`，不会重复打上游。
    """
    require_writable(db)
    account = account_service.resolve_account(db, payload.accountId or accountId, credentials)
    from services import collect_box_service

    target_shop = str(payload.targetShopId or "").strip()
    if not target_shop:
        raise errors.validation("请选择默认店铺", field="targetShopId")

    result = collect_box_service.claim_site_batch(
        db, account, target_shop_id=target_shop,
        max_items=int(payload.maxItems or 200))
    if result["claimed"]:
        audit_service.record(
            db, operator=operator, action="warehouse.claim", target_kind="product",
            target_id=f"site-batch:{target_shop}",
            account_id=account.id if account else None, shop_id=target_shop,
            detail={"claimed": result["claimed"], "failed": result["failed"],
                    "total": result["total"]})
        db.commit()
    return result


@router.get("/{detail_id}", summary="采集箱单件详情（含 ossMd5 / SKU / 诊断）")
def collect_box_detail(
    detail_id: str,
    db: DbSession,
    shopId: str = Query(description="明细只在店铺维度唯一"),
    mode: str = "shop",
    accountId: str | None = None,
    credentials: CredentialsHeader = None,
) -> dict[str, Any]:
    account = account_service.resolve_account(db, accountId, credentials)
    if not str(shopId or "").strip():
        raise errors.validation("缺少 shopId 参数", field="shopId")
    return collect_box_service.detail(db, account, detail_id=detail_id,
                                      shop_id=shopId, mode=mode)


@router.post("/{detail_id}/claim", summary="认领进目标店铺采集箱（不发布）",
             dependencies=[require_permission("claim")])
def claim(
    detail_id: str,
    payload: ClaimRequest,
    db: DbSession,
    operator: OperatorDep,
    shopId: str = Query(description="来源店铺"),
    credentials: CredentialsHeader = None,
) -> dict[str, Any]:
    require_writable(db)
    account = account_service.resolve_account(db, payload.accountId, credentials)
    if not str(shopId or "").strip():
        raise errors.validation("缺少 shopId 参数", field="shopId")
    key = product_key(detail_id, shopId)
    with audit_service.guard(db, operator=operator, action="warehouse.claim",
                             target_kind="product", target_id=key,
                             account_id=account.id if account else None,
                             shop_id=shopId):
        result = collect_box_service.claim(
            db, account, detail_id=detail_id, shop_id=shopId,
            target_shop_id=payload.targetShopId,
            mark_collected=payload.markCollected)
    audit_service.record(
        db, operator=operator, action="warehouse.claim", target_kind="product",
        target_id=key, account_id=account.id if account else None, shop_id=shopId,
        detail={"targetShopId": payload.targetShopId or "",
                "claimed": bool(result.get("claimed"))})
    db.commit()
    return result


@router.post("/{detail_id}/process", summary="处理一件商品并入仓（拉详情→定价→入库）",
             dependencies=[require_permission("automation")])
@_limited_process_slot
def process(
    detail_id: str,
    payload: ProcessRequest,
    db: DbSession,
    operator: OperatorDep,
    credentials: CredentialsHeader = None,
    accountId: str | None = None,
) -> dict[str, Any]:
    """拉详情 → 定价 → 入仓（可选连带图片翻译）。

    这是「上品」最核心的一步：一件商品从采集箱进到仓库，就是这条路径干的。
    `save=false` 是试算（会 rollback），不算写过东西，不记审计；
    `save=true` 落库了才记——否则审计表会被「我点了一百次试算」淹掉。
    """
    require_writable(db)
    account = account_service.resolve_account(db, payload.accountId or accountId, credentials)
    from services import warehouse_service
    from services.account_service import account_client, map_upstream_error
    from services.miaoshou import MiaoshouError

    # --- site 模式商品：先自动认领到目标店铺（2026-11-21）-----------------
    #
    # 负责人要求：「自动化处理不能默认给他选择一个店铺吗，加到文字处理里面」
    #
    # 背景：妙手采集箱的商品有两种形态（详见 collect_box_service.listing_rows）：
    #   shop —— 已分配到店铺，行里有 shopId
    #   site —— 只采集到采集箱、还没分配店铺，shopId 是 None/空
    #
    # 原来的实现里 site 商品根本进不了处理流程：这边 `payload.shopId` 必填，
    # 用户只能先去妙手界面手动分配到店铺。
    #
    # 现在：如果调用方给了 targetShopId（处理设置里选的店），而 shopId 为空，
    # 就先 claim_to_shop 把它认领过去，拿到真实 shopId 再继续。
    # 实测有效（账号 8）：
    #   认领前 editModel=site shopId=''         → claim_to_shop
    #   认领后 editModel=shop shopId=19974446
    resolved_shop_id = str(payload.shopId or "").strip()
    if not resolved_shop_id:
        target_shop = str(getattr(payload, "targetShopId", "") or "").strip()
        if not target_shop:
            raise errors.validation(
                "这件商品还没分配到店铺。请在处理设置里选择「默认店铺」，"
                "或先去妙手采集箱把它分配到店铺。",
                field="shopId")
        from services import collect_box_service
        resolved_shop_id = collect_box_service.claim_site_item(
            db, account, detail_id=detail_id, target_shop_id=target_shop)

    key = product_key(detail_id, resolved_shop_id)
    client = account_client(account)
    try:
        with audit_service.guard(db, operator=operator, action="warehouse.import",
                                 target_kind="product", target_id=key,
                                 account_id=account.id if account else None,
                                 shop_id=resolved_shop_id):
            product = warehouse_service.save_from_collect_box(
                db, account, client, detail_id=detail_id, shop_id=resolved_shop_id,
                language=payload.language or "", group_id=payload.groupId,
                price_template=payload.template, exchange_rate=payload.exchangeRate,
                enforce_pre_save=bool(payload.save),
                allow_title_review=bool(payload.allowTitleReview),
                allow_image_review=bool(payload.allowImageReview),
                skip_image_review=bool(payload.skipImageReview),
                operator_id=getattr(operator, "id", None),
                automation_result=True,
                # 处理设置里的数量 = 保留几张（SKU 图数量 = 颜色数），
                # 主流程按它真正截断主图/详情图/颜色。
                image_limits={
                    "main": int(payload.mainImageCount or 0),
                    "sku": int(payload.skuImageCount or 0),
                    "detail": int(payload.detailImageCount or 0),
                },
                # 货源链接只能从**公共采集箱**取（2026-11-21 实测）：
                # 店铺侧 26 个字段里没有任何 1688 信息，
                # 而 commonCollectBoxDetailId 只在采集箱列表行里。
                # 调用方（前端 process 请求）把它带过来。
                common_detail_id=payload.commonCollectBoxDetailId)
    except MiaoshouError as exc:
        raise map_upstream_error(exc) from None
    finally:
        client.close()
    image_result: dict[str, Any] | None = None
    bg_image_job: dict[str, Any] | None = None
    # 图片处理过没有（2026-11-27）。负责人口径：「一个商品是分图片和文字处理，
    # 哪个处理过了就里面打标记，下次处理就会跳过」。
    #
    # 图片按张计费（一张 24 毫 = 0.024 元），且重复提交会把已经译好的图再翻一遍。
    # 标记在 `_run_image_job` 成功收尾时打上（见下方 mark_image_done）。
    images_done = False
    if payload.processImages and payload.save:
        from models import Product  # noqa: F401 - 下面只有 images_done 分支之外才用

        from services import draft_mark_service
        _want_total = (int(payload.mainImageCount or 0)
                       + int(payload.skuImageCount or 0)
                       + int(payload.detailImageCount or 0))
        images_done = draft_mark_service.image_already_done(
            db, account_id=int(account.id), detail_id=str(detail_id),
            shop_id=str(resolved_shop_id),
            # 张数一致才算数：商品换图后必须重翻，不能拿旧标记挡路。
            expected_total=_want_total if _want_total else None)
        if images_done:
            # 跳过图片流程。**不要动 draft_verified_revision**：
            # 那个字段是「草稿读回核验通过」的凭证，跟图片跑没跑是两件事。
            # 这里若置 None，商品会立刻从「自动化结果」页消失
            # （list_automation_results 要求 verified == revision），
            # 等于把一件原本好的商品弄丢。
            logger.info("图片已处理过，跳过图片翻译 detailId=%s", detail_id)
            image_result = {"queued": False, "completed": True, "skipped": True,
                            "reason": "该商品图片已处理过，本次跳过",
                            "requested": 0, "submitted": 0, "failed": 0, "jobs": []}

    if payload.processImages and payload.save and not images_done:
        # 图片翻译整体放后台：文字/价格/入仓几秒就完成，用户应当立刻看到结果；
        # 图片翻译每张 AI 20-30 秒、十几张要几分钟，绝不能卡在同步请求里让界面
        # 一直「处理中 0%」。这里只记下商品 id 与各环节上限，交给后台线程跑
        # 「本地 OCR 筛选 → 去重 → 提交翻译 → 写回译文」。前端随后轮询
        # 自动化运行的 processResult.images 包含选图、翻译、草稿回写的完整状态。
        from models import Product

        row = db.query(Product).filter(Product.id == int(product["id"])).one()
        row.automation_images = {
            "queued": True, "completed": False, "requested": 0, "submitted": 0,
            "failed": 0, "jobs": [], "generation": secrets.token_hex(16),
            "revision": row.revision, "startedAt": now_ts(),
        }
        row.draft_verified_revision = None
        product["draftVerified"] = False
        bg_image_job = {
            "productId": int(row.id),
            "detailId": str(row.detail_id or ""),
            "operatorId": getattr(operator, "id", None),
            "generation": row.automation_images["generation"],
            # 允许的 revision：图片线程启动后，主流程自己还会写一次库
            # （落库 + 该次入仓的收尾），revision 会从 base 推进到 base+2。
            # 只认 base 会把「我们自己的写入」误判成「别人改过」，
            # 每件商品都变待核对（2026-11-11 实测）。这里把可预期的
            # 几个值都列进去；超出这个范围说明真有第三方改动，照样拦。
            "allowedRevisions": [row.revision + delta for delta in range(0, 6)],
            "limits": {
                "main": int(payload.mainImageCount or 0),
                "sku": int(payload.skuImageCount or 0),
                "detail": int(payload.detailImageCount or 0),
            },
        }
        image_result = {"queued": True, "completed": False, "requested": 0,
                        "submitted": 0, "failed": 0, "jobs": []}

    if not payload.save:
        db.rollback()
        return {"saved": False, "product": product}
    db.commit()
    audit_service.record(
        db, operator=operator, action="warehouse.import",
        target_kind="product", target_id=key,
        account_id=account.id if account else None, shop_id=payload.shopId,
        detail={"title": str(product.get("title") or "")[:80],
                "language": str(payload.language or "")[:16],
                "template": str(payload.template or "")[:64],
                "images": (image_result or {}).get("submitted") or 0
                if isinstance(image_result, dict) else 0})
    db.commit()
    # 文字+价格+入仓已落库，先把结果返回给前端（几秒），图片翻译后台继续。
    if bg_image_job is not None:
        from services import image_service

        _reset_image_semaphore(
            payload.itemConcurrency,
            privileged=image_service._operator_is_privileged(
                getattr(operator, "id", None) or 0),
            key=getattr(operator, "id", None) or 0)
        _spawn_image_translation(bg_image_job)
    return {"saved": True, "product": product, "images": image_result}


def _as_number(value: Any) -> float | None:
    """把重量/尺寸统一成 float 再比；取不到返回 None。"""
    if value in (None, ""):
        return None
    try:
        return round(float(str(value).strip()), 6)
    except (TypeError, ValueError):
        return None


def _draft_identity_changed(saved: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
    """草稿身份字段是否被外部改动（防止覆盖别人同时做的修改）。

    只比**标题 / 类目 / 重量 / 包裹尺寸**这四项，它们是商品身份，
    我们不会在图片处理阶段改动它们，变了就是别人动过。

    **明确不比图片 URL**：主图 / 详情图 / SKU 图在翻译后全部换成新地址，
    这是本流程自己的处理结果。拿翻译前的旧 URL 去比必然判「已变化」，
    于是每件商品图片翻完都变待核对（2026-11-12 实测 product 393：
    title/cid/weight/尺寸全同，只有 9 个主图 URL 全变）。
    负责人口径：「不需要检测草稿是否改动，我自己全自动化」。
    """
    if not isinstance(saved, Mapping) or not isinstance(current, Mapping):
        return True
    if str(saved.get("title") or "") != str(current.get("title") or ""):
        return True
    if str(saved.get("cid") or "") != str(current.get("cid") or ""):
        return True
    for field in ("weight", "packageLength", "packageWidth", "packageHeight"):
        # 按数值比，不按字符串：本地存的是 33.0，妙手读回是 33，
        # 字符串比较会把「同一个尺寸」判成改动（2026-11-09 实测误判）。
        if _as_number(saved.get(field)) != _as_number(current.get(field)):
            return True
    return False

def _resume_stuck_image_jobs() -> list[int]:
    """服务启动时，把卡在 queued=True 的商品重新拉起图片线程。

    为什么需要：图片翻译跑在后台 daemon 线程里，服务重启（部署/升级）
    会直接杀掉它 —— 商品永远停在 queued=True、图片任务 0 个，
    界面一直「识别中…」，没有任何重试机制
    （2026-11-12 实测：部署重启杀了 20 件正在处理的批次，
    5 件成功的商品全部卡死在 queued=True）。

    ## 为什么必须限量（2026-11-28 修：OOM 死循环）

    原来这里是「有多少拉多少」。实测 12:27 服务被 OOM 杀掉时，
    8 件商品的 `queued=True` 还挂着，重启后**一口气全拉起来**：

        启动恢复：重新拉起 8 个卡住的图片任务 [714,715,717,716,713,720,718,719]

    8 个图片任务同时下载/OCR/翻译，内存立刻又冲顶 → 再 OOM → 再重启 →
    再全拉起来，**形成死循环**。负责人那批 10 件全报「处理过程中断
    （服务重启）」就是这么来的；12:27 和 12:53 两次 OOM 的
    `anon-rss` 都是 6.5 GB，一模一样。

    现在按 `IMAGE_RESUME_BATCH` 分批：每次只拉这么多，跑完再拉下一批。
    宁可慢一点，也不能让服务反复崩 —— 崩一次整批商品都要重来。
    """
    import secrets as _secrets

    from db.session import SessionLocal
    from models import Product

    db = SessionLocal()
    resumed: list[int] = []
    try:
        rows = (db.query(Product)
                .filter(Product.deleted_at.is_(None))
                .all())
        stuck: list[Any] = []
        for row in rows:
            ai = dict(row.automation_images or {})
            if ai.get("queued") is not True:
                continue
            # 进程被 OOM 杀掉时 `running=True` 会残留（线程没了但标记还在）。
            # 启动这一刻所有后台线程都随进程消失了，清掉这个标记才可能被重新拉起。
            if ai.get("running") is True:
                ai["running"] = False
                row.automation_images = ai
                db.add(row)
            stuck.append(row)
        if stuck:
            db.commit()

        if len(stuck) > IMAGE_RESUME_BATCH:
            logger.warning(
                "启动恢复：%d 个图片任务排队，本次只拉起前 %d 个，其余等下次",
                len(stuck), IMAGE_RESUME_BATCH)

        for row in stuck[:IMAGE_RESUME_BATCH]:
            ai = row.automation_images or {}
            job = {
                "productId": int(row.id),
                "detailId": str(row.detail_id or ""),
                "operatorId": row.operator_id,
                "generation": ai.get("generation") or _secrets.token_hex(16),
                "allowedRevisions": [row.revision + d for d in range(0, 6)],
                "limits": {
                    "main": 9, "sku": 9, "detail": 9,
                },
            }
            # 重启恢复也要按提交人的身份建信号量。
            #
            # 不建的话 `_image_semaphore_for()` 会落到默认的
            # `Semaphore(1)` —— 老师重启后突然被降到 1 并发，
            # 和「老师写回最高 5」的口径不一致（2026-11-21）。
            from services import image_service

            _reset_image_semaphore(
                0,  # 0 = 用身份上限，不受批次意愿值限制
                privileged=image_service._operator_is_privileged(
                    int(row.operator_id or 0)),
                key=int(row.operator_id or 0))
            _spawn_image_translation(job)
            resumed.append(row.id)
            logger.info("重启恢复图片线程 product=%s detailId=%s",
                        row.id, row.detail_id)
    finally:
        db.close()
    return resumed

def _spawn_image_translation(job: dict[str, Any]) -> None:
    import threading

    threading.Thread(target=_run_image_translation_guarded, args=(job,),
                     name=f"imgtr-{job.get('productId')}", daemon=True).start()


def _run_image_translation_guarded(job: dict[str, Any]) -> None:
    """先拿「写回妙手」的并发许可，再跑真正的图片翻译。

    许可按**提交这件商品的人**分配：老师 5、学员 1（负责人口径）。
    用 operatorId 作键，而不是一个全局信号量 —— 否则学员会把老师的
    那 5 个名额占掉。

    跑完（无论成败）续上一批：启动恢复时只拉 `IMAGE_RESUME_BATCH` 件，
    剩下的靠这里补 —— 否则排在后面的商品永远不会被拉起。
    """
    key = int(job.get("operatorId") or 0)
    acquired = False
    try:
        # 先过全局闸门（全服务器最多 2 个图片任务同时跑），再过写回闸门。
        # 没有全局闸门时，20 件商品各自并行，内存必爆（三次 OOM 都是 6.5 GB）。
        acquired = _acquire_global_image_slot()
        if not acquired:
            logger.warning("图片任务排队超时，跳过本次 product=%s", job.get("productId"))
            return
        with _image_semaphore_for(key):
            _run_image_translation(job)
    finally:
        if acquired:
            _release_global_image_slot()
        try:
            _pump_stuck_image_jobs()
        except Exception as exc:  # noqa: BLE001 - 续批失败不能影响已完成的
            logger.warning("续批图片任务失败：%s", exc)


def _pump_stuck_image_jobs(limit: int | None = None) -> list[int]:
    """补拉「还在 queued 但没在跑」的图片任务。

    用 `automation_images["running"]` 标记正在跑的商品，避免和
    `_resume_stuck_image_jobs` 或彼此之间重复拉起同一件。
    """
    import secrets as _secrets

    from db.session import SessionLocal
    from models import Product
    from services import image_service

    budget = IMAGE_RESUME_BATCH if limit is None else int(limit)
    db = SessionLocal()
    started: list[int] = []
    try:
        rows = (db.query(Product)
                .filter(Product.deleted_at.is_(None))
                .all())
        for row in rows:
            if len(started) >= budget:
                break
            ai = dict(row.automation_images or {})
            if ai.get("queued") is not True or ai.get("running") is True:
                continue
            # 标记占用，防止并发进入本函数时重复拉起
            ai["running"] = True
            row.automation_images = ai
            db.add(row)
            db.commit()
            job = {
                "productId": int(row.id),
                "detailId": str(row.detail_id or ""),
                "operatorId": row.operator_id,
                "generation": ai.get("generation") or _secrets.token_hex(16),
                "allowedRevisions": [row.revision + d for d in range(0, 6)],
                "limits": {"main": 9, "sku": 9, "detail": 9},
            }
            _reset_image_semaphore(
                0,
                privileged=image_service._operator_is_privileged(
                    int(row.operator_id or 0)),
                key=int(row.operator_id or 0))
            _spawn_image_translation(job)
            started.append(row.id)
            logger.info("续批图片线程 product=%s detailId=%s", row.id, row.detail_id)
    finally:
        db.close()
    return started


def _run_image_translation(job: dict[str, Any]) -> None:
    """Finish all image work, save the final draft upstream, and bind its readback proof."""
    import time
    from copy import deepcopy

    from core.locks import hold_for_session
    from db.session import SessionLocal
    from models import Account, ImageJob, Operator, Product
    from services import (
        automation_run_service,
        image_service,
        product_service,
        warehouse_service,
    )

    db = SessionLocal()
    product_id = int(job["productId"])
    generation = job["generation"]
    try:
        row = db.get(Product, product_id)
        if (row is None or (row.automation_images or {}).get("generation") != generation
                or row.automation_images.get("queued") is not True):
            return
        base_revision = row.automation_images["revision"]

        def current_generation() -> bool:
            hold_for_session(db, [f"automation-image-result:{product_id}"])
            db.refresh(row)
            current = row.automation_images or {}
            if current.get("generation") != generation or current.get("queued") is not True:
                # 已经有别的线程接手（新 generation）或状态被清掉了：
                # 静默退出，但把当前挡住的现场记下来，否则界面永远停在
                # 「识别中…」（2026-11-10 实测：queued=true 挂了一整夜，
                # 用户看到左边「完成」右边「识别中」自相矛盾）。
                logger.info(
                    "图片线程退出 product=%s：generation 不匹配或已取消"
                    "（当前=%s 期望=%s queued=%s）",
                    product_id, str(current.get("generation"))[:8],
                    str(generation)[:8], current.get("queued"))
                return False
            # revision 比较：允许「本流程自己推进」的值。
            #
            # row.revision 是本地乐观锁版本号，主流程收尾写库会推进它；
            # 如果只认 base_revision，每件商品图片处理完都会被判「草稿已变化」
            # 变待核对（2026-11-11 负责人反复指出）。但完全去掉又会丢掉
            # 「别人改过就不许覆盖」的保护（tests/test_automation_image_lifecycle
            # 的 revision 用例）。折中：job 里带上流程自己允许的版本集合。
            allowed = set(job.get("allowedRevisions") or [base_revision])
            if row.revision not in allowed or row.operator_id != job.get("operatorId"):
                raise errors.AppError(
                    "CONFLICT", "图片处理期间商品被其他人改动，请刷新核对", status=409)
            owner = (db.query(Operator).populate_existing()
                     .filter_by(id=row.operator_id).one_or_none())
            if (owner is None or not owner.enabled
                    or not account_service.has_account_access(db, row.account_id, owner.id)):
                raise errors.AppError(
                    "CONFLICT", "账号绑定或操作权限已变化，未继续提交图片", status=409)
            return True

        owner = db.get(Operator, row.operator_id)
        # 同上：不用 row.revision（本地乐观锁，主流程会推进它）
        if (row.operator_id != job.get("operatorId")
                or owner is None or not owner.enabled
                or not account_service.has_account_access(db, row.account_id, owner.id)):
            raise errors.AppError(
                "CONFLICT", "商品归属或账号绑定已变化，未继续提交图片", status=409)
        saved_payload = deepcopy(row.payload) if isinstance(row.payload, dict) else {}
        limits = job.get("limits") or {}
        picked = warehouse_service.select_translatable_images(
            saved_payload, main_limit=int(limits.get("main") or 0),
            detail_limit=int(limits.get("detail") or 0),
            sku_limit=int(limits.get("sku") or 0))
        product_map = {"id": row.id, "detailId": row.detail_id}
        state = deepcopy(row.automation_images)
        job_ids: list[str] = []
        if not current_generation():
            return
        for source in ("main", "sku", "detail"):
            urls = (picked.get(source) or [])[:int(limits.get(source) or 0)]
            if not urls:
                continue
            if not current_generation():
                return
            result = warehouse_service.attach_image_translation(
                db, product_map, limit=len(urls), source_images=urls,
                source=source, operator_id=job.get("operatorId"))
            if not current_generation():
                return
            state["requested"] += int(result.get("requested") or 0)
            state["submitted"] += int(result.get("submitted") or 0)
            state["failed"] += int(result.get("failed") or 0)
            state["jobs"].extend(result.get("jobs") or [])
            job_ids.extend(str(item["id"]) for item in result.get("jobs") or []
                           if item.get("id"))
            # 把上游给的真实原因留在 state 里：attach_image_translation 失败时
            # 会带回「图片翻译服务未配置密钥 / 今日额度已用完」这类可操作提示。
            # 旧代码只抛一句固定文案「部分图片未能提交翻译」，界面上看不到
            # 到底是没配密钥还是超预算，运营无从下手（2026-11-09 实测）。
            if result.get("reason"):
                state["reason"] = str(result["reason"])[:300]
            row.automation_images = dict(state)
            db.commit()
            if state["failed"]:
                raise errors.AppError(
                    "IMAGE_FAILED",
                    state.get("reason") or "部分图片未能提交翻译，请核对后重试",
                    status=409)
        job_ids = list(dict.fromkeys(job_ids))
        if state["requested"] and not job_ids:
            raise errors.AppError("IMAGE_FAILED", "图片翻译没有返回可核对的任务", status=409)
        deadline = time.monotonic() + 480
        while job_ids:
            jobs = db.query(ImageJob).filter(ImageJob.id.in_(job_ids)).populate_existing().all()
            if (len(jobs) != len(job_ids)
                    or any(item.product_id != product_id
                           or item.operator_id != job.get("operatorId") for item in jobs)):
                raise errors.AppError(
                    "IMAGE_FAILED", "图片任务归属发生变化，已阻止发布", status=409)
            # **2026-11-26 修**：单张失败不再作废整件。
            #
            # 原来只要有任意一张 failed/unknown 就 raise，整件商品的图片
            # 写回全部放弃 —— 已经翻好的 5 张也白花钱、白等待，商品停在
            # 「图片没处理」状态，负责人要人工重跑一遍。
            #
            # 实测 product 596（2026-11-26 试跑 5 件时）：6 个 job 里 5 个
            # ready、1 个 unknown（上游超时），结果整件报
            # 「图片翻译失败或结果未知」。
            #
            # 现在的口径：ready 的照常写回，失败的记进 state 让界面显示
            # 「N 张未完成」，用户想补就单独重试那几张。
            # 真正需要拦的只剩「任务归属变了」——那条在上面。
            broken = [item for item in jobs if item.status in {"failed", "unknown"}]
            if broken:
                reasons = "；".join(
                    str(item.message or item.error or item.status)[:60]
                    for item in broken[:3])
                state["reason"] = f"{len(broken)} 张图片未完成：{reasons}"[:300]
            state["jobs"] = [image_service.serialize(item) for item in jobs]
            pending = [item for item in jobs if item.status != "ready"]
            if not pending:
                break
            if time.monotonic() >= deadline:
                raise errors.AppError(
                    "IMAGE_TIMEOUT", "图片处理超时，结果待核对，未自动重新提交", status=409)
            for item in pending:
                image_service.refresh(db, item.id)
            if not current_generation():
                return
            row.automation_images = dict(state)
            db.commit()
            time.sleep(4)

        # The same lock is taken by finish_process so neither ordering loses the proof.
        hold_for_session(db, [f"automation-image-result:{product_id}"])
        db.refresh(row)
        if ((row.automation_images or {}).get("generation") != generation
                or row.automation_images.get("queued") is not True):
            return
        if row.operator_id != job.get("operatorId"):
            raise errors.AppError(
                "CONFLICT", "图片处理期间商品归属发生变化，请重新核对", status=409)
        owner = db.get(Operator, row.operator_id)
        account = db.get(Account, row.account_id)
        if (owner is None or not owner.enabled or account is None
                or not account_service.has_account_access(db, account.id, owner.id)):
            raise errors.AppError(
                "CONFLICT", "账号绑定或操作权限已变化，未继续保存草稿", status=409)
        payload = deepcopy(saved_payload)
        applied = warehouse_service.apply_image_results(db, row, payload, job_ids=job_ids)
        # 2026-11-26：`missing` 不再拦截保存。
        #
        # missing 是「有翻译任务但这张拿不到译文地址」的源图 —— 单张超时
        # 或失败就会出现在这里。原来一并 raise，等于 1 张没翻成、
        # 整件商品的图就全不写回（上面那条 unknown 的判断已改，这里同步）。
        # 现在：拿到的译文照常写回，没拿到的保留原图，并在 state 里
        # 记明几张未完成。`error` 仍然要拦 —— 那是「读任务/上传整体失败」，
        # 说明我们的映射根本不可信，不能拿半成品去覆盖 payload。
        if applied.get("error"):
            raise errors.AppError(
                "IMAGE_UPLOAD_FAILED", "译文图片尚未完整上传，未继续保存或发布", status=409)
        if applied.get("missing"):
            state["missing"] = len(applied["missing"])
            row.automation_images = dict(state)
            db.commit()
        client = account_service.account_client(account)
        try:
            fresh = product_service.detail_block(client.shop_detail(row.detail_id, row.shop_id))
            original = fresh["info"]
            # 草稿身份检测：只在「标题 / 类目 / 重量 / 包裹尺寸」被改时拦。
            #
            # **绝不比图片 URL**：主图 / 详情图 / SKU 图在翻译后全部换成
            # 新地址，拿旧 URL 比必然判「已变化」，每件商品都会卡在待核对
            # （负责人口径 2026-11-12：「不需要检测草稿是否改动，这个有
            # 什么意义，我自己全自动化」）。
            #
            # 保留这四个字段是有实际意义的：多账号可能同时处理同一件商品，
            # 标题/类目/重量被改说明草稿已不是我们保存的那一份，此时再写图
            # 会把别人的修改覆盖掉。这是防覆盖，不是要人核对。
            if not original or _draft_identity_changed(saved_payload, original):
                raise errors.AppError(
                    "CONFLICT", "图片处理期间商品被其他人改动，请刷新核对", status=409)
            # 以下为原注释保留说明（历史）：
            # 不再检测「草稿是否被人改过」（负责人口径 2026-11-12：
            # 「不需要检测草稿是否改动，这个有什么意义，我自己全自动化，
            #   我只需要点击就处理好，不需要核对」）。
            #
            # 为什么删掉这项检查：
            #   · 主图 / 详情图 / SKU 图在翻译后**全部换成新 URL**，
            #     拿翻译前的旧地址去比必然判「已变化」，每件商品都会
            #     卡在待核对；
            #   · 这是单人多账号的自动化流水线，不存在「别人同时改草稿」
            #     的协作场景，这项检测没有保护价值；
            #   · 它的唯一效果是把已经处理成功的商品标成待核对，
            #     反而要人去点一下才能继续。
            #
            # 仍然保留的护栏：账号/操作权限变更、译文图未上传完（上面两条）、
            # 以及保存后的读回核验 readback_check（会确认我们提交的图片
            # 确实写进了妙手草稿）。这些是「处理是否真的成功」的证据，
            # 与「草稿是否被别人动过」是两件事。
            if applied.get("replaced"):
                warehouse_service._save_processed_draft(
                    client, detail_id=row.detail_id, shop_id=row.shop_id,
                    oss_md5=fresh["ossMd5"], payload=payload, original=original,
                    coverage=(payload.get("translation") or {}).get("coverage") or {},
                    pricing_enabled=True)
                row.payload = payload
                row.images = product_service.main_images(payload)
                row.detail_images = product_service.detail_images(payload)
                row.sku_properties = product_service.sku_properties(payload)
                if row.images:
                    row.cover = row.images[0]
                row.revision += 1
        finally:
            client.close()
        row.draft_verified_revision = row.revision
        row.automation_images = {**state, "queued": False, "completed": True,
                                 "failed": 0, "running": False}
        automation_run_service.complete_image_processing(db, row)
        db.commit()
        # === 图片处理完成，打标记（2026-11-27）===
        #
        # 到这里图片已经 OCR 筛选、翻译、写回草稿并且读回核验通过。
        # 打上标记后，下次处理这件商品会跳过整套图片流程 ——
        # 图片按张计费（一张 24 毫 = 0.024 元），重复处理等于重复扣费，
        # 而且会把已经译好的图再提交一遍。
        #
        # 独立事务提交，与上面的 db.commit() 解耦：即使后续有异常也不影响标记。
        try:
            from services import draft_mark_service

            draft_mark_service.mark_image_done(
                account_id=int(row.account_id),
                detail_id=str(row.detail_id or ""),
                shop_id=str(row.shop_id or ""),
                total=int((state or {}).get("requested")
                          or (state or {}).get("submitted") or 0))
        except Exception as mark_exc:  # noqa: BLE001 - 标记失败不影响结果
            logger.warning("图片标记写入失败 product=%s：%s", product_id, mark_exc)
    except Exception as exc:  # noqa: BLE001 - never publish an unverified background result
        db.rollback()
        hold_for_session(db, [f"automation-image-result:{product_id}"])
        row = db.get(Product, product_id)
        if row is not None:
            db.refresh(row)
            if (row.automation_images or {}).get("generation") == generation:
                state = dict(row.automation_images)
                state.update({"queued": False, "completed": True, "running": False,
                              "failed": max(1, int(state.get("failed") or 0)),
                              "reason": (exc.message if isinstance(exc, errors.AppError)
                                         else "图片处理或草稿保存失败，请刷新核对")})
                row.automation_images = state
                row.draft_verified_revision = None
                automation_run_service.complete_image_processing(db, row)
                db.commit()
        logger.warning("后台图片处理未通过 product=%s type=%s", product_id, type(exc).__name__)
    finally:
        db.close()


class DeleteCollectRequest(BaseModel):
    accountId: str | int | None = None
    detailIds: list[str | int] = Field(default_factory=list)


@router.post("/claim", summary="批量认领（按仓库键）；claimed=false 为撤销认领",
             dependencies=[require_permission("claim")])
def claim_batch(payload: BatchClaimRequest, db: DbSession,
    operator: OperatorDep,
    credentials: CredentialsHeader = None,
) -> dict[str, Any]:
    require_writable(db)
    account = account_service.resolve_account(db, payload.accountId, credentials)
    if not payload.keys:
        raise errors.validation("请选择要认领的商品", field="keys")
    account_id = account.id if account else None
    # claimed=false 走撤销分支：以前这个字段定义了但没人读，传 false 也照样认领，
    # 运营标错了只能去改数据库。两条路径的返回结构保持一致，前端不用分支处理。
    releasing = payload.claimed is False
    if releasing:
        results = []
        failures: list[dict[str, Any]] = []
        for key in payload.keys[:50]:
            parts = str(key).split(":")
            if len(parts) != 2:
                failures.append({"key": key, "reason": "仓库键格式应为 detailId:shopId"})
                # 连键都解析不出，也要留痕：运营看到「失败 3 件」时得知道是哪 3 件
                audit_service.record(
                    db, operator=operator, action="warehouse.release", target_kind="product",
                    target_id=str(key)[:64], account_id=account_id, result="failed",
                    message="仓库键格式应为 detailId:shopId")
                continue
            try:
                results.append(collect_box_service.release(
                    db, account, detail_id=parts[0], shop_id=parts[1],
                    target_shop_id=payload.targetShopId))
            except errors.AppError as exc:
                failures.append({"key": key, "code": exc.code, "reason": exc.message})
                audit_service.record(
                    db, operator=operator, action="warehouse.release", target_kind="product",
                    target_id=str(key)[:64], account_id=account_id,
                    shop_id=parts[1], result="failed",
                    message=f"{exc.code}: {exc.message}")
            else:
                audit_service.record(
                    db, operator=operator, action="warehouse.release", target_kind="product",
                    target_id=str(key)[:64], account_id=account_id,
                    shop_id=parts[1], detail={"targetShopId": payload.targetShopId or ""})
        db.commit()
        return {
            "claimed": 0,
            "released": sum(1 for item in results if item.get("released")),
            "requested": len(payload.keys),
            "targetShopId": payload.targetShopId,
            "results": results,
            "failures": failures,
        }
    if not str(payload.targetShopId or "").strip():
        raise errors.validation("请选择认领到哪个店铺", field="targetShopId")
    parsed_keys = [str(key).split(":") for key in payload.keys[:50]]
    if (account is not None
            and len(parsed_keys) == min(len(payload.keys), 50)
            and all(len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit()
                    for parts in parsed_keys)
            and all(parts[1] != str(payload.targetShopId) for parts in parsed_keys)):
        try:
            results = collect_box_service.claim_carrier_batch(
                db, account,
                items=[(parts[0], parts[1]) for parts in parsed_keys],
                target_shop_id=payload.targetShopId,
                operator_id=operator.id)
        except errors.AppError as exc:
            db.rollback()
            failures = [{"key": key, "code": exc.code, "reason": exc.message}
                        for key in payload.keys[:50]]
            for parts, key in zip(parsed_keys, payload.keys[:50], strict=True):
                audit_service.record(
                    db, operator=operator, action="warehouse.claim", target_kind="product",
                    target_id=str(key)[:64], account_id=account_id,
                    shop_id=parts[1], result="failed",
                    message=f"{exc.code}: {exc.message}")
            db.commit()
            return {"claimed": 0, "requested": len(payload.keys),
                    "targetShopId": payload.targetShopId,
                    "results": [], "failures": failures}
        failures = []
        for key, parts, result in zip(payload.keys[:50], parsed_keys, results, strict=True):
            if result["claimed"]:
                audit_service.record(
                    db, operator=operator, action="warehouse.claim", target_kind="product",
                    target_id=str(key)[:64], account_id=account_id, shop_id=parts[1],
                    detail={"targetShopId": payload.targetShopId})
            else:
                reason = "; ".join(result.get("failures") or ["载体覆盖失败"])
                failures.append({"key": key, "reason": reason})
                audit_service.record(
                    db, operator=operator, action="warehouse.claim", target_kind="product",
                    target_id=str(key)[:64], account_id=account_id, shop_id=parts[1],
                    result="failed", message=reason)
        db.commit()
        return {"claimed": sum(1 for item in results if item["claimed"]),
                "requested": len(payload.keys), "targetShopId": payload.targetShopId,
                "results": results, "failures": failures}

    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for key in payload.keys[:50]:
        parts = str(key).split(":")
        if len(parts) != 2:
            failures.append({"key": key, "reason": "仓库键格式应为 detailId:shopId"})
            audit_service.record(
                db, operator=operator, action="warehouse.claim", target_kind="product",
                target_id=str(key)[:64], account_id=account_id, result="failed",
                message="仓库键格式应为 detailId:shopId")
            continue
        try:
            results.append(collect_box_service.claim(
                db, account, detail_id=parts[0], shop_id=parts[1],
                target_shop_id=payload.targetShopId, mark_collected=True))
        except errors.AppError as exc:
            failures.append({"key": key, "code": exc.code, "reason": exc.message})
            audit_service.record(
                db, operator=operator, action="warehouse.claim", target_kind="product",
                target_id=str(key)[:64], account_id=account_id, shop_id=parts[1],
                result="failed", message=f"{exc.code}: {exc.message}")
        else:
            audit_service.record(
                db, operator=operator, action="warehouse.claim", target_kind="product",
                target_id=str(key)[:64], account_id=account_id, shop_id=parts[1],
                detail={"targetShopId": payload.targetShopId})
    db.commit()
    return {
        "claimed": sum(1 for item in results if item["claimed"]),
        "requested": len(payload.keys),
        "targetShopId": payload.targetShopId,
        "results": results,
        "failures": failures,
    }


@router.post("/delete", summary="从采集箱删除商品（如带水印的废品）",
             dependencies=[require_permission("process")])
def delete_from_collect_box(payload: DeleteCollectRequest, db: DbSession,
    operator: OperatorDep,
    credentials: CredentialsHeader = None,
) -> dict[str, Any]:
    require_writable(db)
    account = account_service.resolve_account(db, payload.accountId, credentials)
    if not payload.detailIds:
        raise errors.validation("请选择要删除的商品", field="detailIds")
    ids = [str(i) for i in payload.detailIds]
    with audit_service.guard(db, operator=operator, action="warehouse.delete",
                             target_kind="product", target_id=ids[0],
                             account_id=account.id if account else None):
        result = collect_box_service.delete(db, account, ids)
    db.commit()
    # 一件一条：采集箱里的删除在上游是**真删**，出了问题（比如顺手删掉了
    # 带水印的废品之外的正常商品）必须能逐件追到人。
    for ident in ids:
        audit_service.record(db, operator=operator, action="warehouse.delete",
                             target_kind="product", target_id=ident,
                             account_id=account.id if account else None,
                             detail={"verb": "collect_box",
                                     "batchSize": len(ids)})
    db.commit()
    return result
