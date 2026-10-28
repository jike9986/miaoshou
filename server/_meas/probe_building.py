"""验证 word_service.mark_building 的引用计数、30 分钟兜底与 execute 接线。

数据目录用临时目录（AOHUANG_DATA_DIR），绝不碰真实 app.db。
结果写 JSON 文件，避免 PowerShell 控制台中文乱码。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(HERE, "probe_building.json")

# 必须在 import 任何 service 之前设好：core.config 在模块导入期就把 db_path 算死了
TMP = tempfile.mkdtemp(prefix="aohuang-probe-")
os.environ["AOHUANG_DATA_DIR"] = TMP
sys.path.insert(0, ROOT)

# 日志落到临时目录：兜底分支会 warning，混进控制台会把中文打乱
logging.basicConfig(filename=os.path.join(TMP, "probe.log"), level=logging.INFO,
                    encoding="utf-8", force=True)

results: list[dict] = []


def check(name: str, fn) -> None:
    item = {"name": name, "pass": False, "detail": ""}
    try:
        detail = fn()
        item["pass"] = True
        item["detail"] = detail if isinstance(detail, str) else str(detail)
    except AssertionError as exc:
        item["detail"] = f"断言失败：{exc}"
    except Exception as exc:  # noqa: BLE001
        item["detail"] = f"{type(exc).__name__}: {exc}"
        item["trace"] = traceback.format_exc()[-1200:]
    results.append(item)


def main() -> int:
    from db.session import SessionLocal, init_db
    from core import errors
    from models.schedule import Schedule
    from services import schedule_service, word_service

    init_db()
    report: dict = {"data_dir": TMP, "checks": results}

    def new_task(task_id: str, **fields) -> dict:
        """建一个最小可跑的 Schedule，返回它的 id（account_id=None 不会撞外键）。"""
        db = SessionLocal()
        try:
            row = Schedule(id=task_id, name=task_id, account_id=None,
                           template_name="", per_run=2, enabled=0)
            for key, value in fields.items():
                setattr(row, key, value)
            db.add(row)
            db.commit()
            return {"taskId": task_id}
        finally:
            db.close()

    # ---- 必需的第 1 条 ----
    def c1():
        word_service.reset_building()  # 只清本进程内的内存标志，不碰库
        assert word_service.is_building() is False, "初始应为 False"
        return "is_building() == False"

    check("1 初始 is_building() == False", c1)

    # ---- 必需的第 2 条 ----
    def c2():
        word_service.mark_building(True)
        assert word_service.is_building() is True, "置位后应为 True"
        try:
            word_service.require_idle()
        except errors.AppError as exc:
            assert exc.code == errors.CONFLICT_BUSY, f"错误码应为 CONFLICT_BUSY，实际 {exc.code}"
            assert exc.status == 409, f"HTTP 状态应为 409，实际 {exc.status}"
            return (f"is_building()==True；require_idle 抛 {exc.code}({exc.status}) "
                    f"{exc.message}")
        raise AssertionError("require_idle 没有抛错")

    check("2 mark_building(True) 后 is_building()==True 且 require_idle 抛 CONFLICT_BUSY", c2)

    # ---- 必需的第 3 条：嵌套计数 ----
    def c3():
        word_service.mark_building(True)   # 计数 -> 2
        assert word_service._building_count == 2, f"计数应为 2，实际 {word_service._building_count}"
        word_service.mark_building(False)  # 计数 -> 1
        assert word_service.is_building() is True, "还有一个批次没结束，应仍为 True"
        return f"两次 True 后计数={2}，一次 False 后 count={word_service._building_count}，仍为 True"

    check("3 嵌套两次 True 再一次 False 仍为 True（引用计数正确）", c3)

    # ---- 必需的第 4 条：归零 ----
    def c4():
        word_service.mark_building(False)  # 计数 -> 0
        assert word_service.is_building() is False, "归零后应为 False"
        assert word_service._building_count == 0, f"计数应为 0，实际 {word_service._building_count}"
        word_service.require_idle()  # 不应抛
        return "再一次 False 计数归零，is_building()==False，require_idle() 不抛"

    check("4 再一次 False 归零且 require_idle 不抛", c4)

    # ---- 必需的第 5 条：execute 正常走完 ----
    def c5():
        word_service.reset_building()
        new_task("probe-normal")
        db = SessionLocal()
        try:
            row = db.query(Schedule).filter(Schedule.id == "probe-normal").one()
            during: list[bool] = []
            original = schedule_service.pick_products

            def spy(*args, **kwargs):
                # 在批次内部取样：此刻闸门必须是关上的
                during.append(word_service.is_building())
                return original(*args, **kwargs)

            schedule_service.pick_products = spy  # type: ignore[assignment]
            try:
                out = schedule_service.execute(db, row)
            finally:
                schedule_service.pick_products = original  # type: ignore[assignment]
            assert during == [True], f"批次执行中应置位，实测 {during}"
            assert word_service.is_building() is False, "execute 结束后应为 False"
            return (f"批次内取样 is_building()==True，结束后 ==False；"
                    f"status={out.get('status')} reason={out.get('reason')}")
        finally:
            db.close()

    check("5 execute() 走完一遍（仓库空 -> empty）后 is_building() == False", c5)

    # ---- 必需的第 6 条：execute 内部抛异常，异常传播后仍为 False ----
    def c6():
        word_service.reset_building()
        new_task("probe-boom")
        db = SessionLocal()
        try:
            row = db.query(Schedule).filter(Schedule.id == "probe-boom").one()
            during: list[bool] = []
            original = schedule_service.pick_products

            def boom(*args, **kwargs):
                during.append(word_service.is_building())
                raise RuntimeError("模拟批次内部崩溃")

            schedule_service.pick_products = boom  # type: ignore[assignment]
            raised: list[str] = []
            try:
                schedule_service.execute(db, row, raise_on_error=True)
            except RuntimeError as exc:
                raised.append(str(exc))
            finally:
                schedule_service.pick_products = original  # type: ignore[assignment]
            db.rollback()
            assert raised == ["模拟批次内部崩溃"], f"预期异常传播出来，实际 {raised}"
            assert during == [True], f"抛错前应已置位，实测 {during}"
            assert word_service.is_building() is False, "异常传播后应为 False"
            return (f"抛出 RuntimeError({raised[0]})；抛错前 is_building()==True，"
                    f"传播后 ==False")
        finally:
            db.close()

    check("6 execute() 内部抛异常传播后 is_building() 仍为 False", c6)

    # ---- 额外：只读短路不置位（关键点的第 3 条） ----
    def x1():
        from services import setting_service

        word_service.reset_building()
        new_task("probe-readonly")
        db = SessionLocal()
        try:
            row = db.query(Schedule).filter(Schedule.id == "probe-readonly").one()
            seen: list[bool] = []
            original = setting_service.is_read_only
            setting_service.is_read_only = lambda _db: True  # type: ignore[assignment]
            try:
                out = schedule_service.execute(db, row)
                seen.append(word_service.is_building())
            finally:
                setting_service.is_read_only = original  # type: ignore[assignment]
            assert out.get("status") == schedule_service.RUN_EMPTY, \
                f"状态应为 empty，实际 {out.get('status')}"
            assert "只读" in str(out.get("reason")), f"reason 应提到只读，实际 {out.get('reason')}"
            assert seen == [False], f"只读分支不应置位，实测 {seen}"
            return f"只读返回 status={out['status']}，全程 is_building()==False"
        finally:
            db.close()

    check("额外A 只读模式提前 return 不置位", x1)

    # ---- 额外：30 分钟兜底 ----
    def x2():
        import time as _t

        word_service.reset_building()
        word_service.mark_building(True)
        assert word_service.is_building() is True

        # 把「上次置位时间」往前推，模拟线程被杀在 finally 之前
        word_service._building_since = _t.monotonic() - (word_service.BUILDING_STALE_SECONDS + 1)
        assert word_service.is_building() is False, "超龄后应自动视为空闲"
        assert word_service._building_count == 0, "超龄后计数应被清零"
        word_service.require_idle()  # 不抛
        return (f"BUILDING_STALE_SECONDS={word_service.BUILDING_STALE_SECONDS}s"
                f"（对齐 RUN_DUE_TIMEOUT_SECONDS={schedule_service.RUN_DUE_TIMEOUT_SECONDS}s）；"
                "超龄自动清零，require_idle() 不抛")

    check("额外B 30 分钟兜底：超龄自动视为空闲", x2)

    # ---- 额外：计数不会为负 ----
    def x3():
        word_service.reset_building()
        word_service.mark_building(False)
        word_service.mark_building(False)
        assert word_service._building_count == 0, f"计数不应为负，实际 {word_service._building_count}"
        assert word_service.is_building() is False
        return "连续多余 False 后计数停在 0"

    check("额外C 多余的 False 不会把计数减成负数", x3)

    # ---- 额外：并发时先结束的线程不能放开闸门 ----
    def x4():
        import threading
        import time as _t

        word_service.reset_building()
        new_task("probe-concurrent-a")
        new_task("probe-concurrent-b")

        gate = threading.Barrier(2)
        # 模块级只 patch 一次：两个线程各 patch 会互相覆盖，导致统计串台
        original = schedule_service.pick_products
        during: dict[str, list[bool]] = {}
        lock = threading.Lock()
        holds = {"batch-a": 0.0, "batch-b": 0.8}

        def spy(*args, **kwargs):
            mine = threading.current_thread().name
            with lock:
                first = mine not in during
                during.setdefault(mine, [])
            if first:
                gate.wait(timeout=10)   # 两个线程都进批次后再各自往下跑
                _t.sleep(holds.get(mine, 0.0))
                with lock:
                    during[mine].append(word_service.is_building())
            return original(*args, **kwargs)

        schedule_service.pick_products = spy  # type: ignore[assignment]

        def worker(task_id: str) -> None:
            db = SessionLocal()
            try:
                row = db.query(Schedule).filter(Schedule.id == task_id).one()
                schedule_service.execute(db, row)
            finally:
                db.close()

        # batch-a hold 0：它先跑完，batch-b 还在批次里，闸门不能因为 a 结束而放开
        threads = [threading.Thread(target=worker, args=("probe-concurrent-a",), name="batch-a"),
                   threading.Thread(target=worker, args=("probe-concurrent-b",), name="batch-b")]
        try:
            for th in threads:
                th.start()
            _t.sleep(0.4)              # 此刻 a 已收尾、b 仍在跑
            mid = word_service.is_building()
            for th in threads:
                th.join(timeout=15)
        finally:
            schedule_service.pick_products = original  # type: ignore[assignment]

        assert mid is True, f"a 结束后 b 还在跑，应为 True，实测 {mid}"
        samples = [value for values in during.values() for value in values]
        assert samples and all(samples), f"两个线程内部都应看到 True，实测 {during}"
        assert word_service.is_building() is False, "全部结束后应为 False"
        return (f"a 已结束、b 仍在跑时主线程看到 True；线程内取样 {during}；"
                "全部 finally 执行后 ==False")

    check("额外D 并发批次：先结束的线程不放开闸门", x4)

    # ---- 额外：run_due / _run_plan 这条真实调度路径也会置位 ----
    def x5():
        from datetime import datetime as _dt, timedelta as _td

        word_service.reset_building()
        db = SessionLocal()
        try:
            # account_id=None 合法（可空外键），这样不必造账号也能走到期
            due = Schedule(id="probe-due", name="probe-due", account_id=None,
                           template_name="", per_run=1, enabled=True,
                           frequency="daily", time_of_day="00:00",
                           last_run_date="", attempt_date="", attempts=0)
            db.add(due)
            db.commit()

            original_execute = schedule_service.execute
            seen: list[bool] = []

            def fake_execute(db_, task, **kwargs):
                seen.append(word_service.is_building())
                return {"taskId": str(task.id), "status": schedule_service.RUN_EMPTY,
                        "requested": 1, "selected": 0, "published": 0, "failed": 0,
                        "reason": "probe", "results": [], "at": None}

            schedule_service.execute = fake_execute  # type: ignore[assignment]
            try:
                out = schedule_service.run_due(db, now=_dt.now() + _td(minutes=1))
            finally:
                schedule_service.execute = original_execute  # type: ignore[assignment]
            assert seen == [True], f"run_due 执行期间应置位，实测 {seen}"
            assert out and out[0]["status"] == schedule_service.RUN_EMPTY
            assert word_service.is_building() is False, "run_due 返回后应为 False"
            return (f"run_due 内层 execute 看到 is_building()==True（{len(out)} 条摘要），"
                    "返回后 ==False")
        finally:
            db.close()

    check("额外E run_due / _run_plan 路径同样置位并在返回后放开", x5)

    report["summary"] = {
        "total": len(results),
        "passed": sum(1 for r in results if r["pass"]),
        "failed": [r["name"] for r in results if not r["pass"]],
    }
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception:  # noqa: BLE001
        with open(OUT, "w", encoding="utf-8") as fh:
            json.dump({"fatal": traceback.format_exc(), "data_dir": TMP,
                       "checks": results}, fh, ensure_ascii=False, indent=2)
        code = 1
    print("OK" if code == 0 else "FAILED")
    sys.exit(code)
