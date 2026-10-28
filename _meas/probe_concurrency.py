"""并发写压力实测：给 docs/shared-deployment.md 提供真实数字。

只跑 SQLite，不起 FastAPI —— 要测的是「SQLite 在多写者下的行为」，
HTTP 层只会把数字糊掉。但事务形状照抄真实保存路径：一次保存 = 一条
BEGIN IMMEDIATE → 多次 UPDATE/INSERT → COMMIT，而不是「一条 INSERT」。

三组实验：
  A. 轻负载：10 线程 × 1 条 INSERT，busy_timeout 5s / 15s
  B. 真实形状：10 线程 × 1 个含 5 条语句的事务（模拟一次商品保存）
  C. 拐点：4/8/16/32/64 线程 × 同样形状，找失败率开始非零的地方

跑法：python _meas/probe_concurrency.py
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

_TMP = Path(tempfile.mkdtemp(prefix="aohuang-conc-"))
os.environ["AOHUANG_DATA_DIR"] = str(_TMP)
os.environ["TK_AUTOMATION_DATA_DIR"] = str(_TMP)

INSERT_PRODUCT = (
    "INSERT INTO products (warehouse_key, detail_id, shop_id, shop_name, title, "
    "language, site, cover, cid, category_name, group_name, "
    "price_currency, stock, sales_last_30, sku_count, images, "
    "detail_images, sku_properties, payload, status, claimed, "
    "collected, saved_at, updated_at) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def product_row(key: str, detail_id: str, title: str = "x") -> tuple:
    return (key, detail_id, "s1", "压力测试店铺", title,
            "ms", "MY", "", "", "", "",
            "CNY", 0, 0, 0, "[]", "[]", "[]",
            json.dumps({"k": "v" * 200}), "ready", 0,
            "{}", time.time(), time.time())


def prepare_db() -> Path:
    from db.session import init_db, engine

    init_db()
    engine.dispose()
    return _TMP / "app.db"


def run_case(db_file: Path, threads: int, busy_timeout: int,
             statements: int = 1) -> dict:
    """N 线程同时各跑一个事务；statements>1 时在事务里多写几条。"""
    ok = 0
    locked = 0
    errors: list[str] = []
    durations: list[float] = []
    guard = threading.Lock()
    barrier = threading.Barrier(threads)
    stamp = time.time_ns()

    def worker(index: int) -> None:
        nonlocal ok, locked
        started = time.perf_counter()
        try:
            con = sqlite3.connect(str(db_file), timeout=busy_timeout / 1000.0,
                                  isolation_level=None)
            try:
                con.execute(f"PRAGMA busy_timeout={busy_timeout}")
                con.execute("PRAGMA journal_mode=WAL")
                barrier.wait(timeout=30)
                # 真实保存路径的形状：显式事务 + 多条语句
                con.execute("BEGIN IMMEDIATE")
                for step in range(statements):
                    con.execute(INSERT_PRODUCT, product_row(
                        f"c{stamp}-{index}-{step}", f"{index}-{step}"))
                con.execute("COMMIT")
                with guard:
                    ok += 1
            finally:
                con.close()
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            with guard:
                if "locked" in message or "busy" in message:
                    locked += 1
                else:
                    errors.append(f"{type(exc).__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            with guard:
                errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            with guard:
                durations.append(time.perf_counter() - started)

    wall_start = time.perf_counter()
    pool = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    for thread in pool:
        thread.start()
    for thread in pool:
        thread.join()
    wall = time.perf_counter() - wall_start
    ordered = sorted(durations)
    return {
        "threads": threads,
        "busyTimeoutMs": busy_timeout,
        "statementsPerTxn": statements,
        "ok": ok,
        "locked": locked,
        "otherErrors": len(errors),
        "errorSample": errors[:3],
        "failRate": round((threads - ok) / threads * 100, 1),
        "wallSeconds": round(wall, 3),
        "avgMs": round(sum(durations) / len(durations) * 1000, 1) if durations else 0,
        "p50Ms": round(ordered[len(ordered) // 2] * 1000, 1) if ordered else 0,
        "p95Ms": round(ordered[int(len(ordered) * 0.95) - 1] * 1000, 1) if ordered else 0,
        "maxMs": round(ordered[-1] * 1000, 1) if ordered else 0,
        "throughputTxnPerSec": round(ok / wall, 1) if wall > 0 else 0,
    }


def hold_lock_case(db_file: Path, *, hold_seconds: float, busy_timeout: int,
                   writers: int) -> dict:
    """一个长事务持锁 hold_seconds，同时 N 个写者排队等写锁。

    这是 `database is locked` 的真实成因：不是「并发数太高」，而是「有人
    长时间占着写锁」。共享部署里那个长事务就是批量导入 / 定时发布。
    """
    holding = threading.Event()
    release = threading.Event()
    holder_error: list[str] = []

    def holder() -> None:
        try:
            con = sqlite3.connect(str(db_file), timeout=60.0, isolation_level=None)
            try:
                con.execute("PRAGMA busy_timeout=60000")
                con.execute("BEGIN IMMEDIATE")
                con.execute(INSERT_PRODUCT, product_row(
                    f"hold-{time.time_ns()}", "hold"))
                holding.set()
                release.wait(timeout=hold_seconds)
                con.execute("COMMIT")
            finally:
                con.close()
        except Exception as exc:  # noqa: BLE001
            holder_error.append(f"{type(exc).__name__}: {exc}")
            holding.set()

    waits: list[float] = []
    ok = 0
    locked = 0
    guard = threading.Lock()

    def writer(index: int) -> None:
        nonlocal ok, locked
        started = time.perf_counter()
        try:
            con = sqlite3.connect(str(db_file), timeout=busy_timeout / 1000.0,
                                  isolation_level=None)
            try:
                con.execute(f"PRAGMA busy_timeout={busy_timeout}")
                con.execute("BEGIN IMMEDIATE")
                con.execute(INSERT_PRODUCT, product_row(
                    f"wait-{index}-{time.time_ns()}", f"w{index}"))
                con.execute("COMMIT")
                with guard:
                    ok += 1
            finally:
                con.close()
        except sqlite3.OperationalError as exc:
            with guard:
                if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                    locked += 1
                else:
                    locked += 1
        finally:
            with guard:
                waits.append(time.perf_counter() - started)

    holder_thread = threading.Thread(target=holder)
    holder_thread.start()
    holding.wait(timeout=5)
    started = time.perf_counter()
    pool = [threading.Thread(target=writer, args=(i,)) for i in range(writers)]
    for thread in pool:
        thread.start()
    for thread in pool:
        thread.join()
    wall = time.perf_counter() - started
    release.set()
    holder_thread.join()

    return {
        "holdSeconds": hold_seconds,
        "busyTimeoutMs": busy_timeout,
        "writers": writers,
        "ok": ok,
        "locked": locked,
        "wallSeconds": round(wall, 3),
        "maxWaitMs": round(max(waits) * 1000, 1) if waits else 0,
        "holderError": holder_error[:1],
    }


def main() -> int:
    db_file = prepare_db()
    print(f"临时数据目录：{_TMP}")
    print(f"SQLite {sqlite3.sqlite_version} / Python {sys.version.split()[0]}\n")

    report: dict[str, object] = {"sqlite": sqlite3.sqlite_version,
                                 "python": sys.version.split()[0]}

    print("=== A. 轻负载：10 线程，每个事务 1 条 INSERT ===")
    a = [run_case(db_file, 10, 5000), run_case(db_file, 10, 15000)]
    for row in a:
        print(f"  busy_timeout={row['busyTimeoutMs']:>5}ms  ok={row['ok']:>2}/10  "
              f"locked={row['locked']}  失败率={row['failRate']}%  "
              f"wall={row['wallSeconds']}s  avg={row['avgMs']}ms  max={row['maxMs']}ms")
    report["A_light"] = a

    print("\n=== B. 真实形状：10 线程，每个事务 5 条语句（一次商品保存）===")
    b = [run_case(db_file, 10, 5000, statements=5),
         run_case(db_file, 10, 15000, statements=5)]
    for row in b:
        print(f"  busy_timeout={row['busyTimeoutMs']:>5}ms  ok={row['ok']:>2}/10  "
              f"locked={row['locked']}  失败率={row['failRate']}%  "
              f"wall={row['wallSeconds']}s  avg={row['avgMs']}ms  "
              f"p95={row['p95Ms']}ms  max={row['maxMs']}ms  "
              f"{row['throughputTxnPerSec']} txn/s")
    report["B_realistic"] = b

    print("\n=== C. 拐点：线程数递增，busy_timeout=15000ms，每事务 5 条语句 ===")
    c = []
    for threads in (4, 8, 16, 32, 64):
        row = run_case(db_file, threads, 15000, statements=5)
        c.append(row)
        print(f"  {threads:>2} 线程  ok={row['ok']:>2}/{threads:<2} locked={row['locked']:<2} "
              f"失败率={row['failRate']:>5}%  wall={row['wallSeconds']:>6}s  "
              f"avg={row['avgMs']:>7}ms  p95={row['p95Ms']:>7}ms  max={row['maxMs']:>7}ms")
    report["C_sweep"] = c

    print("\n=== D. 单写者基准：串行 100 次保存（5 条语句/次）===")
    started = time.perf_counter()
    con = sqlite3.connect(str(db_file), timeout=30.0, isolation_level=None)
    try:
        con.execute("PRAGMA busy_timeout=15000")
        for index in range(100):
            con.execute("BEGIN IMMEDIATE")
            for step in range(5):
                con.execute(INSERT_PRODUCT, product_row(
                    f"serial-{index}-{step}", f"s{index}-{step}"))
            con.execute("COMMIT")
    finally:
        con.close()
    elapsed = time.perf_counter() - started
    d = {"count": 100, "statementsPerTxn": 5, "seconds": round(elapsed, 3),
         "avgMs": round(elapsed / 100 * 1000, 2),
         "throughputTxnPerSec": round(100 / elapsed, 1)}
    print(f"  100 次保存 总计 {elapsed:.3f}s  单次 {d['avgMs']}ms  "
          f"{d['throughputTxnPerSec']} txn/s")
    report["D_serial"] = d

    print("\n=== E. 读并发（WAL 允许多读）：20 线程读 + 1 线程写 ===")
    stop = threading.Event()
    read_counts = [0]
    read_errors: list[str] = []
    lock = threading.Lock()

    def reader() -> None:
        try:
            con = sqlite3.connect(str(db_file), timeout=15.0)
            try:
                con.execute("PRAGMA busy_timeout=15000")
                while not stop.is_set():
                    con.execute("SELECT COUNT(*) FROM products").fetchone()
                    with lock:
                        read_counts[0] += 1
            finally:
                con.close()
        except Exception as exc:  # noqa: BLE001
            with lock:
                read_errors.append(f"{type(exc).__name__}: {exc}")

    def writer() -> None:
        try:
            con = sqlite3.connect(str(db_file), timeout=15.0, isolation_level=None)
            try:
                con.execute("PRAGMA busy_timeout=15000")
                for index in range(50):
                    con.execute("BEGIN IMMEDIATE")
                    con.execute(INSERT_PRODUCT, product_row(
                        f"rw-{index}-{time.time_ns()}", f"rw{index}"))
                    con.execute("COMMIT")
            finally:
                con.close()
        except Exception as exc:  # noqa: BLE001
            with lock:
                read_errors.append(f"writer {type(exc).__name__}: {exc}")

    readers = [threading.Thread(target=reader) for _ in range(20)]
    write_thread = threading.Thread(target=writer)
    e_started = time.perf_counter()
    for thread in readers:
        thread.start()
    write_thread.start()
    write_thread.join()
    stop.set()
    for thread in readers:
        thread.join()
    e_elapsed = time.perf_counter() - e_started
    e = {"readers": 20, "writerTxns": 50, "readQueries": read_counts[0],
         "errors": read_errors[:3], "errorCount": len(read_errors),
         "seconds": round(e_elapsed, 3),
         "readQps": round(read_counts[0] / e_elapsed, 1) if e_elapsed else 0}
    print(f"  20 读线程 + 50 次写 总计 {e_elapsed:.3f}s  "
          f"读 {e['readQueries']} 次（{e['readQps']} qps）  错误 {e['errorCount']}")
    report["E_read_write"] = e

    print("\n=== F. busy_timeout 到底够不够：长事务持锁 + 10 个写者排队 ===")
    print("    （真实场景：一批 200 件商品导入/定时发布，写事务会持续数秒）")
    f_cases = []
    for hold_seconds in (1.0, 3.0, 6.0, 9.0, 20.0):
        for busy in (5000, 15000):
            row = hold_lock_case(db_file, hold_seconds=hold_seconds, busy_timeout=busy,
                                 writers=10)
            f_cases.append(row)
            print(f"  持锁 {hold_seconds:>4}s  busy_timeout={busy:>5}ms  "
                  f"成功 {row['ok']:>2}/10  锁冲突 {row['locked']:>2}  "
                  f"最长等待 {row['maxWaitMs']:>7}ms  "
                  f"{'全部通过' if row['locked'] == 0 else '出现 database is locked'}")
    report["F_busy_timeout"] = f_cases

    out = Path(__file__).with_name("probe_concurrency.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n明细 JSON: {out}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
