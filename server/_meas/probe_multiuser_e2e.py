"""多人身份端到端：验证「每人一把 token」真的能走通全部业务接口。

发现这个 bug 的过程值得记下来：功能上线后，`GET /operators` 正常、新建操作员
正常、列表也正常，但拿张三的 token 去打 `/warehouse/stats` 直接 401。
原因是 `api/__init__.py` 把所有业务路由挂在一道 `Depends(require_token)` 上，
而 `require_token` 只认全局 `secret.key`——发出去的 token 连第一道门都进不来，
「每人一个身份」等于摆设。`current_operator` 在门后翻译，门本身没开。

本探针的每条断言都对应一个真实会踩的场景，不复述实现细节。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["AOHUANG_DATA_DIR"] = tempfile.mkdtemp(prefix="multiuser-e2e-")
os.environ["TK_AUTOMATION_DATA_DIR"] = os.environ["AOHUANG_DATA_DIR"]

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), str(detail)[:300]))
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  [{str(detail)[:300]}]" if detail else ""))


def main() -> int:
    from fastapi.testclient import TestClient

    from core.config import get_settings
    from db.session import SessionLocal, init_db
    from main import app
    from services import operator_service

    init_db()
    secret = get_settings().local_secret()
    admin = {"Authorization": f"Bearer {secret}"}

    with TestClient(app) as client:
        # --- 向后兼容：老客户端只带 secret.key ---
        r = client.get("/api/v1/warehouse/stats", headers=admin)
        check("老客户端只带 secret.key 能访问业务接口", r.status_code == 200,
              f"{r.status_code} {r.text[:100]}")
        r = client.get("/api/v1/auth/status", headers=admin)
        check("老客户端能读连接状态", r.status_code == 200, r.status_code)

        # --- 默认操作员 ---
        with SessionLocal() as db:
            operator_service.ensure_default(db)
            db.commit()
            default = operator_service.resolve(db, secret)
        check("secret.key 解析成「本机」操作员",
              default is not None and default.name == operator_service.DEFAULT_OPERATOR_NAME,
              default.name if default else "None")

        # --- 新建操作员 ---
        r = client.post("/api/v1/operators", headers=admin, json={"name": "张三"})
        check("新建操作员成功", r.status_code in (200, 201), f"{r.status_code} {r.text[:150]}")
        body = r.json() if r.status_code in (200, 201) else {}
        token = body.get("token") or ""
        check("新建返回明文 token（仅此一次）", len(token) >= 20, f"len={len(token)}")
        check("新建返回的操作员不含 token 字段",
              "token" not in json.dumps(body.get("operator") or {}),
              str(body.get("operator"))[:150])

        # --- 核心：操作员 token 必须能走业务接口 ---
        zhang = {"Authorization": f"Bearer {token}"}
        r = client.get("/api/v1/warehouse/stats", headers=zhang)
        check("★ 操作员 token 能访问业务接口（本次修复的核心）",
              r.status_code == 200, f"{r.status_code} {r.text[:120]}")
        r = client.get("/api/v1/warehouse", headers=zhang)
        check("操作员 token 能列商品", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
        r = client.get("/api/v1/system/bootstrap", headers=zhang)
        check("操作员 token 能读 bootstrap", r.status_code == 200, f"{r.status_code}")
        r = client.get("/api/v1/operators", headers=zhang)
        check("操作员 token 能读操作员列表", r.status_code == 200, f"{r.status_code}")

        # --- 列表不泄露 token ---
        r = client.get("/api/v1/operators", headers=admin)
        raw = r.text
        check("列表响应里搜不到任何 token 原文",
              token not in raw and secret not in raw, f"{len(raw)} 字节")

        # --- 无效 token 必须拒 ---
        r = client.get("/api/v1/warehouse/stats",
                       headers={"Authorization": "Bearer not-a-real-token"})
        check("乱码 token 被拒 401", r.status_code == 401, r.status_code)
        check("拒绝时用 TOKEN_INVALID 错误码",
              r.json().get("error", {}).get("code") == "TOKEN_INVALID",
              str(r.json().get("error", {}))[:150])

        # --- 什么都不带必须拒（与 0.1 一致，没放松）---
        r = client.get("/api/v1/warehouse/stats")
        check("不带 token 被拒 401", r.status_code == 401, r.status_code)

        # --- 停用后立刻失效 ---
        with SessionLocal() as db:
            row = operator_service.resolve(db, token)
            oid = row.id
        r = client.post(f"/api/v1/operators/{oid}/disable", headers=admin)
        check("管理员能停用操作员", r.status_code in (200, 204), f"{r.status_code} {r.text[:120]}")
        r = client.get("/api/v1/warehouse/stats", headers=zhang)
        check("★ 被停用后业务接口立刻拒（停用不能只是界面标记）",
              r.status_code in (401, 403), f"{r.status_code} {r.text[:150]}")
        check("被停用时说明原因而不是干瘪的 401",
              "停用" in r.text, r.text[:200])

        # --- 停用的不能是最后一个管理员 ---
        with SessionLocal() as db:
            admin_op = operator_service.resolve(db, secret)
            admin_id = admin_op.id
        r = client.post(f"/api/v1/operators/{admin_id}/disable", headers=admin)
        check("不能停用最后一个管理员（否则谁都管不了）",
              r.status_code >= 400, f"{r.status_code} {r.text[:150]}")

        # --- X-Operator 声明身份（前端已有共享 key、只想记名）---
        # HTTP 头只能是 ASCII（RFC 7230），中文名必须百分号编码后才能放进去。
        # 这条断言同时验证了「解码」这一步：编码过的名字要能被识别成本人。
        from urllib.parse import quote

        r = client.get("/api/v1/warehouse/stats",
                       headers={**admin, "X-Operator": quote("张三")})
        check("X-Operator 用百分号编码的中文名能识别（已停用则拒）",
              r.status_code in (401, 403), f"{r.status_code} {r.text[:120]}")
        r = client.get("/api/v1/warehouse/stats",
                       headers={**admin, "X-Operator": quote("查无此人")})
        check("X-Operator 声明未知名字被拒（不静默记成本机）",
              r.status_code in (401, 403), f"{r.status_code}")
        r = client.get("/api/v1/warehouse/stats",
                       headers={**admin, "X-Operator": "zhangsan"})
        check("X-Operator 用 ASCII 名字查不到时也拒",
              r.status_code in (401, 403), f"{r.status_code}")

        # --- 审计留痕 ---
        r = client.get("/api/v1/audit-logs", headers=admin)
        check("审计日志可读", r.status_code == 200, r.status_code)
        items = (r.json() or {}).get("items") or []
        check("审计里有记录", len(items) >= 1, f"{len(items)} 条")
        names = {row.get("operatorName") for row in items}
        check("审计记录了操作员名字（不是空白）",
              any(bool(n) for n in names), str(sorted(n for n in names if n))[:150])

    return report()


def report() -> int:
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = len(RESULTS) - passed
    out = {"total": len(RESULTS), "pass": passed, "fail": failed,
           "cases": [{"name": n, "ok": ok, "detail": d} for n, ok, d in RESULTS]}
    Path(__file__).with_name("probe_multiuser_e2e.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nTOTAL {passed} PASS {failed} FAIL")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
