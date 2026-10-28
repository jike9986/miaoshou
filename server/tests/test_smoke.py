"""不依赖探针的独立冒烟测试。

这里只放 ``_meas/`` 里**没有**的检查——也就是「新增价值」。已经由探针覆盖的
东西（业务规则、上游交互、入仓流程）不在这里重复一遍，否则失败时还得分辨
是产品坏了还是测试自己写歪了。

这一组盯的是「结构性事故」：路由漏挂前缀、同一路由被注册两次、版本号只改了
三个地方漏了第四个。这类问题不会让任何一条业务用例变红，但上线就是整片 404
或者「永远提示有更新」。
"""
from __future__ import annotations

import importlib
import json
import pkgutil
import re
from pathlib import Path

import pytest

SERVER_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_ROOT.parent

# health 别名与根路径是唯一允许不带 /api/v1 前缀的两条：
#   · /api/system/health —— Electron 探活写死的地址，历史包袱，不能改；
#   · / —— 给 curl 看服务名的根路由。
ALLOWED_UNPREFIXED = {"/", "/api/system/health"}


@pytest.fixture(scope="module")
def app():
    """导入 main.app；数据目录已由 conftest 钉到临时目录。"""
    module = importlib.import_module("main")
    return module.app


def _route_table(app) -> list[tuple[str, str]]:
    """(path, method) 全表。HEAD 不计：Starlette 会给每个 GET 自动补 HEAD。"""
    rows: list[tuple[str, str]] = []
    for route in app.routes:
        path = getattr(route, "path", "")
        methods = sorted(getattr(route, "methods", []) or [])
        for method in methods:
            if method == "HEAD":
                continue
            rows.append((path, method))
    return rows


def test_app_imports_and_has_routes(app) -> None:
    """app 能导入，且路由数量在合理量级。

    阈值 100 是「少了一大块」的报警线：现在有 126 条，删掉一整个模块
    （比如 20 条 warehouse）才会掉到 100 以下。卡太死会让正常增删路由
    变成「改测试」，那是测试在拖后腿。
    """
    assert len(app.routes) > 100, f"路由只有 {len(app.routes)} 条，疑似某个模块没挂上"


def test_all_routes_under_api_prefix(app) -> None:
    """所有业务路由都必须挂在 /api/v1 下。

    漏挂前缀的后果不是 404 那么明显：``api/__init__.py`` 用 include_router
    挂了一堆子路由，任何一个模块自己写 ``prefix="/xxx"`` 而不带 /api/v1，
    前端拿到的地址就会少一层，本地开发（vite 代理 /api）能跑、打包后全 404。
    """
    bad = sorted({path for path, _ in _route_table(app)
                  if not path.startswith("/api/v1") and path not in ALLOWED_UNPREFIXED})
    assert not bad, (
        "以下路由没有 /api/v1 前缀（新增模块时漏挂？）：\n  " + "\n  ".join(bad)
    )


def test_no_duplicate_route(app) -> None:
    """同一个 (path, method) 只能注册一次。

    FastAPI/Starlette 对重复路由**不报错**：请求永远命中先注册的那条，
    后注册的静默失效。表现是「代码明明改了，接口行为没变」——这类 bug
    靠读代码几乎发现不了，只能靠这条。

    注意 ``@router.api_route("/price-preview", methods=["GET","PUT",...])``
    这种「同路径多方法」是合法的：分组键是 (path, method)，不是 path。
    warehouse.py 里 price-preview 有两条装饰器（一条 api_route 吞
    GET/PUT/PATCH/DELETE 只为回一句「请用 POST」，一条真正的 POST），
    methods 不相交，所以不会被这条判成重复。
    """
    seen: dict[tuple[str, str], int] = {}
    for path, method in _route_table(app):
        seen[(path, method)] = seen.get((path, method), 0) + 1
    dups = sorted(f"{method} {path}（注册 {count} 次）"
                  for (path, method), count in seen.items() if count > 1)
    assert not dups, "重复注册的路由（后注册的会静默失效）：\n  " + "\n  ".join(dups)


