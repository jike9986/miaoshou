"""bootstrap 聚合接口探针：验一次拿齐首屏状态，且任一段失败不整页挂。

老版是单进程内存态，一个 bootstrap 就够；新版拆成十几个接口后首屏要并发发
七八个请求。这个探针专门验聚合接口的两件事：
  1. 该有的段都在，且内容是真实的（不是空壳）
  2. 某一段读失败时只降级那一段，其它段照常返回
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["AOHUANG_DATA_DIR"] = tempfile.mkdtemp(prefix="boot-")
os.environ["TK_AUTOMATION_DATA_DIR"] = os.environ["AOHUANG_DATA_DIR"]

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), str(detail)))


def main() -> int:
    import main as app_main
    from db.session import SessionLocal, init_db
    from fastapi.testclient import TestClient
    from models.account import Account
    from services import setting_service

    routes = [(getattr(r, "path", ""), sorted(getattr(r, "methods", []) or []))
              for r in app_main.app.routes]
    hits = [r for r in routes if r[0].endswith("/system/bootstrap")]
    check("路由已注册 /system/bootstrap", bool(hits), str(hits))
    check("是 GET", bool(hits) and "GET" in hits[0][1], str(hits[0][1]) if hits else "")

    init_db()
    db = SessionLocal()
    setting_service.set_read_only(db, False)
    db.add(Account(id=1, name="探针", app_key="ak", app_secret_enc="as",
                   site="MY", shops=json.dumps([{"shopId": "1", "name": "店"}]),
                   created_at=0.0, updated_at=0.0))
    db.commit()
    db.close()

    client = TestClient(app_main.app)
    from _probe_auth import teacher_headers

    headers = teacher_headers()
    resp = client.get("/api/v1/system/bootstrap", headers=headers)
    check("HTTP 200", resp.status_code == 200, str(resp.status_code))
    body = resp.json()

    for key in ("application", "version", "readOnly", "publishEnabled", "sharedMode",
                "dataDir", "imageKeyConfigured", "languages", "languageOptions",
                "workflowDefaults", "imageLimits", "accounts", "templates",
                "stats", "batches"):
        check(f"含 {key}", key in body, "" if key in body else str(sorted(body)))

    check("version 是真实版本号", isinstance(body.get("version"), str)
          and body["version"].count(".") == 2, body.get("version"))
    check("languages 非空", bool(body.get("languages")), str(body.get("languages")))
    check("languageOptions 与 languages 等长",
          len(body.get("languageOptions") or []) == len(body.get("languages") or []),
          f"{len(body.get('languageOptions') or [])} vs {len(body.get('languages') or [])}")
    opts = body.get("languageOptions") or []
    check("languageOptions 有 label（不是裸 code）",
          all(o.get("label") for o in opts), str(opts[:3]))
    check("accounts 是真实列表", isinstance(body.get("accounts"), list),
          type(body.get("accounts")).__name__)
    check("templates 结构完整",
          isinstance(body.get("templates"), dict)
          and "items" in body["templates"] and "selected" in body["templates"],
          str(body.get("templates"))[:60])
    check("stats 有商品数", isinstance(body.get("stats"), dict)
          and "totalProducts" in body["stats"],
          str(body.get("stats"))[:80])
    check("无降级段", not body.get("degraded"), str(body.get("degraded")))

    # 降级：把某一段打成故障，确认只有那一段消失
    import api.system as system_api

    original = system_api.batch_service.list_batches

    def boom(*a, **kw):
        raise RuntimeError("模拟批次表读失败")

    system_api.batch_service.list_batches = boom
    try:
        resp2 = client.get("/api/v1/system/bootstrap", headers=headers)
        body2 = resp2.json()
    finally:
        system_api.batch_service.list_batches = original
    check("一段故障仍返回 200", resp2.status_code == 200, str(resp2.status_code))
    check("故障段为 None", body2.get("batches") is None, str(body2.get("batches")))
    check("故障记进 degraded",
          any(d.get("section") == "batches" for d in (body2.get("degraded") or [])),
          str(body2.get("degraded")))
    check("其它段未受影响（accounts 仍在）",
          isinstance(body2.get("accounts"), list), type(body2.get("accounts")).__name__)
    check("其它段未受影响（stats 仍在）", isinstance(body2.get("stats"), dict))
    check("版本号未受影响", body2.get("version") == body.get("version"))

    # 聚合不等于回源：连续两次调用都应很快（<3s），说明没打上游
    import time

    start = time.monotonic()
    client.get("/api/v1/system/bootstrap", headers=headers)
    elapsed = time.monotonic() - start
    check("不回源妙手（单次 < 3s）", elapsed < 3.0, f"{elapsed:.2f}s")

    return report()


def report() -> int:
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = len(RESULTS) - passed
    out = {"total": len(RESULTS), "pass": passed, "fail": failed,
           "cases": [{"name": n, "ok": ok, "detail": d} for n, ok, d in RESULTS]}
    path = Path(__file__).with_name("probe_bootstrap.json")
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    for name, ok, detail in RESULTS:
        print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail else ""))
    print(f"\nTOTAL {passed} PASS {failed} FAIL")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

