"""API router 汇总：所有业务路由挂在 /api/v1 下。

注意：/system/health 另有 /api/system/health 别名（Electron 探活用），
见 main.py。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from api import (
    auth,
    automation_runs,
    batch,
    carrier,
    categories,
    collect_box,
    groups,
    images,
    operators,
    publish,
    schedules,
    shops,
    size_charts,
    sync,
    system,
    titles,
    warehouse,
    words,
)
from api.deps import (
    current_operator,
    require_password_change_complete,
    require_token,
    track_client,
)

API_PREFIX = "/api/v1"

# 全部业务路由统一要求本地 token；health 在 system.py 里单独放行（见 main.py）
router = APIRouter()
# 两道依赖，职责分开：
#   require_token     —— 门禁：这把凭据能不能进（本机 secret.key 或某个人的 token）
#   current_operator  —— 翻译：这把凭据是谁，顺带拦掉已停用的身份
# 两个都挂在这里而不是让每个路由自己写，是因为「声明了身份却不被解析」会
# 变成最糟的一种 bug：审计里把张三做的事记到本机名下，人却以为记对了。
# FastAPI 会按依赖函数缓存结果，所以已经写了 OperatorDep 的路由不会重复解析。
_guarded = APIRouter(dependencies=[
    Depends(require_token), Depends(current_operator),
    Depends(require_password_change_complete),
])
# 在线客户端心跳：挂在**根 router** 上，覆盖包括免鉴权 health 在内的所有请求。
# 为什么不做成中间件：main.py 归另一处改动所有（bind_host 那批），
# 而 api/__init__.py 是我们能加东西的地方；router 依赖对经过它的每个请求都执行，
# 效果等价，且不会在 404（未匹配路径）上白记一笔。
router.dependencies.append(Depends(track_client))

for _module in (auth, automation_runs, shops, collect_box, warehouse, groups, categories,
                images, publish, schedules, words, carrier, size_charts, titles,
                sync, batch):
    _guarded.include_router(_module.router)

# /words 是 /violation-words 的短路径别名（任务书用短名，契约用长名）
_guarded.include_router(words.alias_router)

# 操作员：**不进 _guarded**。GET /operators 是「我是谁」的探活接口——
# 前端切换身份时拿一串 token 打它，200=有效 / 401=无效。挂在 require_token
# 后面的话，共享 secret.key 会先放行任何请求，探活永远返回 200，判不出真假。
# 写接口自己带 OperatorDep（见 api/operators.py），鉴权一点没松。
router.include_router(operators.router)
router.include_router(operators.audit_router)

# system 里的 health 需要免鉴权，因此单独挂载
router.include_router(system.health_router)
router.include_router(auth.login_router)
router.include_router(_guarded)
router.include_router(system.router, dependencies=[Depends(require_token)])


def health_aliases() -> list[str]:
    return [f"{API_PREFIX}/system/health", "/api/system/health"]


__all__ = ["router", "API_PREFIX", "health_aliases"]
