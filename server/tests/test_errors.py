"""错误信封契约：code 稳定、status 正确、结构永远是 {"error": {...}}。

前端是按 ``error.code`` 分支的（见 core/errors.py 顶部那段说明），所以两件事
一旦破掉就是线上事故：

  1. **code 大小写漂移。** 有人写了 ``AppError("image_key_missing", ...)``，
     后端日志里看着一切正常，前端 ``switch (code)`` 全落到 default，
     用户看到的是「未知错误」。
  2. **信封结构不统一。** 有的接口返回 ``{"detail": ...}``、有的返回
     ``{"error": ...}``，前端得写两套解析，迟早漏一个。

这里用真实 HTTP 请求验，不是直接调函数：``main.py`` 里的异常处理器挂在 app 上，
只有走一遍请求栈才能证明「工厂函数产出的 AppError 真的会被转成信封」。
"""
from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from core import errors

SERVER_ROOT = Path(__file__).resolve().parents[1]

# AppError.code 的合法形态：大写字母开头，只含大写字母/数字/下划线
CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")

# 所有工厂函数 → 产出的 AppError。新增工厂函数忘了加进来时，
# test_all_factories_are_covered 会红（见下）。
FACTORIES: dict[str, Callable[[], errors.AppError]] = {
    "validation": lambda: errors.validation("字段不合法", field="x"),
    "unprocessable": lambda: errors.unprocessable("无法处理"),
    "not_found": lambda: errors.not_found("资源不存在"),
    "duplicate_name": lambda: errors.duplicate_name("名字重复"),
    "busy": lambda: errors.busy("正在执行"),
    "read_only": errors.read_only,
    "forbidden": lambda: errors.forbidden("没有权限做这个", reason="not_admin"),
    "upstream": lambda: errors.upstream("上游失败", code="500"),
    "upstream_timeout": errors.upstream_timeout,
    "upstream_rate_limit": errors.upstream_rate_limit,
    "not_connected": errors.not_connected,
    # 多人协作：编辑期间被他人改过（乐观锁冲突）
    "revision_conflict": lambda: errors.revision_conflict(
        "这件商品已被修改", expected=1, actual=2, hint="刷新后重试"),
}


@pytest.fixture(scope="module")
def app():
    """导入 main.app，并建好表。

    ``init_db()`` 平时由 lifespan 里的启动钩子调用，而 ``TestClient(app)``
    不进入 lifespan（除非 ``with``），所以不建表的话第一条查库的路由就会
    ``no such table``。这里显式建表——建的是 conftest 钉住的临时库。
    """
    import main
    from db.session import init_db

    init_db()
    return main.app


@pytest.fixture(scope="module")
def client(app):
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(scope="module")
def auth_headers() -> dict[str, str]:
    """独立老师 token，避免把首次接管凭据当成业务身份。"""
    from db.session import SessionLocal, init_db
    from services import operator_service

    init_db()
    db = SessionLocal()
    try:
        _operator, token = operator_service.create(
            db, f"errors-teacher-{uuid.uuid4().hex[:8]}",
            role=operator_service.ROLE_TEACHER)
        db.commit()
        return {"Authorization": f"Bearer {token}"}
    finally:
        db.close()


@pytest.fixture
def boom_route(app):
    """在 app 上临时挂一条「必抛 AppError」的路由，用完摘掉。

    为什么不直接调 handler：我们要验的是 FastAPI 真的把异常交给了
    main.py 注册的处理器，中间还隔着 ExceptionMiddleware。
    """
    router = APIRouter(prefix="/api/v1/_contract")
    holder: dict[str, Any] = {}

    @router.get("/boom")
    def _boom() -> dict[str, Any]:
        raise holder["exc"]

    before = len(app.router.routes)
    app.include_router(router)
    try:
        yield holder
    finally:
        # 摘干净：别的测试（比如 test_smoke 的重复路由检查）不该看到它
        del app.router.routes[before:]


def test_validation_factory() -> None:
    exc = errors.validation("x")
    assert exc.code == "VALIDATION_ERROR"
    assert exc.status == 400


def test_not_found_factory() -> None:
    exc = errors.not_found("x")
    assert exc.code == "NOT_FOUND"
    assert exc.status == 404


def test_read_only_factory() -> None:
    exc = errors.read_only()
    assert exc.code == "READ_ONLY_MODE"
    assert exc.status == 403


@pytest.mark.parametrize("name", sorted(FACTORIES), ids=sorted(FACTORIES))
def test_factory_code_is_upper_snake(name: str) -> None:
    """每个工厂产出的 code 都必须匹配 ^[A-Z][A-Z0-9_]*$。"""
    exc = FACTORIES[name]()
    assert CODE_PATTERN.match(exc.code), (
        f"errors.{name}() 产出的 code={exc.code!r} 不是 UPPER_SNAKE；"
        "前端是按 code 精确匹配的，小写/驼峰会导致它落到 default 分支"
    )
    assert isinstance(exc.message, str) and exc.message, "message 不能为空"
    assert 400 <= exc.status <= 599, f"{name} 的 status={exc.status} 不是合法 HTTP 错误码"


