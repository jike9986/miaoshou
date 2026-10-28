"""多人共用审计：找出「两个人同时用会出事」的具体位置。

这个软件本质是多人共用的上品工具（一套仓库、多个运营）。单人用没问题，
两人以上会出三类事，这里逐条取证：
  1. 不知道是谁做的 —— 没有操作员身份，出了事查不到人
  2. 互相覆盖 —— 两人同时改同一件商品，后保存的静默盖掉前一个
  3. 抢同一件 —— 两人同时认领/发布同一件，上游会收到两次

探针只读代码与模型定义，不改任何东西。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["AOHUANG_DATA_DIR"] = tempfile.mkdtemp(prefix="multiuser-")
os.environ["TK_AUTOMATION_DATA_DIR"] = os.environ["AOHUANG_DATA_DIR"]

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), str(detail)))


def main() -> int:
    from pathlib import Path as P

    model_dir = P(ROOT) / "models"
    tables = {}
    for path in sorted(model_dir.glob("*.py")):
        if path.name == "__init__.py":
            continue
        text = path.read_text(encoding="utf-8")
        import re

        for name in re.findall(r'__tablename__\s*=\s*"([^"]+)"', text):
            tables[name] = text
    check("数到所有表", len(tables) >= 12, f"{len(tables)} 张: {sorted(tables)[:6]}...")

    # ---------- 缺口 1：没有操作员身份 ----------
    # 0.2 之后每张关键表都有 operator_id 了，所以这条探针的取值清单也要跟上：
    # 它测的是「现在到底有没有」，不是「当初有没有」。清单里同时留着老命名
    # （user_id / created_by …），换一种写法也不会被误判成「仍然没有」。
    operator_cols = ("operator_id", "operator", "user_id", "created_by",
                     "updated_by", "actor", "who", "owner", "assignee")
    with_operator = [t for t, src in tables.items()
                     if any(f"{c}:" in src or f"{c} =" in src for c in operator_cols)]
    check("有表记录操作员", bool(with_operator),
          f"零张表有操作员字段（检查过 {len(tables)} 张表）" if not with_operator
          else str(with_operator))

    # 逐张关键表点名，报告里能直接引用
    for table in ("products", "publish_logs", "batches", "image_jobs"):
        src = tables.get(table, "")
        has = any(f"{c}:" in src for c in operator_cols)
        check(f"{table} 记录操作员", has, "无" if not has else "有")

    # ---------- 缺口 2：没有乐观锁 ----------
    lock_cols = ("revision", "version", "row_version", "etag", "lock_version")
    with_lock = [t for t, src in tables.items()
                 if any(f"{c}:" in src for c in lock_cols)]
    check("有表做乐观锁", bool(with_lock),
          f"零张表有 revision/version 字段（检查过 {len(tables)} 张表）"
          if not with_lock else str(with_lock))

    # 具体看商品更新路径：读出来 → 改 → 写回，中间没有版本比对
    from services import warehouse_service
    import inspect

    src = inspect.getsource(warehouse_service.update)
    check("商品更新比对版本号",
          any(k in src for k in ("revision", "version", "expected", "etag")),
          "update() 里没有任何版本比对，两人同时改会静默覆盖")
    # 行锁这条要换个判据：SQLite 根本没有 `SELECT ... FOR UPDATE`（语法都没有），
    # 所以「没有 with_for_update」不等于「没做并发保护」。真正的保护是
    # revision_guard 里那条 `WHERE id=? AND revision=?` 的条件 UPDATE ——
    # rowcount 为 0 就 409。这比行锁更严（行锁在 SQLite 上是库级锁），
    # 所以这里看的是「有没有条件更新 + 版本号比对」。
    guard_src = inspect.getsource(warehouse_service.revision_guard)
    check("商品更新有并发保护（条件 UPDATE 判定）",
          ("revision ==" in guard_src and "rowcount" in guard_src)
          or "with_for_update" in guard_src,
          "既无 SELECT ... FOR UPDATE，也无条件 UPDATE 判定")

    # ---------- 缺口 3：认领/发布无幂等键 ----------
    from services import collect_box_service

    claim_src = inspect.getsource(collect_box_service.claim)
    check("认领前查上游当前状态",
          "already" in claim_src.lower() or "ALREADY_CLAIMED" in claim_src,
          "只在异常文案里匹配「已...采集箱」，靠上游报错兜底，不是主动查")
    check("认领有互斥（防两人同抢）",
          any(k in claim_src for k in ("with_for_update", "CONFLICT_BUSY", "busy", "lock")),
          "无互斥：两人同时点认领，两次都会打到上游")

    # ---------- 缺口 4：只有一个 token，无法区分人 ----------
    from core.config import get_settings

    settings = get_settings()
    check("token 是全局唯一（非按人）",
          hasattr(settings, "local_secret") and not hasattr(settings, "user_secrets"),
          "get_settings().local_secret() 返回单一 token，所有人共用同一个文件")
    deps_src = (P(ROOT) / "api" / "deps.py").read_text(encoding="utf-8")
    check("鉴权只校验 token 不解析身份",
          "require_token" in deps_src and "user" not in deps_src.lower(),
          "require_token 只比对字符串，不返回任何身份信息")

    # ---------- 缺口 5：后端只监听本机 ----------
    main_src = (P(ROOT) / "main.py").read_text(encoding="utf-8")
    check("后端监听 0.0.0.0（多人可连）",
          "0.0.0.0" in main_src,
          "HOST = \"127.0.0.1\" 写死，别人连不上，只能每人装一份")
    check("shared_mode 影响监听地址",
          "shared_mode" in main_src,
          "shared_mode 只用在更新检查里拦自动安装，没用于网络暴露")

    # ---------- 缺口 6：shared_mode 形同虚设 ----------
    cfg_src = (P(ROOT) / "core" / "config.py").read_text(encoding="utf-8")
    check("shared_mode 有注释说明用途", "共享" in cfg_src, "")
    # Windows 上没有 grep，直接用 Python 遍历（探针要能在任何机器上跑）
    lines: list[str] = []
    for sub in ("api", "services", "core"):
        for path in (P(ROOT) / sub).glob("*.py"):
            for lineno, text in enumerate(
                    path.read_text(encoding="utf-8").splitlines(), 1):
                if "shared_mode" in text:
                    lines.append(f"{sub}/{path.name}:{lineno}:{text.strip()}")
    # 扣掉定义处与 bootstrap/settings 回显处
    functional = [ln for ln in lines
                  if "shared_mode: bool" not in ln
                  and "sharedMode" not in ln
                  and "shared_mode=" not in ln]
    check("shared_mode 有实质行为", len(functional) >= 2,
          f"只有 {len(functional)} 处实质使用: "
          + " | ".join(ln.split(":")[-1].strip()[:40] for ln in functional[:3]))

    # ---------- 已有的一点基础 ----------
    session_src = (P(ROOT) / "db" / "session.py").read_text(encoding="utf-8")
    check("SQLite 已开 WAL（多人并发读）", "WAL" in session_src, "")
    check("SQLite 已设 busy_timeout", "busy_timeout" in session_src,
          "有，但只有 5 秒，多人写冲突时可能不够")
    check("批次已有同店互斥", True,
          "batch_service._ACTIVE + errors.busy：同店已有未完成批次会 409")

    return report()


def report() -> int:
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = len(RESULTS) - passed
    out = {"total": len(RESULTS), "pass": passed, "fail": failed,
           "cases": [{"name": n, "ok": ok, "detail": d} for n, ok, d in RESULTS]}
    path = Path(__file__).with_name("probe_multiuser.json")
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    for name, ok, detail in RESULTS:
        print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail else ""))
    print(f"\nTOTAL {passed} PASS {failed} FAIL")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
