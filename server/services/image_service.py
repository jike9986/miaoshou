"""图片翻译队列：同步版本 + 限流 + 预扣费台账。

核心不变量（不可回归）：
  · 任务 id = sha256(原图 + model + prompt + size)，命中 ready 直接复用，不重复扣费；
  · 累计预扣费上限 10000 毫央（10 元），在写事务内检查；
  · 状态五态：queued / submitted / ready / unknown / failed；
  · unknown 不自动重试（结果未确认，可能已扣费）；
  · 上传原图上限 15MB。

本轮实现为同步版本：提交后立即向上游要结果，拿不到就落 submitted 并返回
task_id，由 GET /images/jobs/{id}?refresh=true 查询（不重复提交、不重复扣费）。
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import os
import threading
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import func
from sqlalchemy.orm import Session

from core import errors
from core.config import get_settings
from core.constants import (
    IMAGE_BUDGET_MILLIYUAN,
    IMAGE_MAX_BYTES,
    IMAGE_MODEL,
    IMAGE_RATE_LIMITS,
    IMAGE_SIZES,
    UNIT_MILLIYUAN,
)
from core.utils import image_job_id, now_ts, redact, safe_filename, sha256_hex
from models.image_job import ImageJob
from models.product import Product
from services import setting_service
from services.miaoshou import system_proxy

# 商品发往哪个站点，图上的字就该是哪个语言（负责人 2026-11-09 定）：
# 马来站用马来语。老写法写死「翻译成英语」，马来站的买家根本看不懂，
# 等于白翻。语言由调用方按商品的 target_language 传入，默认马来语。
TRANSLATION_PROMPT_TEMPLATE = (
    "任务：仅把原商品图片中的全部中文翻译成{language}，逐处替换中文文字。"
    "准确、完整地翻译标题、卖点、说明、小字和水印，不遗漏、不编造。"
    "保留已有非中文文字、数字、型号、尺寸、单位、商标及图标。"
    "保持文字原有的位置、层级、字体风格、颜色、对齐和间距；译文过长时仅在原文字区域内缩小或换行。"
    "只修复清除中文字后的局部背景；商品外观、颜色、材质、光影、背景、构图和画面比例保持原样。"
    "输出前逐处核对：所有中文均已替换为{language}，没有汉字或中文残影；只输出编辑后的图片。"
)

# 站点语言 -> prompt 用语
LANGUAGE_PROMPT_NAMES = {
    "ms": "马来语",
    "en": "英语",
    "th": "泰语",
    "vi": "越南语",
    "id": "印尼语",
}


# 兼容旧引用（tests / 老代码直接拿这个常量拼 job id）。保持默认语言
# 的 prompt 形态，保证同语言下 job id 稳定（image_job_id 按 prompt 摘要）。
TRANSLATION_PROMPT = TRANSLATION_PROMPT_TEMPLATE.format(language="马来语")


def translation_prompt(language: str = "ms") -> str:
    """按目标语言生成翻译指令。"""
    target = str(language or "ms").strip().lower()
    name = LANGUAGE_PROMPT_NAMES.get(target, "马来语")
    return TRANSLATION_PROMPT_TEMPLATE.format(language=name)
WINDOWS = {"minute": 60, "hour": 3600, "day": 86400}
# 图片翻译并发（负责人口径，2026-11-21 重新确认）。
#
# 分清两件完全不同的事，别混：
#
#   ┌ 发出去翻译（提交给上游中转站）
#   │   UPSTREAM_IMAGE_CONCURRENCY         全服务器合计上限 = 500
#   │   PER_USER_IMAGE_CONCURRENCY         每个学员账号上限 = 50
#   │   老师的图片翻译**不占**学员那份额度，但同样计入 500 的总额
#   │
#   └ 翻译完写回妙手（落库 + save_shop）
#       TEACHER_WRITEBACK_CONCURRENCY      老师 = 5
#       MEMBER_WRITEBACK_CONCURRENCY       学员 = 1
#
# 负责人原话：
#   「服务器能够同时把图片发到上游翻译的张数上限为 500……每个学员用户
#     当前限制是 50 个，一个产品处理肯定是没有 50 个上限的，所以可以把
#     两三四件里面的图片一起上传翻译。但是老师没有限制，老师学员所有
#     加起来的图片并发为 500，但是老师图片翻译完成写回来是最高 5 个，
#     学员都是默认 1 个」
#
# ## 单件商品不该受 50 限制
#
# `IMAGE_CONCURRENCY` 是**单次 translate() 里最多同时发几张**。它只受
# 「本用户剩余额度」和「全局剩余额度」约束，不写死 50 —— 一件 40 张图的
# 商品可以一次全发（只要额度和全局闸门允许）。所以这里取一个够大的值，
# 真正的闸门交给下面两个 Semaphore。
IMAGE_CONCURRENCY = 200

# 全服务器合计同时提交给上游翻译的张数上限（负责人 2026-11-21：500）。
# 上游给自己留了 1000 的余量，我们只占一半。
UPSTREAM_IMAGE_CONCURRENCY = 500

# 每个学员账号同时提交的上限（负责人：每个学员 50）。
PER_USER_IMAGE_CONCURRENCY = 50

# 翻译完成写回妙手的并发：老师 5、学员 1（负责人明确）。
TEACHER_WRITEBACK_CONCURRENCY = 5
MEMBER_WRITEBACK_CONCURRENCY = 1

# 全局闸门：跨用户共享，保证同时提交给上游的总数不超过 500。
#
# 多人同时用时，每件商品各开一个 ThreadPoolExecutor，光靠单件并发压不住
# 总数 —— 全靠这个 Semaphore 把总提交数卡住。拿不到许可就等（阻塞在提交
# 线程里），不会丢任务、不会报错。
_GLOBAL_SUBMIT_SLOTS = threading.BoundedSemaphore(UPSTREAM_IMAGE_CONCURRENCY)

# 每个用户各自的闸门（学员 50；老师不限，故不建闸门）。
#
# 为什么按用户单独一个 Semaphore 而不是一个总数：负责人要的是「每个学员
# 50，老师不限制」，那就必须分开计数。用 dict 惰性创建，键是 operator_id。
_USER_SUBMIT_SLOTS: dict[int, threading.BoundedSemaphore] = {}
_USER_SUBMIT_LOCK = threading.Lock()


def _user_submit_slot(operator_id: int | None) -> threading.BoundedSemaphore | None:
    """取该用户的上游提交闸门；老师/未知身份返回 None（不限）。"""
    if operator_id is None:
        return None
    if _operator_is_privileged(operator_id):
        return None
    with _USER_SUBMIT_LOCK:
        slot = _USER_SUBMIT_SLOTS.get(operator_id)
        if slot is None:
            slot = threading.BoundedSemaphore(PER_USER_IMAGE_CONCURRENCY)
            _USER_SUBMIT_SLOTS[operator_id] = slot
        return slot


def _operator_is_privileged(operator_id: int) -> bool:
    """老师 / 管理员不受 50 的限制。

    这里只读一次角色，命中缓存后几乎无开销；查不到身份时按「受限」处理
    （宁可慢一点，也不能让额度被无主线程吃满）。
    """
    cached = _PRIVILEGED_CACHE.get(operator_id)
    if cached is not None:
        return cached
    try:
        from db.session import SessionLocal
        from models import Operator
        from services import operator_service

        db = SessionLocal()
        try:
            operator = db.query(Operator).filter(Operator.id == operator_id).one_or_none()
            privileged = (operator is not None
                          and operator_service.role_of(operator) in {
                              operator_service.ROLE_ADMIN,
                              operator_service.ROLE_TEACHER})
        finally:
            db.close()
    except Exception:  # noqa: BLE001 - 判定失败按受限处理
        privileged = False
    _PRIVILEGED_CACHE[operator_id] = privileged
    return privileged


_PRIVILEGED_CACHE: dict[int, bool] = {}


def writeback_concurrency(operator_id: int | None) -> int:
    """翻译完成写回妙手的并发：老师/管理员 5，学员 1。"""
    if operator_id is not None and _operator_is_privileged(operator_id):
        return TEACHER_WRITEBACK_CONCURRENCY
    return MEMBER_WRITEBACK_CONCURRENCY


STATUSES = ("queued", "submitted", "ready", "unknown", "failed")


# --- 限流（滑动窗口，按 image_jobs.created_at 统计） ----------------------
def rate_limits(db: Session) -> dict[str, int]:
    configured = setting_service.image_limits(db)
    limits = dict(IMAGE_RATE_LIMITS)
    for key in ("minute", "hour", "day"):
        try:
            value = int(configured.get(key))
        except (TypeError, ValueError):
            continue
        if value > 0:
            limits[key] = value
    return limits


def budget_limit(db: Session) -> int:
    configured = setting_service.image_limits(db)
    try:
        value = int(configured.get("budgetMilliyuan"))
    except (TypeError, ValueError):
        return IMAGE_BUDGET_MILLIYUAN
    return value if value > 0 else IMAGE_BUDGET_MILLIYUAN


def reserved_total(db: Session, *, operator_id: int | None = None) -> int:
    query = db.query(func.coalesce(func.sum(ImageJob.reserved_milliyuan), 0))
    if operator_id is not None:
        query = query.filter(ImageJob.operator_id == operator_id)
    total = query.scalar()
    return int(total or 0)


def usage(db: Session, *, operator_id: int | None = None) -> dict[str, Any]:
    """Return image usage, optionally scoped to one operator.

    Operational quota checks omit ``operator_id`` and remain installation-wide.
    The account-facing usage endpoint passes the signed-in member's id so that
    activity and reserved-cost totals do not disclose other members' work.
    """
    now = time.time()
    limits = rate_limits(db)
    used: dict[str, int] = {}
    for name, window in WINDOWS.items():
        query = db.query(ImageJob).filter(ImageJob.created_at >= now - window)
        if operator_id is not None:
            query = query.filter(ImageJob.operator_id == operator_id)
        used[name] = query.count()
    next_slot = 0.0
    for name, window in WINDOWS.items():
        if used[name] >= limits[name]:
            query = db.query(ImageJob.created_at).filter(
                ImageJob.created_at >= now - window)
            if operator_id is not None:
                query = query.filter(ImageJob.operator_id == operator_id)
            oldest = (query.order_by(ImageJob.created_at.asc())
                      .offset(max(0, used[name] - limits[name])).first())
            if oldest and oldest[0]:
                next_slot = max(next_slot, float(oldest[0]) + window - now)
    reserved = reserved_total(db, operator_id=operator_id)
    limit = budget_limit(db)
    return {
        "usedToday": used["day"],
        "usedThisHour": used["hour"],
        "usedThisMinute": used["minute"],
        "budgets": limits,
        "nextSlotSeconds": round(max(0.0, next_slot), 1),
        "budget": {
            "limitMilliyuan": limit,
            "reservedMilliyuan": reserved,
            "remainingMilliyuan": max(0, limit - reserved),
            "unitMilliyuan": UNIT_MILLIYUAN,
        },
    }


# 分钟窗口塞满时最多等这么久；再长就不值得让用户干等了
RATE_WAIT_SECONDS = 300
# 一次等多久再重新算额度（别一口气 sleep 到天亮，中间可能有人撤销）
RATE_WAIT_STEP = 5.0


def _reserve_slot(db: Session) -> None:
    """滑动窗口限流：分钟窗口满了就等，日额度用完才失败。

    老版是「阻塞排队」：窗口满了 sleep 到有位置，最长等 300 秒，
    只有今日额度耗尽才立刻抛错。注释原话是「the window blocks (rather than
    fails) so a batch slows down instead of collapsing」。
    新版此前是直接 429——批量翻译从「自动变慢后跑完」退化成「整批硬失败」，
    运营只能手动重试，而且每批只查一次，超量时是整批被拒。
    """
    import time as _time

    deadline = _time.monotonic() + RATE_WAIT_SECONDS
    while True:
        state = usage(db)
        used = {
            "minute": state["usedThisMinute"],
            "hour": state["usedThisHour"],
            "day": state["usedToday"],
        }
        blocking = [name for name in WINDOWS if used[name] >= state["budgets"][name]]
        if not blocking:
            return
        # 日额度用完不是「稍等就好」，直接失败，别让人白等五分钟
        if "day" in blocking:
            raise errors.AppError(
                "RATE_LIMITED",
                f"图片翻译今日额度已用完（每天 {state['budgets']['day']} 张），"
                "请在图片设置里调整额度或明天再试",
                status=429,
                details={"budgets": state["budgets"], "blocked": blocking},
            )
        remaining = deadline - _time.monotonic()
        if remaining <= 0:
            raise errors.AppError(
                "RATE_LIMITED",
                f"图片翻译排队超过 {RATE_WAIT_SECONDS} 秒仍未轮到："
                f"每分钟最多 {state['budgets']['minute']} 张、"
                f"每小时 {state['budgets']['hour']} 张",
                status=429,
                details={"budgets": state["budgets"], "blocked": blocking,
                         "waitedSeconds": RATE_WAIT_SECONDS},
            )
        _time.sleep(min(RATE_WAIT_STEP, remaining))
        # 换掉会话里的状态，重新统计窗口
        db.expire_all()


async def _reserve_slot_async(db: Session) -> None:
    """_reserve_slot 的异步版本：用 asyncio.sleep 代替 time.sleep。

    在 async 端点中调用此函数，asyncio.sleep 只挂起当前协程而不占用线程池名额。
    同步版本的 time.sleep 在线程池中等待最多 300 秒，高并发时会耗尽线程池导致
    所有路由无响应；异步版本解决了这个问题。
    DB 查询（usage/expire_all）耗时极短（<10ms），短暂占用事件循环可接受。
    """
    import time as _time

    deadline = _time.monotonic() + RATE_WAIT_SECONDS
    while True:
        state = usage(db)
        used = {
            "minute": state["usedThisMinute"],
            "hour": state["usedThisHour"],
            "day": state["usedToday"],
        }
        blocking = [name for name in WINDOWS if used[name] >= state["budgets"][name]]
        if not blocking:
            return
        if "day" in blocking:
            raise errors.AppError(
                "RATE_LIMITED",
                f"图片翻译今日额度已用完（每天 {state['budgets']['day']} 张），"
                "请在图片设置里调整额度或明天再试",
                status=429,
                details={"budgets": state["budgets"], "blocked": blocking},
            )
        remaining = deadline - _time.monotonic()
        if remaining <= 0:
            raise errors.AppError(
                "RATE_LIMITED",
                f"图片翻译排队超过 {RATE_WAIT_SECONDS} 秒仍未轮到："
                f"每分钟最多 {state['budgets']['minute']} 张、"
                f"每小时 {state['budgets']['hour']} 张",
                status=429,
                details={"budgets": state["budgets"], "blocked": blocking,
                         "waitedSeconds": RATE_WAIT_SECONDS},
            )
        await asyncio.sleep(min(RATE_WAIT_STEP, remaining))
        db.expire_all()


# --- 上游调用 -------------------------------------------------------------
def _client() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(240.0, connect=15.0),
                        follow_redirects=False, proxy=system_proxy())


def _api_key() -> str:
    key = get_settings().image_api_key()
    if not key:
        raise errors.AppError(
            "IMAGE_KEY_MISSING",
            "图片翻译服务未配置密钥：请设置 AOHUANG_IMAGE_KEY 或写入数据目录的 image-key.txt",
            status=503,
        )
    return key


# 超过这个体积才降采样；目标是把 1600px 的长边压到够用又不糊
# 翻译前的降采样门槛。
#
# 负责人 2026-11-18：「图片翻译这个也是消耗流量比较大吧」——确实大。
# 翻译一张图要往返 4 次（下载原图 → 上传 API → 返回结果 → 存图床），
# 单张约 2.6 MB；一件商品 7 张主图 = 18 MB，若是 3.8 MB 的 PNG 就 100+ MB。
#
# 翻译只需要看清图里的文字，1200px 足够。门槛从 2MB 降到 500KB 后，
# 绝大多数商品图都会先降采样再上传，翻译流量能省一半以上。
SHRINK_LIMIT = 500 * 1024
SHRINK_MAX_SIDE = 1200
SHRINK_QUALITY = 85

# 翻译结果图落盘时的编码参数（2026-11-26）。
#
# 与上面的 SHRINK_* 是两回事：
#   SHRINK_*  用在**送翻译前的源图**上（限 1200px / q85，为省上传流量）；
#   RESULT_*  用在**翻译回来的结果图**上（不降分辨率，q95 保文字）。
#
# 为什么不复用同一条：结果图是要发给妙手当商品图的终态，
# 「限 1200px + q85」两者都会让文字变软。实测见 _shrink_result 的注释。
RESULT_JPEG_QUALITY = 95
# 已经是 JPEG 且小于这个体积就原样保留，不重编码（重编码只白掉一次画质）
RESULT_KEEP_AS_IS_BYTES = 300 * 1024


def _shrink(raw: bytes) -> bytes:
    """超大图先降采样再上传——这是省流量、省时长最有效的一步。

    老版注释原话：「images are shrunk before they are sent -- this is the
    single biggest bandwidth win」。**永不失败**：Pillow 没装、图坏了、
    压完反而更大，一律原样返回，不能因为优化把整批翻译搞挂。

    带透明通道（RGBA / LA）的图不能直接转 RGB——Pillow 会把透明区域填成黑色，
    导致商品图背景变黑而非白。这里把 alpha 合成到白底再压缩，保证视觉正确。
    """
    if len(raw) <= SHRINK_LIMIT:
        return raw
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(raw)) as image:
            if max(image.size) <= SHRINK_MAX_SIDE:
                return raw
            # 处理带透明通道的图片：合成到白色背景，避免透明区域变黑
            has_alpha = image.mode in ("RGBA", "LA") or (
                image.mode == "P" and "transparency" in image.info)
            if has_alpha:
                if image.mode != "RGBA":
                    image = image.convert("RGBA")
                background = Image.new("RGB", image.size, (255, 255, 255))
                background.paste(image, mask=image.split()[3])
                image = background
            else:
                image = image.convert("RGB")
            image.thumbnail((SHRINK_MAX_SIDE, SHRINK_MAX_SIDE), Image.LANCZOS)
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=SHRINK_QUALITY, optimize=True)
        smaller = buffer.getvalue()
        return smaller if len(smaller) < len(raw) else raw
    except (ImportError, OSError, ValueError):
        return raw


def _repair_zone_url(url: str) -> str:
    """图床地址指向的分区里文件已经不在时，换到文件真正的分区。

    ## 为什么需要（2026-11-21 实测）

    入库搬运 `promote_to_product_zone()` 会把文件从 `auto-temp/` 移到
    `products/`，并 `unlink()` 源文件。但妙手草稿里存的仍是旧地址
    —— 于是重新处理这件商品时，`_fetch_source()` 拿到的还是
    `.../auto-temp/xxx.jpg`，那个文件早已不存在 → **HTTP 404**
    → 后台图片翻译失败 → `draft_verified_revision=None`
    → 商品死锁在 needs_review，永不进「自动化结果」。

    实测 detailId=3549610513（id=559）：19 个图片地址里 18 个 404，
    而对应文件 18 个全都在 `products/` 里。这不是网络问题，
    是「地址和文件分了家」。

    修复策略（只动明确失联的地址，不做任何猜测）：
      · 地址在 auto-temp、但本地 auto-temp 没这个文件、
        而 products 有同名文件 → 换到 products
      · 反过来（地址在 products、文件在 auto-temp）同理
      · 拿不准就原样返回，让上层的 404 照常报出来，不掩盖真问题
    """
    text = str(url or "").strip()
    if not text.startswith("https://"):
        return url
    try:
        from services import image_upload
    except Exception:  # noqa: BLE001 - 循环依赖兜底
        return url

    product_dir, product_base, _pl, _pb = image_upload._zone_paths("product")
    auto_dir, auto_base, _al, _ab = image_upload._zone_paths("auto")
    if text.startswith(product_base):
        name = text[len(product_base):]
        if not name or "/" in name:
            return url
        if os.path.isfile(os.path.join(product_dir, name)):
            return url
        if os.path.isfile(os.path.join(auto_dir, name)):
            return auto_base + name
        return url
    if text.startswith(auto_base):
        name = text[len(auto_base):]
        if not name or "/" in name:
            return url
        if os.path.isfile(os.path.join(auto_dir, name)):
            return url
        if os.path.isfile(os.path.join(product_dir, name)):
            return product_base + name
        return url
    return url


def _fetch_source(url: str) -> bytes:
    if url.startswith("data:"):
        return _decode_base64(url.split(",", 1)[-1])
    url = _repair_zone_url(url)
    try:
        with httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0),
                          follow_redirects=True, proxy=system_proxy()) as client:
            response = client.get(url)
    except httpx.HTTPError as exc:
        raise errors.upstream(f"下载原图失败：{type(exc).__name__}") from None
    if response.status_code >= 400:
        raise errors.upstream(f"下载原图失败：HTTP {response.status_code}")
    raw = response.content
    if len(raw) > IMAGE_MAX_BYTES:
        raise errors.AppError(errors.IMAGE_TOO_LARGE,
                              f"原图超过 {IMAGE_MAX_BYTES // (1024 * 1024)} MB",
                              status=400, details={"url": url, "bytes": len(raw)})
    if not raw:
        raise errors.upstream("下载原图得到空内容")
    return raw


def _decode_base64(value: str) -> bytes:
    try:
        raw = base64.b64decode(value, validate=False)
    except (binascii.Error, ValueError):
        raise errors.validation("imageBase64 不是合法 base64", field="imageBase64") from None
    if len(raw) > IMAGE_MAX_BYTES:
        raise errors.AppError(errors.IMAGE_TOO_LARGE,
                              f"原图超过 {IMAGE_MAX_BYTES // (1024 * 1024)} MB",
                              status=400, details={"bytes": len(raw)})
    return raw


def _image_type(raw: bytes) -> tuple[str, str] | None:
    """按魔数判图片格式；判不出返回 None。

    老版 image_type() 判不出就抛「图片格式须为 PNG、JPEG 或 WebP」。
    这里不能兜底成 image/jpeg：上游返回 HTML 错误页、截断文件、
    GIF 之类的二进制都会被当图片提交并扣费，用户还看不到那张图。
    """
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "jpg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp", "webp"
    return None


def _first_text(value: Any, names: set[str]) -> str:
    found = ""

    def visit(node: Any) -> None:
        nonlocal found
        if found:
            return
        if isinstance(node, Mapping):
            for key, item in node.items():
                if str(key).lower() in names and isinstance(item, (str, int)) and str(item).strip():
                    found = str(item).strip()
                    return
                visit(item)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(value)
    return found


def _image_urls(data: Any) -> list[str]:
    urls: list[str] = []

    def visit(node: Any) -> None:
        if isinstance(node, Mapping):
            for key, item in node.items():
                lowered = str(key).lower()
                if lowered in {"url", "image_url", "imageurl", "result_url", "resulturl",
                               "output_url", "outputurl", "b64_json"} and isinstance(item, str):
                    if item and item not in urls:
                        urls.append(item)
                    continue
                visit(item)
        elif isinstance(node, list):
            for item in node:
                visit(item)
        elif (isinstance(node, str)
                and node.startswith(("https://", "http://", "data:image"))
                and node not in urls):
            urls.append(node)

    visit(data)
    return urls


def _decode_result(data: Mapping[str, Any]) -> bytes | None:
    for item in data.get("data") or [] if isinstance(data.get("data"), list) else []:
        if isinstance(item, Mapping) and item.get("b64_json"):
            try:
                return base64.b64decode(str(item["b64_json"]))
            except (binascii.Error, ValueError):
                continue
    for url in _image_urls(data):
        if url.startswith("data:"):
            try:
                return base64.b64decode(url.split(",", 1)[-1])
            except (binascii.Error, ValueError):
                continue
        try:
            with httpx.Client(timeout=httpx.Timeout(120.0, connect=15.0),
                              follow_redirects=True, proxy=system_proxy()) as client:
                response = client.get(url)
            if response.status_code < 400 and response.content:
                return response.content
        except httpx.HTTPError:
            continue
    return None


def _submit(raw: bytes, size: str, *, prompt: str) -> dict[str, Any]:
    """提交一次图片编辑；返回 {"image": bytes|None, "taskId": str, "status": str}。"""
    settings = get_settings()
    key = _api_key()
    detected = _image_type(raw)
    if detected is None:
        raise errors.unprocessable("图片格式须为 PNG、JPEG 或 WebP")
    mime, extension = detected
    fields = {
        "model": IMAGE_MODEL,
        "prompt": prompt,
        "n": "1",
        "size": size,
        "quality": "high",
        "response_format": "b64_json",
    }
    try:
        with _client() as client:
            response = client.post(settings.edits_endpoint(), data=fields,
                                   files={"image": (f"source.{extension}", raw, mime)},
                                   headers={"Authorization": f"Bearer {key}"})
    except httpx.TimeoutException:
        raise errors.AppError(errors.UPSTREAM_UNKNOWN_RESULT,
                              "图片请求超时，结果未确认（可能已扣费），不会自动重试",
                              status=502) from None
    except httpx.HTTPError as exc:
        raise errors.AppError(errors.UPSTREAM_UNKNOWN_RESULT,
                              f"图片请求结果未知（{type(exc).__name__}），未自动重试",
                              status=502) from None

    try:
        data = response.json()
    except ValueError:
        data = {}
    task_id = _first_text(data, {"task_id", "taskid", "job_id", "jobid"})
    if not task_id:
        # 有些异步接口把任务号放在响应头里，body 是空的。
        # 只看 body 会导致「没图也没 id」→ 判成 unknown，白扣一次费。
        for header in ("x-task-id", "x-taskid", "x-job-id"):
            value = str(response.headers.get(header) or "").strip()
            if value:
                task_id = value
                break
    upstream_status = _first_text(data, {"status", "state", "task_status"})
    if response.status_code >= 400:
        message = _first_text(data, {"message", "error", "msg"}) or f"HTTP {response.status_code}"
        # 上游常把整个请求头回显在报错里（Authorization: Bearer sk-...）。
        # 这条 message 会进 job.message、/images/jobs 和日志，必须先抹密钥。
        safe = redact(message, [key, f"Bearer {key}"])[:200]
        raise errors.upstream(f"图片服务返回失败：{safe}",
                              status=response.status_code)
    output = _decode_result(data) if isinstance(data, Mapping) else None
    if output:
        return {"image": output, "taskId": task_id, "status": "ready"}
    if task_id:
        return {"image": None, "taskId": task_id, "status": "submitted",
                "upstreamStatus": upstream_status or "processing"}
    raise errors.AppError(errors.UPSTREAM_UNKNOWN_RESULT,
                          "图片服务返回成功但没有图片也没有任务 ID；结果未确认，不会自动重试",
                          status=502)


def query_task(task_id: str) -> dict[str, Any]:
    """查询异步任务；返回 {"status", "image": bytes|None, "upstreamStatus"}。"""
    settings = get_settings()
    key = _api_key()
    endpoint = f"{settings.task_endpoint().rstrip('/')}/{task_id}"
    try:
        with httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0),
                          proxy=system_proxy()) as client:
            response = client.get(endpoint, headers={"Authorization": f"Bearer {key}"})
    except httpx.HTTPError as exc:
        raise errors.upstream(f"查询图片任务失败：{type(exc).__name__}") from None
    if response.status_code == 404:
        # 404 不等于失败：可能只是还没落库/查询路径拼错/任务刚提交。
        # 判成 failed 会让「可能已扣费」的图彻底作废；保持 submitted 等下次再查。
        return {"status": "submitted", "upstreamStatus": "not_found",
                "message": "上游暂时查不到这个任务，稍后重试"}
    if response.status_code >= 400:
        raise errors.upstream(f"查询图片任务失败：HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError:
        data = {}
    status = (_first_text(data, {"status", "state", "task_status"}) or "").lower()
    output = _decode_result(data) if isinstance(data, Mapping) else None
    if output:
        return {"status": "ready", "image": output, "upstreamStatus": status or "succeeded"}
    if status in {"failed", "error", "cancelled", "canceled"}:
        return {"status": "failed", "upstreamStatus": status,
                "message": _first_text(data, {"message", "error", "msg"}) or "上游任务失败"}
    return {"status": "submitted", "upstreamStatus": status or "processing"}


# --- 任务落库 -------------------------------------------------------------
def serialize(job: ImageJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "batchId": job.batch_id or "",
        "productId": str(job.product_id) if job.product_id else None,
        "detailId": job.detail_id or "",
        "source": job.source,
        "sourceUrl": job.source_url or "",
        "status": job.status,
        "model": job.model,
        "prompt": job.prompt,
        "size": job.size,
        "reservedMilliyuan": int(job.reserved_milliyuan or 0),
        "taskId": job.task_id,
        "upstreamStatus": job.upstream_status or "",
        "filename": job.filename or None,
        "previewUrl": (f"/api/v1/images/jobs/{job.id}/file" if job.filename else None),
        # previewUrl 是本机回环地址，只有本机能看；上游平台与导出要的是公网地址，
        # 所以单独暴露一列，不把 previewUrl 改成公网（前端预览仍走本地，不依赖外网）。
        "publicUrl": job.public_url or None,
        "uploadedAt": job.uploaded_at,
        "uploadError": job.upload_error or "",
        "message": job.message or "",
        "retryOf": job.retry_of,
        "attempt": int(job.attempt or 0),
        "createdAt": job.created_at,
        "updatedAt": job.updated_at,
        "readyAt": job.ready_at,
    }


def get_job(db: Session, job_id: str) -> ImageJob:
    row = db.query(ImageJob).filter(ImageJob.id == str(job_id)).one_or_none()
    if row is None:
        raise errors.not_found(f"图片任务 {job_id} 不存在", id=str(job_id))
    return row


def list_jobs(db: Session, *, batch_id: str | None = None, product_id: Any = None,
              status: str | None = None, page: int = 1, page_size: int = 50,
              operator_id: int | None = None
              ) -> dict[str, Any]:
    if status is not None and status not in STATUSES:
        raise errors.validation("任务状态无效", field="status", value=status)
    if page < 1:
        raise errors.validation("页码必须不小于 1", field="page", value=page)
    if not 1 <= page_size <= 100:
        raise errors.validation("每页数量必须在 1 到 100 之间", field="pageSize", value=page_size)
    query = db.query(ImageJob)
    if operator_id is not None:
        query = query.filter(ImageJob.operator_id == operator_id)
    if batch_id:
        query = query.filter(ImageJob.batch_id == str(batch_id))
    if product_id not in (None, ""):
        text = str(product_id)
        if text.isdigit():
            query = query.filter(ImageJob.product_id == int(text))
    if status:
        query = query.filter(ImageJob.status == status)
    total = query.count()
    rows = (query.order_by(ImageJob.created_at.desc())
            .offset((page - 1) * page_size).limit(page_size).all())
    return {
        "items": [serialize(row) for row in rows],
        "page": page,
        "pageSize": page_size,
        "total": total,
        "hasMore": page * page_size < total,
        "budget": usage(db, operator_id=operator_id)["budget"],
    }


def _product_id(db: Session, value: Any) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    if not text.isdigit():
        raise errors.validation("productId 必须是数字", field="productId", value=value)
    row = db.query(Product).filter(Product.id == int(text)).one_or_none()
    if row is None:
        raise errors.not_found(f"仓库里没有商品 {text}", productId=text)
    return row.id


def _shrink_result(raw: bytes) -> tuple[bytes, str]:
    """翻译结果图落盘前转 JPEG —— **不降分辨率**，只换编码。

    ## 为什么只换编码、不缩尺寸（2026-11-26 实测）

    负责人：「你压缩了后面原图不是很模糊吗」

    PNG 是无损格式，照片存 PNG 会大得离谱。实测 job
    9f0a7b34e05b（商品 3563411743 的 Hitam 颜色图）：

        翻译结果  1254 x 1254  PNG  2 307 339 字节 (2.20 MB)
        原始中文图  800 x  800  JPG     92 556 字节

    也就是说**翻译服务把 800x800 放大到了 1254x1254** —— 放大的图本来
    就没有更多细节，只要不缩小分辨率，JPEG 压不出「更模糊」。

    实测各质量档（保持 1254x1254，subsampling=0）：

        q=95   558 KB   全图 PSNR 44.62 dB   文字块 42.76 dB  视觉无损
        q=92   421 KB   全图 PSNR 42.72 dB   文字块 40.32 dB  视觉无损
        q=90   379 KB   全图 PSNR 41.82 dB   文字块 39.12 dB  几乎无差别
        q=85   300 KB   全图 PSNR 40.07 dB   文字块 36.89 dB  文字边缘变软
        q=80   253 KB   全图 PSNR 38.83 dB   文字块 35.36 dB  能看出

    商品图上最怕糊的就是**翻译后的文字**，所以取 q=95（文字块 42.76 dB，
    与无损不可区分），体积降到 24%。q85 省得更多但文字会软 —— 不取。

    ## 不做的事

    · 不调用 `thumbnail()`：`_shrink` 是给「送翻译前的源图」用的，
      它会缩到 1200px 且用 q85，两者都会让结果图变差。
    · 带 alpha 的图合成白底再转（否则透明区变黑），沿用 `_shrink` 的做法。
    · **永不失败**：Pillow 没装、图坏了、转完反而更大，一律原样返回。

    返回 (字节, 扩展名)。
    """
    try:
        import io

        from PIL import Image

        detected = _image_type(raw)
        if detected is None:
            return raw, "jpg"
        mime, extension = detected
        # 本来就是 JPEG 且体积不大：没必要重编码，重编码只会白掉一次画质
        if mime == "image/jpeg" and len(raw) <= RESULT_KEEP_AS_IS_BYTES:
            return raw, extension
        with Image.open(io.BytesIO(raw)) as image:
            has_alpha = image.mode in ("RGBA", "LA") or (
                image.mode == "P" and "transparency" in image.info)
            if has_alpha:
                if image.mode != "RGBA":
                    image = image.convert("RGBA")
                background = Image.new("RGB", image.size, (255, 255, 255))
                background.paste(image, mask=image.split()[3])
                image = background
            else:
                image = image.convert("RGB")
            buffer = io.BytesIO()
            # subsampling=0（4:4:4）：不做色度抽样，文字边缘的彩色镶边才不会糊。
            # 默认的 4:2:0 会把色度分辨率砍半，q 再高文字边也不干净。
            image.save(buffer, format="JPEG", quality=RESULT_JPEG_QUALITY,
                       optimize=True, subsampling=0)
        smaller = buffer.getvalue()
        if len(smaller) < len(raw):
            return smaller, "jpg"
        return raw, extension
    except Exception:  # noqa: BLE001 - 优化失败绝不能把翻译成果弄丢
        logging.getLogger(__name__).warning(
            "翻译结果转 JPEG 失败，按原格式保存", exc_info=True)
        detected = _image_type(raw)
        return raw, (detected[1] if detected else "jpg")


def _write_result(raw: bytes, job_id: str) -> str:
    settings = get_settings()
    settings.ensure_dirs()
    # 上游回的不一定是图（HTML 错误页、截断文件）。老版在这种情况直接报错，
    # 不能落盘成 .jpg 再以 image/jpeg 吐给前端——用户看到裂图，钱已经花了。
    raw, extension = _shrink_result(raw)
    if _image_type(raw) is None:
        raise errors.upstream("图片服务返回的不是有效图片")
    name = f"{safe_filename(job_id)}.{extension}"
    target: Path = settings.image_results_dir / name
    target.write_bytes(raw)
    return name


def _chinese_residue(filename: str) -> str:
    """本地 OCR 复查译文是否还带中文；返回提示文案，无残留或 OCR 不可用返回空串。

    老版 `image_review.py` 的用途是「翻译后仍检出中文就拒绝」，新版只标注：
    图已经翻译并扣费，判成失败会让操作者重复付费。
    """
    if not filename:
        return ""
    try:
        from services import image_review_service
    except Exception:  # noqa: BLE001 - 缺失 OCR 模块不该影响翻译主流程
        return ""
    target = get_settings().image_results_dir / filename
    try:
        result = image_review_service.check_local_file(target)
    except Exception as exc:  # noqa: BLE001 - OCR 失败只记日志
        _log_ocr_failure(exc)
        return ""
    if result.get("error"):
        return ""
    if result.get("hasChinese") is True:
        sample = str(result.get("chineseText") or "").strip()
        hint = f"：{sample[:40]}" if sample else ""
        return f"翻译完成，但本地 OCR 仍检出中文{hint}，发海外前请复核"
    return ""


def _log_ocr_failure(exc: BaseException) -> None:
    logging.getLogger(__name__).warning(
        "译文中文残留复查失败 type=%s：%s", type(exc).__name__, exc)


def _upload_result(db: Session, job: ImageJob) -> bool:
    """把本地结果文件传到图床，成功后写 public_url。失败不改变 job.status。

    为什么状态不动：走到这里说明图已经生成、上游已经扣费，失败的只是
    「再传一份到公网」这一步。把 status 打成 failed 会让前端把它当成可重试
    的失败任务，重试就是再花一次钱；正确的动作是留着 ready，只记 upload_error，
    由 POST /images/jobs/{id}/upload 手动重试。
    """
    # 幂等：已经有公网地址就不再传。三处接线（translate/refresh/reconcile）
    # 和手动重试接口都可能落到这里，重复上传既费时又会覆盖已写好的地址。
    if (job.public_url or "").strip():
        return True
    if not job.filename:
        job.upload_error = "任务没有结果文件"
        return False
    target = get_settings().image_results_dir / job.filename
    if not target.is_file():
        # 老库/被清理过的结果目录：留一条可读的原因，别让操作者对着空字段猜。
        job.upload_error = "结果文件不存在"
        return False
    try:
        # 延迟导入：image_upload 只在真正要传的时候才需要（它可能拉 paramiko）。
        from services import image_upload

        url = image_upload.upload_image(target)
    except Exception as exc:  # noqa: BLE001 - 上传失败只降级，绝不能掀翻翻译主流程
        # 只截断到列宽（500），且只记类型不记内容：image_upload 的异常短语里
        # 可能带服务器地址，凭据不能进库也不能进日志。
        job.upload_error = str(exc)[:500] or type(exc).__name__
        logging.getLogger(__name__).warning(
            "译文图上传图床失败 job=%s type=%s", job.id, type(exc).__name__)
        return False
    job.public_url = url
    job.uploaded_at = now_ts()
    job.upload_error = ""
    return True


def upload_job_result(db: Session, job_id: str) -> dict[str, Any]:
    """POST /images/jobs/{id}/upload：把已完成任务的结果补传到图床。

    与三处自动接线共用 _upload_result，语义完全一致；这里只额外把
    「任务不存在 / 还没生成完 / 传不上去」翻成 HTTP 语义。
    """
    job = get_job(db, job_id)
    if job.status != "ready":
        raise errors.AppError(
            errors.CONFLICT_BUSY,
            f"任务还没生成完成，不能上传（当前状态 {job.status}）",
            status=409, details={"id": job.id, "status": job.status})
    # 幂等：已经传过就直接回现有地址，不重复走 SFTP/HTTP。
    if (job.public_url or "").strip():
        return {"job": serialize(job)}
    if not _upload_result(db, job):
        raise errors.AppError(
            errors.UPSTREAM_ERROR,
            job.upload_error or "图片上传失败",
            status=502, details={"id": job.id})
    job.updated_at = now_ts()
    db.flush()
    return {"job": serialize(job)}


def translate(db: Session, payload: Mapping[str, Any],
              *, operator_id: int | None = None,
              owner_operator_id: int | None = None,
              _skip_reserve: bool = False) -> dict[str, Any]:
    """POST /images/translate：批量提交（幂等 + 预算 + 限流）。

    `operator_id` 只落在**新建**的 job 上：一行 job 代表「这张图是谁先提交的」，
    复用的行（同图同参数已存在）保留原提交人——不然重提一次就把历史提交人
    擦掉了，扣费归属反而更查不清。
    """
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise errors.validation("items 必须是非空数组", field="items")
    if len(items) > 200:
        raise errors.validation("一次最多提交 200 张图片", field="items", value=len(items))
    # 先确认密钥已配置：否则下面的下载原图只会报一个没意义的连接错误。
    # 但全部标记 keep 的批次压根不提交，不该因为没密钥就拒绝——
    # 用户可能只是想「这批都保留」，那是合法的空操作。
    _all_kept = bool(items) and all(
        isinstance(item, Mapping) and str(item.get("action") or "") == "keep"
        for item in items)
    if not _all_kept:
        _api_key()
    default_size = str(payload.get("size") or "1:1")
    if default_size not in IMAGE_SIZES:
        raise errors.validation("size 只能是 1:1 / 16:9 / 9:16", field="size", value=default_size)
    retry_ocr = payload.get("retryOcr") is True
    batch_id = str(payload.get("batchId") or "").strip()
    if len(batch_id) > 32:
        raise errors.validation("batchId 不能超过 32 个字符", field="batchId")
    if not batch_id:
        from core.utils import new_batch_id

        batch_id = new_batch_id("imgb")

    busy = (db.query(ImageJob)
            .filter(ImageJob.batch_id == batch_id,
                    ImageJob.status.in_(("queued", "submitted"))).count())
    if busy:
        raise errors.busy(f"批次 {batch_id} 还有 {busy} 个任务在处理中",
                          batchId=batch_id, running=busy)

    if retry_ocr:
        unsafe = (db.query(ImageJob)
                  .filter(ImageJob.status == "unknown",
                          ImageJob.batch_id == batch_id).count())
        if unsafe:
            raise errors.AppError(
                errors.RETRY_UNSAFE,
                f"批次 {batch_id} 有 {unsafe} 个任务结果未确认（可能已扣费），已拒绝重复提交",
                status=422, details={"ungoverned": unsafe},
            )

    results: list[dict[str, Any]] = []
    reused = 0
    accepted = 0
    pending: list[tuple[ImageJob, bytes]] = []
    prompt = translation_prompt(str(payload.get("language") or "ms"))

    limit = budget_limit(db)
    reserved_now = reserved_total(db)

    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise errors.validation(f"第 {index + 1} 项不是对象", field=f"items[{index}]")
        source = str(item.get("source") or "").strip()
        if source not in ("main", "detail", "sku"):
            raise errors.validation(f"第 {index + 1} 项的 source 只能是 main / detail / sku",
                                    field=f"items[{index}].source", value=source)
        size = str(item.get("size") or default_size)
        if size not in IMAGE_SIZES:
            raise errors.validation(f"第 {index + 1} 项的 size 无效",
                                    field=f"items[{index}].size", value=size)
        # 人工核对结论（老版 image-plan rows[].action）：
        # keep 的直接跳过，不占预算也不提交——用户在界面上逐张改的就是它。
        action = str(item.get("action") or "").strip()
        if action not in ("", "keep", "translate"):
            raise errors.validation(
                f"第 {index + 1} 项的 action 只能是 keep / translate",
                field=f"items[{index}].action", value=action)
        if action == "keep":
            results.append({"index": index, "status": "skipped",
                            "reason": "人工标记为保留，不翻译",
                            "url": str(item.get("url") or "")})
            continue
        url = str(item.get("url") or "").strip()
        raw_b64 = item.get("imageBase64")
        if not url and not raw_b64:
            raise errors.validation(f"第 {index + 1} 项必须给 url 或 imageBase64",
                                    field=f"items[{index}].url")
        raw = _decode_base64(str(raw_b64)) if raw_b64 else _fetch_source(url)
        if not raw:
            raise errors.upstream("原图内容为空")
        # 必须在算摘要之前压：摘要按上传的字节算，先算再压会让同一张图
        # 因为「原图/压后图」算出两个 job id，去重和复用全部失效。
        raw = _shrink(raw)
        # 格式必须在建 job 之前就拦：job 建出来是 queued，_submit 抛错后
        # 状态停在失败，但预扣费已经算进去，而且用户看不清是格式问题。
        if _image_type(raw) is None:
            raise errors.unprocessable(
                "图片格式须为 PNG、JPEG 或 WebP",
                details={"index": index, "url": url})
        digest = image_job_id(raw, IMAGE_MODEL, prompt, size)
        if owner_operator_id is not None:
            # A job has one product and one owner. Global content deduplication
            # would leave the next user's/product's result on somebody else's row.
            digest = sha256_hex(
                f"{digest}:{owner_operator_id}:{item.get('productId') or ''}".encode())
        existing = db.query(ImageJob).filter(ImageJob.id == digest).one_or_none()
        if (existing is not None and owner_operator_id is not None
                and existing.operator_id != owner_operator_id):
            raise errors.AppError(errors.FORBIDDEN,
                                  "该图片任务不属于当前学员，不能复用", status=403)
        if existing is not None and existing.status == "ready":
            results.append({**serialize(existing), "reused": True})
            reused += 1
            continue
        if existing is not None and existing.status == "unknown":
            # unknown = 上游结果未确认，很可能已经扣过费。不管 retry_ocr 是什么
            # 都不能重提（老版 image_translate.generate 的显式保护）：
            # 「图片请求结果未确认，可能已产生费用，不会重复扣费重试」。
            results.append({**serialize(existing), "reused": True,
                            "reason": "结果未确认，可能已产生费用；不会重复扣费重试"})
            reused += 1
            continue
        if existing is not None and existing.status in ("queued", "submitted"):
            results.append({**serialize(existing), "reused": True})
            reused += 1
            continue

        job = existing
        if job is None:
            if reserved_now + UNIT_MILLIYUAN > limit:
                raise errors.AppError(
                    errors.IMAGE_BUDGET_EXCEEDED,
                    f"图片预扣费将超过上限 {limit / 1000:.2f} 元，"
                    f"已用 {reserved_now / 1000:.2f} 元",
                    status=402,
                    details={"requiredMilliyuan": UNIT_MILLIYUAN,
                             "reservedMilliyuan": reserved_now,
                             "limitMilliyuan": limit},
                )
            reserved_now += UNIT_MILLIYUAN
            job = ImageJob(
                id=digest,
                batch_id=batch_id,
                product_id=_product_id(db, item.get("productId")),
                detail_id=str(item.get("detailId") or ""),
                source=source,
                source_url=url,
                source_sha256=sha256_hex(raw),
                status="queued",
                model=IMAGE_MODEL,
                prompt=prompt,
                size=size,
                reserved_milliyuan=UNIT_MILLIYUAN,
                message="已排队",
                attempt=0,
                operator_id=operator_id,
                created_at=now_ts(),
                updated_at=now_ts(),
            )
            db.add(job)
            db.flush()
        else:
            job.batch_id = batch_id
            job.status = "queued"
            job.message = "重新排队"
            job.attempt = int(job.attempt or 0) + 1
            job.retry_of = job.retry_of or job.id
            job.updated_at = now_ts()
            db.flush()
        pending.append((job, raw))

    if pending and not _skip_reserve:
        _reserve_slot(db)

    # 并发提交：老版 IMAGE_CONCURRENCY=3。串行提交时 10 张图要 10 个往返，
    # 每张 20~60 秒，用户就盯着转圈。只把网络请求并行化，落库仍在主线程做
    # （SQLAlchemy Session 不跨线程）。
    outcomes: dict[int, dict[str, Any] | Exception] = {}

    def _submit_guarded(raw, size, *, prompt):
        """Submit through both gates: per-user first, then global.

        ## 两层闸门（负责人 2026-11-21 口径）

        先拿该用户自己的额度（学员 50），再拿全服务器额度（500）。
        **必须先拿用户闸门再拿全局闸门**，反过来会死锁：
          用户 A 占满全局 500 张后等自己的 50 张，
          用户 B 在等全局 —— A 的 50 张永远等不到全局许可，全员卡死。
        按「先私后公」的固定顺序不会成环。

        老师没有用户闸门（`_user_submit_slot` 返回 None），
        只受 500 全局上限约束 —— 负责人原话「但是老师没有限制」。

        Waiting here only blocks this submit thread; no task is lost and
        nothing errors out.
        """
        user_slot = _user_submit_slot(operator_id)
        if user_slot is None:
            with _GLOBAL_SUBMIT_SLOTS:
                return _submit(raw, size, prompt=prompt)
        # 先私后公，顺序固定（反过来会死锁，见上面的说明）
        with user_slot, _GLOBAL_SUBMIT_SLOTS:
            return _submit(raw, size, prompt=prompt)

    if pending:
        if len(pending) == 1:
            job, raw = pending[0]
            try:
                outcomes[0] = _submit_guarded(raw, job.size, prompt=job.prompt or prompt)
            except Exception as exc:  # noqa: BLE001 - 统一在下方处理
                outcomes[0] = exc
        else:
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=IMAGE_CONCURRENCY,
                                    thread_name_prefix="img") as pool:
                futures = {
                    pool.submit(_submit_guarded, raw, job.size,
                                prompt=job.prompt or prompt): index
                    for index, (job, raw) in enumerate(pending)
                }
                for future, index in futures.items():
                    try:
                        outcomes[index] = future.result()
                    except Exception as exc:  # noqa: BLE001
                        outcomes[index] = exc

    for index, (job, _raw) in enumerate(pending):
        outcome = outcomes.get(index)
        if isinstance(outcome, Exception):
            exc = outcome
            if isinstance(exc, errors.AppError):
                job.status = ("unknown"
                              if exc.code == errors.UPSTREAM_UNKNOWN_RESULT
                              else "failed")
                message = exc.message
            else:
                # 非 AppError 一律按「结果未知」处理：可能是网络中断，
                # 上游可能已经扣费，不能标 failed 诱导用户重试。
                job.status = "unknown"
                message = f"提交异常（{type(exc).__name__}），结果未确认"
            job.message = message[:500]
            job.error = message[:500]
            job.updated_at = now_ts()
            db.flush()
            results.append({**serialize(job), "reused": False})
            continue
        outcome = outcome or {}
        if outcome["status"] == "ready" and outcome.get("image"):
            job.filename = _write_result(outcome["image"], job.id)
            job.status = "ready"
            job.result_url = f"/api/v1/images/jobs/{job.id}/file"
            job.ready_at = now_ts()
            job.message = "翻译完成"
            job.task_id = outcome.get("taskId") or job.task_id
            # 结果落盘后顺手传图床：失败只写 upload_error，不回滚 ready
            _upload_result(db, job)
            accepted += 1
        else:
            job.status = "submitted"
            job.task_id = outcome.get("taskId") or job.task_id
            job.upstream_status = outcome.get("upstreamStatus") or "processing"
            job.message = "图片任务已提交，正在查询上游结果"
            accepted += 1
        job.updated_at = now_ts()
        db.flush()
        results.append({**serialize(job), "reused": False})

    db.flush()
    state = usage(db)
    return {
        "batchId": batch_id,
        "accepted": accepted,
        "reused": reused,
        "jobs": results,
        "budget": state["budget"],
        "rate": {key: state[key] for key in ("usedThisMinute", "usedThisHour",
                                             "usedToday", "budgets")},
        "jobsUrl": f"/api/v1/images/jobs?batchId={batch_id}",
    }


def refresh(db: Session, job_id: str) -> dict[str, Any]:
    """GET /images/jobs/{id}?refresh=true：向上游查询任务状态并落库。"""
    job = get_job(db, job_id)
    if job.status not in ("submitted", "queued") or not job.task_id:
        return {"job": serialize(job), "progress": _progress(job)}
    outcome = query_task(str(job.task_id))
    if outcome["status"] == "ready" and outcome.get("image"):
        job.filename = _write_result(outcome["image"], job.id)
        job.status = "ready"
        job.result_url = f"/api/v1/images/jobs/{job.id}/file"
        job.ready_at = now_ts()
        job.message = "翻译完成"
        job.upstream_status = outcome.get("upstreamStatus") or "succeeded"
        # 同上：公网地址是独立一次尝试，失败不改变 ready
        _upload_result(db, job)
        # 译文中文残留复查（老版 image_review 的检查）：只标注，不改状态、不阻断，
        # 以免 OCR 不可用时把已经扣费的成果判成失败。
        residue = _chinese_residue(job.filename)
        if residue:
            job.message = residue
    elif outcome["status"] == "failed":
        job.status = "failed"
        job.message = str(outcome.get("message") or "上游任务失败")[:500]
        job.upstream_status = outcome.get("upstreamStatus") or "failed"
    else:
        job.upstream_status = outcome.get("upstreamStatus") or "processing"
        job.message = "图片任务已提交，正在查询上游结果"
    job.updated_at = now_ts()
    db.flush()
    return {"job": serialize(job), "progress": _progress(job)}


def _progress(job: ImageJob) -> dict[str, Any] | None:
    if job.status not in ("queued", "submitted"):
        return None
    elapsed = max(0.0, now_ts() - float(job.created_at or now_ts()))
    return {"polledAt": now_ts(), "elapsedSeconds": round(elapsed, 1),
            "estimateSeconds": 25}


def result_file(db: Session, job_id: str) -> tuple[Path, str]:
    job = get_job(db, job_id)
    if job.status != "ready":
        raise errors.not_found(f"任务 {job_id} 还没有结果文件（当前状态 {job.status}）",
                               id=job_id)
    if not job.filename:
        raise errors.AppError(errors.RESULT_FILE_MISSING,
                              "任务标记为完成但本地文件缺失，禁止自动重新提交",
                              status=404, details={"id": job_id})
    target = get_settings().image_results_dir / job.filename
    if not target.is_file():
        raise errors.AppError(errors.RESULT_FILE_MISSING,
                              "任务标记为完成但本地文件缺失，禁止自动重新提交",
                              status=404, details={"id": job_id})
    return target, job.filename


def reconcile(db: Session, job_ids: Iterable[str] | None = None) -> dict[str, Any]:
    """进程重启后重新查上游：只更新状态，绝不重复提交。"""
    query = db.query(ImageJob).filter(ImageJob.status.in_(("submitted", "queued")),
                                      ImageJob.task_id.is_not(None))
    if job_ids:
        wanted = [str(item) for item in job_ids]
        query = query.filter(ImageJob.id.in_(wanted))
    checked = 0
    ready = 0
    still = 0
    failed = 0
    for job in query.all():
        checked += 1
        try:
            outcome = query_task(str(job.task_id))
        except errors.AppError:
            still += 1
            continue
        if outcome["status"] == "ready" and outcome.get("image"):
            job.filename = _write_result(outcome["image"], job.id)
            job.status = "ready"
            job.result_url = f"/api/v1/images/jobs/{job.id}/file"
            job.ready_at = now_ts()
            job.message = "翻译完成"
            # 进程重启后的补账同样要补公网地址（_upload_result 内部幂等）
            _upload_result(db, job)
            ready += 1
        elif outcome["status"] == "failed":
            job.status = "failed"
            job.message = str(outcome.get("message") or "上游任务失败")[:500]
            failed += 1
        else:
            still += 1
        job.updated_at = now_ts()
        db.flush()
    return {"checked": checked, "ready": ready, "pending": still, "failed": failed}