@pytest.mark.parametrize("name", sorted(FACTORIES), ids=sorted(FACTORIES))
def test_factory_envelope_over_http(name: str, client: TestClient, auth_headers,
                                    boom_route) -> None:
    """工厂函数 → HTTP 响应：状态码与信封结构都必须对得上。"""
    exc = FACTORIES[name]()
    boom_route["exc"] = exc

    response = client.get("/api/v1/_contract/boom", headers=auth_headers)
    assert response.status_code == exc.status, (
        f"errors.{name}() 声明 status={exc.status}，实际返回 {response.status_code}")

    body = response.json()
    assert "error" in body, f"信封缺 error 字段：{body}"
    envelope = body["error"]
    assert envelope["code"] == exc.code
    assert envelope["message"] == exc.message
    assert isinstance(envelope["details"], dict), (
        "details 必须是对象：前端统一按 error.details.xxx 取值，"
        f"实际 {type(envelope['details']).__name__}")
    # requestId 由中间件注入，排查线上问题全靠它
    assert envelope.get("requestId"), "信封里没有 requestId"


def test_error_code_constants_are_upper_snake() -> None:
    """core/errors.py 里所有错误码常量都要是 UPPER_SNAKE。"""
    bad = [f"{name}={value!r}" for name, value in vars(errors).items()
           if isinstance(value, str) and name.isupper() and not CODE_PATTERN.match(value)]
    assert not bad, "错误码常量不是 UPPER_SNAKE：" + ", ".join(bad)


def test_literal_codes_in_source_are_upper_snake() -> None:
    """全项目直接写死的 code 字面量也要合规。

    有些地方绕过常量直接 ``AppError("IMAGE_KEY_MISSING", ...)``，这些不会出现在
    ``vars(errors)`` 里，只能扫源码。这是唯一能挡住「随手写个小写 code」的办法。
    """
    pattern = re.compile(r'AppError\(\s*["\']([A-Za-z0-9_]+)["\']')
    offenders: list[str] = []
    for path in _python_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in pattern.finditer(text):
            code = match.group(1)
            if not CODE_PATTERN.match(code):
                line = text[: match.start()].count("\n") + 1
                offenders.append(f"{path.relative_to(SERVER_ROOT)}:{line} -> {code!r}")
    assert not offenders, "以下地方直接写死的错误码不是 UPPER_SNAKE：\n  " + "\n  ".join(offenders)


def test_all_factories_are_covered() -> None:
    """新增的 errors.* 工厂函数必须被 FACTORIES 覆盖。

    漏覆盖的后果：新工厂可能产出一个前端没见过的 code，而这里全绿。
    """
    missing = [
        name for name, value in vars(errors).items()
        if callable(value) and not name.startswith("_")
        and name not in {"AppError"}
        and value.__module__ == errors.__name__
        and name not in FACTORIES
    ]
    assert not missing, f"新工厂函数没加进 FACTORIES：{missing}"


# --- 真实路由上的信封（不依赖临时路由） ------------------------------------
def test_unauthenticated_request_returns_envelope(client: TestClient) -> None:
    """没带 token 打受保护接口：401 + AUTH_REQUIRED 信封，而不是 FastAPI 的 detail。"""
    response = client.get("/api/v1/auth/accounts")
    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == errors.AUTH_REQUIRED
    assert body["error"]["requestId"]


def test_unknown_path_returns_envelope(client: TestClient, auth_headers) -> None:
    """404 也要走统一信封：Starlette 的默认 404 是 {"detail": "Not Found"}。"""
    response = client.get("/api/v1/definitely-not-a-route", headers=auth_headers)
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == errors.NOT_FOUND
    assert "detail" not in body


def test_real_not_found_path_returns_envelope(client: TestClient, auth_headers) -> None:
    """真实业务路径的 not_found：仓库里没有这号商品 → 404 NOT_FOUND。"""
    response = client.get("/api/v1/warehouse/999999999", headers=auth_headers)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == errors.NOT_FOUND


def test_validation_error_over_http(client: TestClient, auth_headers) -> None:
    """参数校验失败统一成 400 VALIDATION_ERROR（不是 FastAPI 默认的 422 detail）。"""
    response = client.get("/api/v1/collect-box", params={"page": 0}, headers=auth_headers)
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == errors.VALIDATION_ERROR
    assert body["error"]["details"]["errors"], "校验失败必须带上逐字段的 errors 明细"


def _python_files() -> list[Path]:
    """扫源码的范围：只扫产品代码，_meas/dist/build 里的探针与产物不算。"""
    roots = ("api", "services", "models", "schemas", "core", "db")
    files: list[Path] = [SERVER_ROOT / "main.py"]
    for root in roots:
        files.extend(sorted((SERVER_ROOT / root).rglob("*.py")))
    return [path for path in files if path.is_file()]
