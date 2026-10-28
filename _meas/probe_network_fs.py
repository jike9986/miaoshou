"""网络盘（SMB/UNC）上跑 SQLite 的实测：区分「实测到的事实」与「官方结论」。

实验设计要点（决定了结论能不能用）：

  · **多进程**，不是多线程。SQLite 的锁在网络盘上失效的根因是 fcntl/字节区间锁
    跨主机不可靠，同进程内的线程共享同一份 OS 缓存，测不出来。
  · 路径走 **UNC**（`\\\\localhost\\C$\\...`）与 **映射盘符**两条，因为 Windows 上
    运维习惯用映射盘（Z:），而 SMB 语义相同、访问路径不同。
  · 每个用例都跑「建库 → WAL → 多进程并发写 → 读回校验 → 网络中断模拟」。

诚实边界（写进文档时必须保留）：
  · 本机的 `\\\\localhost\\C$` 是**回环 SMB**，物理上没有跨网线。
    它能证明「SMB 上的锁语义在正常网络下能工作」，**不能**证明「跨主机断网时不坏」。
  · 断网/掉线才是官方警告的真正场景，这里只能模拟（见 case D）。

跑法：python _meas/probe_network_fs.py
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

RESULTS: list[dict] = []


def record(name: str, ok: bool, detail: str, **extra) -> None:
    RESULTS.append({"name": name, "ok": bool(ok), "detail": str(detail), **extra})
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail else ""))


# 子进程要跑的写入脚本：独立进程 = 独立连接 = 真实的「另一台客户端」
CHILD = r'''
import sqlite3, sys, time
path, tag, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
ok = 0
err = ""
try:
    con = sqlite3.connect(path, timeout=10.0, isolation_level=None)
    con.execute("PRAGMA busy_timeout=10000")
    con.execute("PRAGMA journal_mode=WAL")
    for i in range(count):
        con.execute("BEGIN IMMEDIATE")
        con.execute("INSERT INTO items(tag, n) VALUES (?, ?)", (tag, i))
        con.execute("COMMIT")
        ok += 1
    con.close()
except Exception as exc:
    err = f"{type(exc).__name__}: {exc}"
print(f"{ok}|{err}")
'''


def setup_db(db_path: Path, journal: str = "WAL") -> None:
    con = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        mode = con.execute(f"PRAGMA journal_mode={journal}").fetchone()
        con.execute("CREATE TABLE IF NOT EXISTS items("
                    "id INTEGER PRIMARY KEY, tag TEXT, n INTEGER)")
        con.commit()
        return str(mode[0]) if mode else ""
    finally:
        con.close()


def multi_process_write(db_path: Path, processes: int, per_process: int) -> dict:
    """N 个独立进程并发写，返回成功/失败/错误样本。"""
    child_file = Path(tempfile.gettempdir()) / "_aohuang_netfs_child.py"
    child_file.write_text(CHILD, encoding="utf-8")
    started = time.perf_counter()
    procs = []
    for index in range(processes):
        procs.append(subprocess.Popen(
            [sys.executable, str(child_file), str(db_path), f"p{index}", str(per_process)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
    total_ok = 0
    errors: list[str] = []
    for proc in procs:
        out, err = proc.communicate(timeout=180)
        text = (out or "").strip().splitlines()
        if text:
            ok_text, _, err_text = text[-1].partition("|")
            try:
                total_ok += int(ok_text)
            except ValueError:
                errors.append(f"解析失败: {text[-1]}")
            if err_text:
                errors.append(err_text)
        elif err:
            errors.append(err.strip().splitlines()[-1])
    wall = time.perf_counter() - started
    return {"processes": processes, "perProcess": per_process,
            "ok": total_ok, "expected": processes * per_process,
            "errors": errors[:4], "errorCount": len(errors),
            "wallSeconds": round(wall, 3)}


def read_back(db_path: Path) -> dict:
    con = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        integrity = con.execute("PRAGMA integrity_check").fetchone()
        rows = con.execute("SELECT COUNT(*) FROM items").fetchone()
        mode = con.execute("PRAGMA journal_mode").fetchone()
    finally:
        con.close()
    return {"integrity": str(integrity[0]) if integrity else "",
            "rows": int(rows[0]) if rows else -1,
            "journalMode": str(mode[0]) if mode else ""}


def case_local_baseline() -> None:
    """对照组：本地 NTFS。网络盘的结论必须有本地基线才能对比。"""
    tmp = Path(tempfile.mkdtemp(prefix="netfs-local-"))
    try:
        db = tmp / "app.db"
        mode = setup_db(db)
        record("本地盘 WAL 生效", mode.lower() == "wal", f"journal_mode={mode}")
        res = multi_process_write(db, processes=4, per_process=10)
        back = read_back(db)
        record("本地盘 4 进程 × 10 写全部成功",
               res["ok"] == res["expected"],
               f"{res['ok']}/{res['expected']} wall={res['wallSeconds']}s "
               f"err={res['errorCount']}")
        record("本地盘 integrity_check=ok", back["integrity"] == "ok",
               f"{back['integrity']} rows={back['rows']}")
        return {"mode": mode, "write": res, "readback": back}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def case_unc() -> dict | None:
    """UNC 路径（\\\\localhost\\C$\\...）—— 回环 SMB。"""
    unc_root = Path(r"\\localhost\C$\Windows\Temp\aohuang-netfs-probe")
    try:
        unc_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        record("UNC 路径可写", False, f"无法创建 {unc_root}: {exc}")
        return None
    record("UNC 路径可写", True, str(unc_root))
    db = unc_root / "app.db"
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = Path(str(db) + suffix)
        if candidate.exists():
            candidate.unlink()
    try:
        mode = setup_db(db)
        record("UNC 上 journal_mode=WAL 设置成功（返回 wal）",
               mode.lower() == "wal", f"journal_mode={mode}")
        res = multi_process_write(db, processes=4, per_process=10)
        back = read_back(db)
        record("UNC 4 进程 × 10 写全部成功",
               res["ok"] == res["expected"],
               f"{res['ok']}/{res['expected']} wall={res['wallSeconds']}s "
               f"err={res['errorCount']} {res['errors'][:2]}")
        record("UNC integrity_check=ok", back["integrity"] == "ok",
               f"{back['integrity']} rows={back['rows']}")
        files = sorted(p.name for p in unc_root.glob("app.db*"))
        # 注意：最后一个连接关闭时 SQLite 会 checkpoint 并**删掉** -wal，
        # 所以「现在磁盘上没有 -wal」不代表 WAL 没工作过。必须在连接活着的时候看。
        con = sqlite3.connect(str(db), timeout=10.0, isolation_level=None)
        try:
            con.execute("PRAGMA busy_timeout=10000")
            con.execute("PRAGMA wal_autocheckpoint=0")
            con.execute("BEGIN IMMEDIATE")
            con.execute("INSERT INTO items(tag, n) VALUES ('live', 1)")
            con.execute("COMMIT")
            live_files = sorted(p.name for p in unc_root.glob("app.db*"))
        finally:
            con.close()
        record("UNC 上 WAL 落盘文件齐全（连接活着时能看到 -wal）",
               any(name.endswith("-wal") for name in live_files),
               f"空闲后={files} 写入中={live_files}")
        return {"root": str(unc_root), "mode": mode, "write": res,
                "readback": back, "filesIdle": files, "filesLive": live_files}
    finally:
        pass


def case_mapped_drive() -> dict | None:
    """映射盘符（net use 到 \\\\localhost\\C$）—— 运维最常用的形态。"""
    letter = "Z:"
    target = r"\\localhost\C$"
    mapped = False
    try:
        check = subprocess.run(["net", "use", letter], capture_output=True, text=True,
                               timeout=20)
        if check.returncode != 0:
            made = subprocess.run(["net", "use", letter, target, "/persistent:no"],
                                  capture_output=True, text=True, timeout=30)
            if made.returncode != 0:
                record("映射盘符可用", False,
                       f"net use 失败: {(made.stderr or made.stdout).strip()[:120]}")
                return None
            mapped = True
        record("映射盘符可用", True, f"{letter} -> {target}" + ("（本次新建）" if mapped else "（已存在）"))
        root = Path(letter) / "Windows" / "Temp" / "aohuang-netfs-probe-mapped"
        root.mkdir(parents=True, exist_ok=True)
        db = root / "app.db"
        for suffix in ("", "-wal", "-shm", "-journal"):
            candidate = Path(str(db) + suffix)
            if candidate.exists():
                candidate.unlink()
        mode = setup_db(db)
        res = multi_process_write(db, processes=4, per_process=10)
        back = read_back(db)
        record("映射盘 4 进程 × 10 写全部成功",
               res["ok"] == res["expected"],
               f"{res['ok']}/{res['expected']} wall={res['wallSeconds']}s "
               f"err={res['errorCount']} {res['errors'][:2]}")
        record("映射盘 integrity_check=ok", back["integrity"] == "ok",
               f"{back['integrity']} rows={back['rows']}")
        return {"root": str(root), "mode": mode, "write": res, "readback": back}
    finally:
        if mapped:
            subprocess.run(["net", "use", letter, "/delete", "/y"],
                           capture_output=True, text=True, timeout=30)


def case_network_interrupt() -> dict:
    """模拟网络中断：写入后立刻把底层连接掐掉，看 WAL 是否留下半截事务。

    做法：在 UNC 上建库并写入，然后**不 commit 就退出进程**，再读回。
    这模拟的是「客户端写到一半网断了」——官方警告的正是这个场景：
    网络盘上的锁与 fsync 不可靠，可能出现「一边以为提交了，另一边没看到」。
    """
    unc_root = Path(r"\\localhost\C$\Windows\Temp\aohuang-netfs-probe")
    if not unc_root.exists():
        record("网络中断模拟", False, "UNC 目录不可用，跳过")
        return {}
    db = unc_root / "interrupt.db"
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = Path(str(db) + suffix)
        if candidate.exists():
            candidate.unlink()
    setup_db(db)
    child = r'''
import sqlite3, sys, os
path = sys.argv[1]
con = sqlite3.connect(path, timeout=10.0, isolation_level=None)
con.execute("PRAGMA busy_timeout=10000")
con.execute("BEGIN IMMEDIATE")
con.execute("INSERT INTO items(tag, n) VALUES ('half', 1)")
# 故意不 COMMIT，直接硬退出：模拟写到一半网络断掉 / 客户端进程被杀
os._exit(9)
'''
    child_file = Path(tempfile.gettempdir()) / "_aohuang_netfs_interrupt.py"
    child_file.write_text(child, encoding="utf-8")
    proc = subprocess.run([sys.executable, str(child_file), str(db)],
                          capture_output=True, text=True, timeout=60)
    back = read_back(db)
    # 未提交事务必须**看不到**；看得到就是原子性在网络盘上失效了
    committed_lost = back["rows"] != 0
    record("未提交事务在网络盘上不可见（原子性）", not committed_lost,
           f"rows={back['rows']} integrity={back['integrity']} "
           f"childExit={proc.returncode}")
    # 关键：恢复后库还能不能正常用
    con = sqlite3.connect(str(db), timeout=10.0, isolation_level=None)
    try:
        con.execute("PRAGMA busy_timeout=10000")
        con.execute("BEGIN IMMEDIATE")
        con.execute("INSERT INTO items(tag, n) VALUES ('after', 1)")
        con.execute("COMMIT")
        usable = True
        err = ""
    except Exception as exc:  # noqa: BLE001
        usable = False
        err = f"{type(exc).__name__}: {exc}"
    finally:
        con.close()
    record("中断后库仍可写（能恢复）", usable, err or "ok")
    return {"rowsAfterInterrupt": back["rows"], "integrity": back["integrity"],
            "recoverable": usable, "error": err}


def case_latency() -> dict:
    """同一份写入负载在本地盘 / UNC / 映射盘上的耗时对比。

    这一项才是「网络盘不能放 SQLite」在**能连通的正常网络**下最直接的证据：
    每次 commit 都要走一遍网络往返 + fsync，几百次小事务就慢到不可用。
    """
    def bench(root: Path, label: str) -> dict:
        root.mkdir(parents=True, exist_ok=True)
        db = root / f"lat-{label}.db"
        for suffix in ("", "-wal", "-shm", "-journal"):
            candidate = Path(str(db) + suffix)
            if candidate.exists():
                candidate.unlink()
        con = sqlite3.connect(str(db), timeout=30.0, isolation_level=None)
        try:
            con.execute("PRAGMA busy_timeout=30000")
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("CREATE TABLE items(id INTEGER PRIMARY KEY, tag TEXT, n INTEGER)")
            # 预热：第一次访问网络盘要建连接、协商 SMB，不能算进平均
            con.execute("BEGIN IMMEDIATE")
            con.execute("INSERT INTO items(tag, n) VALUES ('warm', 0)")
            con.execute("COMMIT")
            rounds = 200
            started = time.perf_counter()
            for index in range(rounds):
                con.execute("BEGIN IMMEDIATE")
                con.execute("INSERT INTO items(tag, n) VALUES ('b', ?)", (index,))
                con.execute("COMMIT")
            elapsed = time.perf_counter() - started
        finally:
            con.close()
        return {"label": label, "rounds": rounds, "seconds": round(elapsed, 3),
                "avgMs": round(elapsed / rounds * 1000, 3)}

    rows = []
    tmp = Path(tempfile.mkdtemp(prefix="netfs-lat-"))
    try:
        rows.append(bench(tmp, "local"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    unc_root = Path(r"\\localhost\C$\Windows\Temp\aohuang-netfs-probe")
    if unc_root.exists():
        rows.append(bench(unc_root, "unc"))
        mapped = Path("Z:") / "Windows" / "Temp" / "aohuang-netfs-probe"
        # 映射盘只在前面用例里临时建过；这里不重建，能写就测
        if mapped.exists():
            rows.append(bench(mapped, "mapped"))
    base = rows[0]["avgMs"] if rows else 0
    for row in rows:
        row["vsLocal"] = round(row["avgMs"] / base, 2) if base else 0
    detail = " | ".join(f"{row['label']} {row['avgMs']}ms/txn "
                        f"(×{row['vsLocal']})" for row in rows)
    record("网络盘写入延迟对比本地盘", True, detail)
    return {"rows": rows}


def main() -> int:
    print(f"SQLite {sqlite3.sqlite_version} / Python {sys.version.split()[0]}\n")
    report: dict = {"sqlite": sqlite3.sqlite_version,
                    "python": sys.version.split()[0]}
    print("=== 对照组：本地 NTFS ===")
    report["local"] = case_local_baseline()
    print("\n=== UNC 路径（回环 SMB，\\\\localhost\\C$）===")
    report["unc"] = case_unc()
    print("\n=== 映射盘符（net use Z: \\\\localhost\\C$）===")
    report["mapped"] = case_mapped_drive()
    print("\n=== 网络中断模拟 ===")
    report["interrupt"] = case_network_interrupt()
    print("\n=== 写入延迟：本地盘 vs UNC vs 映射盘 ===")
    report["latency"] = case_latency()

    out = Path(__file__).with_name("probe_network_fs.json")
    out.write_text(json.dumps({**report, "results": RESULTS},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    passed = sum(1 for row in RESULTS if row["ok"])
    print(f"\nTOTAL {passed} PASS {len(RESULTS) - passed} FAIL")
    print(f"明细 JSON: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