def test_every_router_has_prefix_and_tags() -> None:
    """每个 APIRouter 都必须有 prefix 和 tags。

    没有 tags 的路由在 /api/docs 里全挤在 default 分组，运维排查时找不到；
    没有 prefix 则意味着这个模块的路径是手写在每个装饰器里的，改一次前缀
    要改几十处，迟早漏一个。

    只查 ``api/`` 下的模块：``api/__init__.py`` 里的 ``_guarded`` 是纯聚合
    容器（它只 include 子路由，自己不带路径），不适用这条规则。
    """
    offenders: list[str] = []
    for module in pkgutil.iter_modules([str(SERVER_ROOT / "api")]):
        if module.name.startswith("_"):
            continue
        mod = importlib.import_module(f"api.{module.name}")
        for name, value in vars(mod).items():
            if not name.endswith("router") or name.startswith("_"):
                continue
            if not hasattr(value, "prefix") or not hasattr(value, "routes"):
                continue
            missing = []
            if not value.prefix:
                missing.append("prefix")
            if not value.tags:
                missing.append("tags")
            if missing:
                offenders.append(f"api/{module.name}.py:{name} 缺 {' / '.join(missing)}")
    assert not offenders, "\n  ".join(["APIRouter 缺 prefix 或 tags："] + offenders)


def test_language_domains_match() -> None:
    """``SUPPORTED_LANGUAGES`` 必须覆盖 ``SITE_LANGUAGES`` 里出现的每一个值。

    constants.py 里那段注释记着一次真实事故：印尼站（ID → id）因为白名单漏了
    ``id``，该站所有商品发布时被「语言只能是 …」直接 400 拒掉。这条把那次事故
    固化成断言，以后加站点（比如新开 PH → fil）漏加白名单会当场红。
    """
    from core.constants import SITE_LANGUAGES, SUPPORTED_LANGUAGES

    used = {lang for langs in SITE_LANGUAGES.values() for lang in langs}
    missing = sorted(used - set(SUPPORTED_LANGUAGES))
    assert not missing, (
        f"SUPPORTED_LANGUAGES 缺少站点在用的语言 {missing}；"
        "漏一个，那个站点的商品在发布时会被直接 400 拒掉"
    )


def _read_version(path: Path) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    return str(data.get("version") or "")


def test_version_consistent_across_four_places() -> None:
    """后端 APP_VERSION 与三处 package.json 必须一致。

    这条防的是「改了三个地方漏了第四个」：``core/constants.py`` 的
    ``_package_version()`` 优先读仓库根 package.json，但打包后（PyInstaller）
    读不到，会回退到函数里的硬编码兜底值。所以四处版本号是**四份独立数据**，
    少同步一处就出现「装了新版，侧栏永远挂更新角标」或者「真的更新了却检测不到」。

    只比较「能不能读到」：某个 package.json 不在（比如只 checkout 了 server/）
    就跳过，不把缺文件当成版本不一致。
    """
    from core.constants import APP_VERSION

    targets = {
        "根 package.json": REPO_ROOT / "package.json",
        "web/package.json": REPO_ROOT / "web" / "package.json",
        "app/package.json": REPO_ROOT / "app" / "package.json",
    }
    found = {label: _read_version(path)
             for label, path in targets.items() if path.is_file()}
    if not found:
        pytest.skip("三处 package.json 都读不到（可能只 checkout 了 server/）")

    mismatched = {label: value for label, value in found.items() if value != APP_VERSION}
    assert not mismatched, (
        f"版本号不一致：core.constants.APP_VERSION={APP_VERSION!r}，"
        f"但 {mismatched}；"
        "constants.py 的兜底值也必须同步（打包后读不到根 package.json）"
    )
    # 兜底值单独盯一眼：它是 _package_version() 里写死的那个字符串
    source = (SERVER_ROOT / "core" / "constants.py").read_text(encoding="utf-8")
    fallback = re.search(r'return\s+"(\d+\.\d+\.\d+)"', source)
    assert fallback, "core/constants.py 里找不到 _package_version() 的兜底版本号"
    assert fallback.group(1) == APP_VERSION, (
        f"core/constants.py 的兜底版本号 {fallback.group(1)!r} 与 APP_VERSION "
        f"{APP_VERSION!r} 不一致：打包后读不到 package.json，用的就是这个兜底值"
    )
