"""奥皇上品 · 后端入口（FastAPI）。

启动：python main.py（读 settings.api_port，被占用则在 api_port_max 内递增探测）
      或 python -m uvicorn main:app --port 8812（单 worker：SQLite 单写者）
Base： http://127.0.0.1:8812/api/v1
探活： GET /api/system/health 与 GET /api/v1/system/health（均免鉴权）
日志： stdout + <data_dir>/logs/server.log（UTF-8，见 configure_logging）
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import socket
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from api import API_PREFIX, router
from core import errors
from core.config import get_settings
from core.constants import APP_NAME, APP_VERSION, SLOW_REQUEST_MS
from core.utils import new_request_id, redact
from db.session import init_db

logger = logging.getLogger("aohuang.api")
ALLOWED_ORIGINS = [
    "http://127.0.0.1:5173",
    "http://localhost:5173",
    "file://",
    "null",
]

# --- 日志 ------------------------------------------------------------------
# 监听地址不再写死。为什么：这个工具本身就是多人共用的（一套仓库、多个运营），
# 而 HOST 固定 "127.0.0.1" 意味着别人连不上，只能每人在自己机器上装一份 ——
# 于是「两个人同时改同一件商品」变成必然，而且没有共享的库可查。
# 取值规则在 core/config.py 的 Settings.listen_host（显式 bind_host 优先，
# 其次 shared_mode → 0.0.0.0，最后回环 127.0.0.1），run_server.py 用的是同一个属性，
# 保证源码启动和打包启动算出同一个地址。
BOOT_SETTINGS = get_settings()
HOST = BOOT_SETTINGS.listen_host
LOG_FILE_NAME = "server.log"
LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
# handler 上打的标记：多次 create_app / 多次 lifespan 时不重复挂载
_HANDLER_MARK = "_aohuang_handler"


def configure_logging(level: int = logging.INFO) -> Path:
    """配置日志：同时写 stdout 与 logs/server.log（UTF-8），幂等。

    Electron 生产模式用 ``spawn(stdio="ignore")`` 丢弃 stdout，日志必须落盘，
    否则打包后任何 warning 都看不到。
    """
    settings = get_settings()
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = settings.logs_dir / LOG_FILE_NAME

    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT)
    root = logging.getLogger()
    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)

    file_handler: logging.Handler | None = None
    if not any(getattr(handler, _HANDLER_MARK, False) for handler in root.handlers):
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        setattr(stream_handler, _HANDLER_MARK, "stream")
        root.addHandler(stream_handler)
        try:
            file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
            file_handler.setFormatter(formatter)
            setattr(file_handler, _HANDLER_MARK, "file")
            root.addHandler(file_handler)
        except OSError as exc:
            # 日志落盘只是辅助：被占用或没权限时降级为只输出 stdout，
            # 不能让一个可有可无的 handler 把整个服务挡在启动之外。
            file_handler = None
            root.warning("无法写日志文件 %s（%s），本次只输出到控制台",
                         log_file, exc)
    else:
        for handler in root.handlers:
            if getattr(handler, _HANDLER_MARK, "") == "file":
                file_handler = handler
                break

    # uvicorn 自己的 logger 设了 propagate=False，不会流到 root，单独补文件 handler。
    # 只补文件 handler，避免与 uvicorn 自带的 console handler 重复输出。
    if file_handler is not None:
        for name in ("uvicorn", "uvicorn.access"):
            target = logging.getLogger(name)
            if not any(getattr(handler, _HANDLER_MARK, False)
                       for handler in target.handlers):
                target.addHandler(file_handler)

    return log_file


BAD_JSON_MESSAGE = "请求体不是合法 JSON"
# FastAPI/Starlette 把 body 解析失败包装成 type=json_invalid 的 RequestValidationError
_JSON_BODY_ERROR_TYPES = frozenset({"json_invalid", "json_decode"})


def _is_json_body_error(exc: RequestValidationError) -> bool:
    """判断是不是「请求体不是合法 JSON」，而不是普通参数校验失败。"""
    return any(str(item.get("type") or "") in _JSON_BODY_ERROR_TYPES
               for item in exc.errors() or [])


def _error_response(code: str, message: str, status: int, request_id: str,
                    details: Any = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message,
                           "details": details or {}, "requestId": request_id}},
    )


def _reconcile_once() -> dict:
    """同步跑一轮图片任务回收（给 to_thread 用，自己管事务）。"""
    from db.session import session_scope
    from services import image_service

    with session_scope() as db:
        result = image_service.reconcile(db)
        db.commit()
        return result


def _run_due_once() -> list:
    """同步跑一轮到期定时任务（给 to_thread 用，自己管事务）。"""
    from db.session import session_scope
    from services import schedule_service

    with session_scope() as db:
        summaries = schedule_service.run_due(db)
        db.commit()
        return summaries


async def _image_reconcile_loop(stop: asyncio.Event) -> None:
    """后台轮询未完成的图片任务。

    图片翻译是同步调用，拿不到结果时会落 submitted + task_id。没有这个循环，
    那些任务会永远停在 submitted，前端只能一直转圈。
    """

    while not stop.is_set():
        try:
            # 必须丢到线程里跑：reconcile 会同步发 HTTP 请求，
            # 直接在事件循环上调会把整个服务钉住（所有 API 一起无响应）。
            result = await asyncio.to_thread(_reconcile_once)
            if result.get("ready") or result.get("failed"):
                logger.info("图片任务回收：%s", result)
        except Exception as exc:  # noqa: BLE001 - 轮询失败不能拖垮服务
            logger.exception("图片任务回收失败 type=%s：%s", type(exc).__name__, exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=20)
        except TimeoutError:
            continue


async def _schedule_loop(stop: asyncio.Event) -> None:
    """每分钟扫一次到期的定时发布任务。

    异常必须带堆栈落盘（logger.exception），否则定时发布静默失败无法排查；
    run_due 内部已把 errorType 写进任务 last_result，这里只负责不吞堆栈。
    """
    from services import schedule_service

    logger.info("定时发布扫描已启动，间隔 60s")
    while not stop.is_set():
        try:
            schedule_service.mark_scheduler_tick()
            # run_due 内部按 1.5s/账号节流调上游，200 件能跑五分钟。
            # 放到线程里，事件循环才不会被一次定时发布冻住。
            summaries = await asyncio.to_thread(_run_due_once)
            if summaries:
                logger.info("定时发布执行：%s", summaries)
        except Exception as exc:  # noqa: BLE001 - 扫描失败不能拖垮服务
            logger.exception("定时发布扫描失败 type=%s：%s", type(exc).__name__, exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=60)
        except TimeoutError:
            continue


def shared_mode_warning(settings_obj=None) -> str:
    """共享模式下必须打出来的启动告示；只绑回环时返回空串。

    为什么不能只写注释：`shared_mode` 以前是个只被「自动更新」读一下的开关，
    打开它的人以为自己开了「多人模式」，其实后端仍然只听 127.0.0.1 ——
    现在它真的把后端开到所有网卡上了，这是一次**暴露面变化**，必须在启动
    日志里说清楚，否则没人知道同一局域网里其他人能直接连上来。

    文案写死成一句完整的话（而不是几个 %s 拼出来），是因为它会被人截图发到
    群里问「这是什么意思」；能一次读懂比好看重要。
    """
    settings_obj = settings_obj or get_settings()
    host = settings_obj.listen_host
    if host in ("127.0.0.1", "localhost", "::1"):
        return ""
    source = ("AOHUANG_SHARED_MODE=1" if settings_obj.shared_mode
              else "AOHUANG_BIND_HOST")
    return (
        "共享模式已开启：后端已对所有网卡开放，请确认局域网可信 + 每个人有自己的操作员 token"
        f"（监听 {host}:{int(settings_obj.api_port)}，来源 {source}）。"
        "同一网络内任何人只要拿到 token 就能改仓库和发布商品；"
        "如果只是自己用，请把 AOHUANG_SHARED_MODE 关掉。"
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    log_file = configure_logging()
    settings = get_settings()
    settings.ensure_dirs()
    init_db()
    from db.session import SessionLocal
    from services.operator_service import migrate_legacy_tokens

    with SessionLocal() as db:
        migrate_legacy_tokens(db)
        db.commit()
    _seed_words()
    # 首次启动就把本机 token 落盘。require_token 是惰性调用 local_secret()，
    # 而「没带 token」的请求会先在 header 检查处 401、根本走不到生成那一步 ——
    # 全新安装后用户没有任何地方能拿到 token（审查新发现）。
    settings.local_secret()
    # 暴露面变化必须显眼：用 warning 级别，日志文件里也是黄字。
    notice = shared_mode_warning(settings)
    if notice:
        logger.warning(notice)
    logger.info("后端启动完成 data_dir=%s log_file=%s token_file=%s",
                settings.data_dir, log_file, settings.secret_path)
    stop = asyncio.Event()
    worker = asyncio.create_task(_image_reconcile_loop(stop))
    scheduler = asyncio.create_task(_schedule_loop(stop))
    try:
        yield
    finally:
        stop.set()
        worker.cancel()
        scheduler.cancel()
        for task in (worker, scheduler):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        logger.info("后端关闭")


def _seed_words() -> None:
    """首次启动写入内置违规词（幂等）。"""
    from db.session import session_scope
    from services import word_service

    try:
        with session_scope() as db:
            word_service.seed(db)
            db.commit()
    except Exception as exc:  # noqa: BLE001 - 种子失败不应阻塞启动
        logger.warning("写入内置违规词失败：%s", exc)


settings = get_settings()

app = FastAPI(
    title=f"{APP_NAME} API",
    version=APP_VERSION,
    docs_url="/api/docs" if settings.dev else None,
    redoc_url=None,
    openapi_url="/api/openapi.json" if settings.dev else None,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_origin_regex=r"^(file://.*|http://(127\.0\.0\.1|localhost):\d+)$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-Id"],
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    """requestId 注入 + 时长记录 + 请求体上限 + 统一异常信封。"""
    request_id = request.headers.get("X-Request-Id") or new_request_id()
    request.state.request_id = request_id
    started = time.perf_counter()
    try:
        # 全局请求体上限（core/config.py 的 max_body_bytes，默认 40 MB）。
        # 这里读 Content-Length 而不是读 body：读到超限才拒绝，内存已经吃进去了，
        # 而声明超限的请求连读都不用读。没有 Content-Length 的（chunked）放行，
        # 由各路由自己兜——星形lette 在那种情况下也只能边读边判，代价一样。
        #
        # 为什么必须有：图片 base64 走 JSON 时单件商品可达十几 MB，而
        # uvicorn 默认不限制请求体。没有这道闸，一个手滑的脚本或者共享部署里
        # 一台机器发出的畸形请求就能把后端的内存吃光，界面上表现为「整个软件卡死」，
        # 而日志里只有一条 MemoryError。
        limit = get_settings().max_body_bytes
        declared = request.headers.get("content-length")
        if limit > 0 and declared:
            try:
                too_big = int(declared) > limit
            except ValueError:
                raise errors.validation("请求长度不正确", field="Content-Length") from None
            if too_big:
                logger.warning("请求体超限 requestId=%s path=%s declared=%s limit=%s",
                               request_id, request.url.path, declared, limit)
                response = _error_response(
                    errors.VALIDATION_ERROR,
                    f"请求体过大：上限 {limit // (1024 * 1024)} MB",
                    413, request_id,
                    {"declaredBytes": int(declared), "limitBytes": limit})
                response.headers["X-Request-Id"] = request_id
                return response
        response = await call_next(request)
    except errors.AppError as exc:
        response = _error_response(exc.code, exc.message, exc.status, request_id,
                                   exc.details)
    except RequestValidationError as exc:
        if _is_json_body_error(exc):
            logger.warning("请求体不是合法 JSON requestId=%s path=%s",
                           request_id, request.url.path)
            response = _error_response(errors.BAD_JSON, BAD_JSON_MESSAGE, 400,
                                       request_id,
                                       {"errors": _validation_details(exc)})
        else:
            response = _error_response(
                errors.VALIDATION_ERROR, _validation_message(exc), 400, request_id,
                {"errors": _validation_details(exc)})
    except json.JSONDecodeError as exc:
        logger.warning("请求体不是合法 JSON requestId=%s path=%s err=%s",
                       request_id, request.url.path, exc)
        response = _error_response(errors.BAD_JSON, BAD_JSON_MESSAGE, 400, request_id)
    except StarletteHTTPException as exc:
        code = {404: errors.NOT_FOUND, 405: errors.VALIDATION_ERROR,
                403: errors.FORBIDDEN_ORIGIN}.get(exc.status_code, errors.INTERNAL_ERROR)
        response = _error_response(code, str(exc.detail or "请求失败"),
                                   exc.status_code, request_id)
    except Exception as exc:  # noqa: BLE001 - 兜底，响应不含堆栈
        # 异常类型与堆栈只进日志；用户可见 message 不带内部类名
        logger.exception("未捕获异常 type=%s requestId=%s path=%s",
                         type(exc).__name__, request_id, request.url.path)
        response = _error_response(
            errors.INTERNAL_ERROR,
            "服务器内部错误，请把 requestId 反馈给开发者",
            500, request_id)
    response.headers["X-Request-Id"] = request_id
    elapsed = (time.perf_counter() - started) * 1000
    if elapsed > SLOW_REQUEST_MS:
        logger.info("慢请求 %s %s %.0fms requestId=%s", request.method,
                    request.url.path, elapsed, request_id)
    return response


def _validation_message(exc: RequestValidationError) -> str:
    first = (exc.errors() or [{}])[0]
    loc = ".".join(str(part) for part in (first.get("loc") or []) if part not in ("body", "query"))
    message = str(first.get("msg") or "参数不合法")
    return f"参数校验失败：{loc} {message}".strip()


def _validation_details(exc: RequestValidationError) -> list[dict[str, Any]]:
    rows = []
    for item in exc.errors() or []:
        rows.append({
            "field": ".".join(str(part) for part in (item.get("loc") or [])
                              if part not in ("body", "query")),
            "message": redact(str(item.get("msg") or "")),
            "type": item.get("type"),
        })
    return rows


def _validation_field(exc: RequestValidationError) -> str:
    first = (exc.errors() or [{}])[0]
    return ".".join(str(part) for part in (first.get("loc") or [])
                    if part not in ("body", "query"))


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request,
                                   exc: RequestValidationError) -> JSONResponse:
    """参数校验失败统一成 400 VALIDATION_ERROR（而不是 FastAPI 默认的 422 detail）。

    请求体不是合法 JSON 单独归到 BAD_JSON，不与普通参数校验混用同一个码。
    """
    request_id = getattr(request.state, "request_id", "") or new_request_id()
    if _is_json_body_error(exc):
        logger.warning("请求体不是合法 JSON requestId=%s path=%s",
                       request_id, request.url.path)
        return _error_response(errors.BAD_JSON, BAD_JSON_MESSAGE, 400, request_id,
                               {"errors": _validation_details(exc)})
    return _error_response(errors.VALIDATION_ERROR, _validation_message(exc), 400,
                           request_id, {"errors": _validation_details(exc),
                                        "field": _validation_field(exc)})


@app.exception_handler(errors.AppError)
async def app_error_handler(request: Request, exc: errors.AppError) -> JSONResponse:
    request_id = getattr(request.state, "request_id", "") or new_request_id()
    response = _error_response(exc.code, exc.message, exc.status, request_id, exc.details)
    if exc.code == errors.RATE_LIMITED:
        response.headers["Retry-After"] = str(
            max(1, int((exc.details or {}).get("retryAfterSeconds", 1)))
        )
    return response


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    request_id = getattr(request.state, "request_id", "") or new_request_id()
    code = {404: errors.NOT_FOUND, 405: errors.VALIDATION_ERROR}.get(
        exc.status_code, errors.INTERNAL_ERROR)
    return _error_response(code, str(exc.detail or "请求失败"), exc.status_code, request_id)


# --- 路由挂载 -------------------------------------------------------------
app.include_router(router, prefix=API_PREFIX)


@app.get("/api/system/health", tags=["系统"], summary="健康检查（Electron 探活别名）")
def health_alias() -> dict[str, Any]:
    from db.session import SessionLocal
    from services import system_service

    db = SessionLocal()
    try:
        state = system_service.db_state(db)
    finally:
        db.close()
    return system_service.health(db_ok=state == "ok", db_state=state)


@app.get("/", include_in_schema=False)
def root() -> dict[str, Any]:
    return {
        "name": APP_NAME,
        "version": APP_VERSION,
        "api": API_PREFIX,
        "health": "/api/system/health",
        "docs": "/api/docs" if get_settings().dev else None,
    }


def log_path() -> Path:
    return get_settings().logs_dir / LOG_FILE_NAME


# --- 端口探测与启动入口 ----------------------------------------------------
def _port_available(host: str, port: int) -> bool:
    """真 bind 一次判断端口是否可用（Windows 上不能加 SO_REUSEADDR，
    否则已被占用的端口也能 bind 成功，探测会失效）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def pick_port(host: str = "") -> int:
    """从 settings.api_port 起逐个探测到 api_port_max，返回第一个可用端口。

    全被占用则记日志并退出，不再默默绑一个占不上的端口。

    `host` 默认取实际监听地址（不是写死的回环）：绑 0.0.0.0 时，
    「127.0.0.1 上能绑」和「0.0.0.0 上能绑」不是一回事——回环空闲但通配地址
    被别的进程（例如另一份实例）占着，探测会给出错误结论，然后 uvicorn 在
    真正的 run() 里 bind 失败退出。
    """
    settings = get_settings()
    target = host or settings.listen_host
    first = int(settings.api_port)
    last = max(int(settings.api_port_max), first)
    for port in range(first, last + 1):
        if _port_available(target, port):
            if port != first:
                logger.warning("端口 %s 已被占用，改用 %s（探测范围 %s–%s）",
                               first, port, first, last)
            return port
        logger.warning("端口 %s 已被占用，继续探测下一个", port)
    logger.error("端口 %s–%s 全部被占用，后端无法启动：请释放其中一个端口后重试",
                 first, last)
    raise SystemExit(2)


def run() -> None:
    """本地启动入口：配置日志 → 探测端口 → 起 uvicorn（单 worker）。"""
    log_file = configure_logging()
    settings = get_settings()
    settings.ensure_dirs()
    host = settings.listen_host
    notice = shared_mode_warning(settings)
    if notice:
        logger.warning(notice)
    port = pick_port(host)
    logger.info("后端监听 host=%s port=%s（探测范围 %s–%s）data_dir=%s log_file=%s",
                host, port, settings.api_port, settings.api_port_max,
                settings.data_dir, log_file)
    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run()
