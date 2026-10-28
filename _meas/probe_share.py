"""共享部署运维面探针：验三条新接口 + 并发写 + VACUUM INTO 备份。

红线：`AOHUANG_DATA_DIR` 一律指到临时目录，**绝不碰真实 app.db**。
脚本自己在临时目录里建库、建商品、跑并发、做备份，退出时删干净。

跑法（在 tk-desktop 根目录）：
    python _meas/probe_share.py

输出：`_meas/probe_share.json` + 终端逐条 PASS/FAIL。
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
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
sys.path.insert(0, str(SERVER))

# 数据目录必须在 import main 之前定死：db/session.py 在模块导入阶段就建 engine。
_TMP_ROOT = Path(tempfile.mkdtemp(prefix="aohuang-share-probe-"))
os.environ["AOHUANG_DATA_DIR"] = str(_TMP_ROOT)
os.environ["TK_AUTOMATION_DATA_DIR"] = str(_TMP_ROOT)
os.environ["AOHUANG_SHARED_MODE"] = "1"

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

RESULTS: list[tuple[str, bool, str]] = []
DATA: dict[str, object] = {}

# products 表里 NOT NULL 且无默认值的列必须逐个给值（shop_name 也在其中，
# 少给一个就是 IntegrityError，而不是「插进去了但字段为空」）。
INSERT_PRODUCT = (
    "INSERT INTO products (warehouse_key, detail_id, shop_id, shop_name, title, "
    "language, site, cover, cid, category_name, group_name, "
    "price_currency, stock, sales_last_30, sku_count, images, "
    "detail_images, sku_properties, payload, status, claimed, "
    "collected, saved_at, updated_at) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def product_row(key: str, detail_id: str, title: str = "x") -> tuple:
    """一行合法的商品数据（列顺序与 INSERT_PRODUCT 对齐）。"""
    return (key, detail_id, "s1", "探针店铺", title,
            "ms", "MY", "", "", "", "",
            "CNY", 0, 0, 0, "[]", "[]", "[]", "{}", "ready", 0,
            "{}", time.time(), time.time())


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), str(detail)))


# --- 并发写：直连 sqlite3，绕开 HTTP 层，测的是 SQLite 本身 -------------------
def concurrent_writes(db_file: Path, threads: int, busy_timeout: int,
                      payload_size: int = 1) -> dict[str, object]:
    """N 个线程各写一条商品，返回成功/失败/锁冲突/耗时。

    用**独立连接**（每线程一个），这是共享部署的真实形态：多个客户端 → 多个
    uvicorn 请求 → 连接池里不同连接。共用一个连接测不出锁竞争。
    """
    successes = 0
    locked = 0
    other_errors: list[str] = []
    durations: list[float] = []
    guard = threading.Lock()
    barrier = threading.Barrier(threads)

    def worker(index: int) -> None:
        nonlocal successes, locked
        started = time.perf_counter()
        try:
            con = sqlite3.connect(str(db_file), timeout=busy_timeout / 1000.0)
            try:
                con.execute(f"PRAGMA busy_timeout={busy_timeout}")
                con.execute("PRAGMA journal_mode=WAL")
                # 同时起跑，把竞争压到最窄的时间窗里
                barrier.wait(timeout=10)
                con.execute(INSERT_PRODUCT, product_row(
                    f"probe-{index}-{time.time_ns()}", str(index), "x" * payload_size))
                con.commit()
                with guard:
                    successes += 1
            finally:
                con.close()
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            with guard:
                if "locked" in message or "busy" in message:
                    locked += 1
                else:
                    other_errors.append(f"{type(exc).__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            with guard:
                other_errors.append(f"{type(exc).__name__}: {exc}")
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
        "success": successes,
        "failed": len(other_errors),
        "locked": locked,
        "otherErrors": other_errors[:5],
        "wallSeconds": round(wall, 3),
        "avgMs": round(sum(durations) / len(durations) * 1000, 1) if durations else 0.0,
        "p50Ms": round(ordered[len(ordered) // 2] * 1000, 1) if ordered else 0.0,
        "maxMs": round(ordered[-1] * 1000, 1) if ordered else 0.0,
    }


def vacuum_into(src: Path, dest: Path) -> dict[str, object]:
    """在线备份：`VACUUM INTO`。**不能直接 copy .db** —— WAL 里还有没 checkpoint 的数据。"""
    started = time.perf_counter()
    con = sqlite3.connect(str(src), timeout=15.0)
    try:
        con.execute("PRAGMA busy_timeout=15000")
        con.execute("VACUUM INTO ?", (str(dest),))
    finally:
        con.close()
    elapsed = time.perf_counter() - started

    check_con = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
    try:
        integrity = check_con.execute("PRAGMA integrity_check").fetchone()
        rows = check_con.execute("SELECT COUNT(*) FROM products").fetchone()
        journal = check_con.execute("PRAGMA journal_mode").fetchone()
    finally:
        check_con.close()
    src_con = sqlite3.connect(str(src), timeout=5.0)
    try:
        src_rows = src_con.execute("SELECT COUNT(*) FROM products").fetchone()
    finally:
        src_con.close()
    return {
        "seconds": round(elapsed, 3),
        "sizeBytes": dest.stat().st_size,
        "integrity": str(integrity[0]) if integrity else "",
        "backupRows": int(rows[0]) if rows else -1,
        "sourceRows": int(src_rows[0]) if src_rows else -1,
        "journalMode": str(journal[0]) if journal else "",
    }


def raw_copy_check(src: Path, dest: Path) -> dict[str, object]:
    """对照实验：直接 copy app.db（不带 -wal），看会不会丢数据。"""
    shutil.copy2(src, dest)
    con = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT COUNT(*) FROM products").fetchone()
    finally:
        con.close()
    return {"rows": int(rows[0]) if rows else -1}


def http_get(base: str, path: str, token: str | None = None,
             timeout: float = 5.0) -> tuple[int, dict | str]:
    url = f"{base}{path}"
    request = urllib.request.Request(url)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            return response.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, body
    except (urllib.error.URLError, OSError) as exc:
        return 0, f"{type(exc).__name__}: {exc}"


def http_post(base: str, path: str, payload: dict, token: str | None = None,
              timeout: float = 5.0) -> tuple[int, dict | str]:
    request = urllib.request.Request(
        f"{base}{path}", data=json.dumps(payload).encode("utf-8"), method="POST")
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            return response.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, body
    except (urllib.error.URLError, OSError) as exc:
        return 0, f"{type(exc).__name__}: {exc}"


def main() -> int:
    from fastapi.testclient import TestClient

    import main as server_main
    from db.session import engine, init_db

    init_db()
    data_dir = Path(os.environ["AOHUANG_DATA_DIR"])
    db_file = data_dir / "app.db"
    check("数据目录指向临时目录（不碰真实 app.db）",
          "aohuang-share-probe-" in str(data_dir), str(data_dir))
    DATA["dataDir"] = str(data_dir)

    token = server_main.get_settings().local_secret()
    DATA["tokenPresent"] = bool(token)

    client = TestClient(server_main.app)
    # TestClient 的 client.host 是 "testclient"，在线表照样能记；不影响判定。

    # ---------- 1. GET /system/share/health 免鉴权 ----------
    health_no_auth = client.get("/api/v1/system/share/health")
    check("GET /system/share/health 不带 token 返回 200",
          health_no_auth.status_code == 200, f"HTTP {health_no_auth.status_code}")
    health_body = health_no_auth.json() if health_no_auth.status_code == 200 else {}
    DATA["shareHealth"] = health_body
    check("health 含 ok / serverVersion / uptimeSeconds / dbWritable / operatorCount",
          all(key in health_body for key in
              ("ok", "serverVersion", "uptimeSeconds", "dbWritable", "operatorCount")),
          str(sorted(health_body))[:200])
    check("health 不泄露 dbPath",
          "dbPath" not in health_body and "dataDir" not in health_body,
          str(sorted(health_body))[:200])

    # ---------- 2. GET /system/share 需要 token ----------
    no_token = client.get("/api/v1/system/share")
    check("GET /system/share 无 token 返回 401", no_token.status_code == 401,
          f"HTTP {no_token.status_code}")
    auth = {"Authorization": f"Bearer {token}"}
    with_token = client.get("/api/v1/system/share", headers=auth)
    check("GET /system/share 带 token 返回 200", with_token.status_code == 200,
          f"HTTP {with_token.status_code}")
    share_body = with_token.json() if with_token.status_code == 200 else {}
    DATA["share"] = share_body
    required = ("sharedMode", "bindHost", "port", "lanAddresses", "clientCount",
                "dbPath", "dbSizeBytes", "walEnabled", "activeOperators")
    check("share 含全部约定字段", all(key in share_body for key in required),
          f"缺 {[k for k in required if k not in share_body]}")

    # ---------- 3. lanAddresses 真能列出非回环地址 ----------
    addresses = share_body.get("lanAddresses") or []
    DATA["lanAddresses"] = addresses
    check("lanAddresses 过滤掉 127.x / 169.254.x",
          all(not str(ip).startswith(("127.", "169.254.")) for ip in addresses),
          str(addresses))
    check("lanAddresses 至少一个非回环地址（本机只有回环则允许为空）",
          bool(addresses), f"{len(addresses)} 个: {addresses}")
    check("shareUrls 与 lanAddresses 一一对应",
          len(share_body.get("shareUrls") or []) == len(addresses),
          str(share_body.get("shareUrls"))[:200])

    # ---------- 4. WAL / busy_timeout ----------
    check("walEnabled 为 true", share_body.get("walEnabled") is True,
          str(share_body.get("walEnabled")))
    check("busyTimeoutMs 已上报", int(share_body.get("busyTimeoutMs") or 0) > 0,
          f"{share_body.get('busyTimeoutMs')} ms")
    check("dbSizeBytes > 0", int(share_body.get("dbSizeBytes") or 0) > 0,
          f"{share_body.get('dbSizeBytes')} bytes")

    # ---------- 5. POST /system/share 落库 + restartRequired ----------
    post = client.post("/api/v1/system/share", json={"sharedMode": True}, headers=auth)
    check("POST /system/share 返回 200", post.status_code == 200,
          f"HTTP {post.status_code}")
    post_body = post.json() if post.status_code == 200 else {}
    DATA["postShare"] = post_body
    check("POST /system/share 返回 restartRequired=true",
          post_body.get("restartRequired") is True, str(post_body.get("restartRequired")))
    check("POST /system/share 给出重启后地址",
          bool(post_body.get("bindHost")), str(post_body.get("bindHost")))

    # 落库验证：直接查 settings 表，而不是只看响应
    from services import setting_service
    from db.session import session_scope
    with session_scope() as session:
        stored = setting_service.get(session, "server.sharedMode")
    check("sharedMode 真的写进 settings 表", stored is True, repr(stored))

    # 切回 false 再切 true，确认可来回切
    post_off = client.post("/api/v1/system/share", json={"sharedMode": False},
                           headers=auth)
    check("POST sharedMode=false 也能落库",
          post_off.status_code == 200 and post_off.json().get("sharedMode") is False,
          f"HTTP {post_off.status_code} {post_off.json() if post_off.status_code == 200 else ''}")
    client.post("/api/v1/system/share", json={"sharedMode": True}, headers=auth)

    bad = client.post("/api/v1/system/share", json={"sharedMode": "maybe"}, headers=auth)
    check("POST sharedMode 非法值返回 400", bad.status_code == 400,
          f"HTTP {bad.status_code}")

    # ---------- 6. 在线客户端计数 ----------
    share_again = client.get("/api/v1/system/share", headers=auth).json()
    DATA["clientCount"] = share_again.get("clientCount")
    check("clientCount 至少 1（刚才的请求被记下）",
          int(share_again.get("clientCount") or 0) >= 1,
          str(share_again.get("clientCount")))
    operators = share_again.get("activeOperators") or []
    check("activeOperators 里不含 token 字段",
          all("token" not in json.dumps(row).lower() for row in operators),
          json.dumps(operators[:1], ensure_ascii=False)[:200])

    # ---------- 7. 并发写实测 ----------
    engine.dispose()  # 让 SQLAlchemy 松手，下面用独立连接压
    runs = []
    for busy in (5000, 15000):
        runs.append(concurrent_writes(db_file, threads=10, busy_timeout=busy))
    # 20 线程再压一轮，看失败率拐点
    runs.append(concurrent_writes(db_file, threads=20, busy_timeout=15000))
    DATA["concurrent"] = runs
    for row in runs:
        check(f"并发写 {row['threads']} 线程 / busy_timeout={row['busyTimeoutMs']}ms "
              f"成功 {row['success']} 失败 {row['failed']} 锁冲突 {row['locked']}",
              row["success"] == row["threads"],
              f"wall={row['wallSeconds']}s avg={row['avgMs']}ms max={row['maxMs']}ms")

    # 单写者：20 次串行写，测单次延迟
    engine.dispose()
    started = time.perf_counter()
    serial = 0
    con = sqlite3.connect(str(db_file), timeout=15.0)
    try:
        con.execute("PRAGMA busy_timeout=15000")
        for index in range(20):
            con.execute(INSERT_PRODUCT, product_row(
                f"serial-{index}-{time.time_ns()}", str(index)))
            con.commit()
            serial += 1
    finally:
        con.close()
    serial_elapsed = time.perf_counter() - started
    DATA["serial"] = {"count": serial, "seconds": round(serial_elapsed, 3),
                      "avgMs": round(serial_elapsed / max(serial, 1) * 1000, 2)}
    check("串行写 20 次全成功", serial == 20,
          f"{serial}/20, 总计 {serial_elapsed:.3f}s, 单次 {serial_elapsed / 20 * 1000:.2f}ms")

    # ---------- 8. VACUUM INTO 备份 ----------
    engine.dispose()
    backup_dir = data_dir / "backups"
    backup_dir.mkdir(exist_ok=True)
    backup = backup_dir / "app-20260101-000000.db"
    vac = vacuum_into(db_file, backup)
    DATA["vacuum"] = vac
    check("VACUUM INTO 产物 integrity_check=ok", vac["integrity"] == "ok",
          str(vac["integrity"]))
    check("VACUUM INTO 商品数与源库一致",
          vac["backupRows"] == vac["sourceRows"] and vac["sourceRows"] > 0,
          f"备份 {vac['backupRows']} / 源 {vac['sourceRows']}")
    check("VACUUM INTO 产物是独立单文件（无 -wal 依赖）",
          backup.exists() and not Path(str(backup) + "-wal").exists(),
          str(backup))

    # 对照：直接 copy .db（WAL 里还有未 checkpoint 的数据）
    #
    # 关键细节：**必须让写连接活着**。SQLite 在最后一个连接关闭时会做一次
    # checkpoint 并删掉 -wal，那时候裸 copy 也能拿到全量，对照实验就失效了。
    # 真实共享服务器上恰恰相反：服务一直开着，WAL 一直有未合并的数据。
    engine.dispose()
    con = sqlite3.connect(str(db_file), timeout=15.0)
    try:
        con.execute("PRAGMA wal_autocheckpoint=0")  # 关掉自动 checkpoint
        for index in range(5):
            con.execute(INSERT_PRODUCT, product_row(
                f"walonly-{index}-{time.time_ns()}", str(900 + index)))
        con.commit()
        wal_path = Path(str(db_file) + "-wal")
        wal_size = wal_path.stat().st_size if wal_path.exists() else 0
        live_rows = con.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        # 连接没关，WAL 里这 5 行还在，此刻裸 copy 主库文件
        raw = raw_copy_check(db_file, backup_dir / "rawcopy.db")
        vac2 = vacuum_into(db_file, backup_dir / "app-20260101-000001.db")
    finally:
        con.close()
    DATA["walComparison"] = {"walSizeBytes": wal_size, "rawCopyRows": raw["rows"],
                             "vacuumRows": vac2["backupRows"],
                             "liveRows": int(live_rows),
                             "sourceRows": vac2["sourceRows"]}
    check("WAL 里确实有未 checkpoint 的数据（对照实验前提成立）",
          wal_size > 0 and raw["rows"] < int(live_rows),
          f"wal={wal_size}B copy={raw['rows']} 活库={live_rows}")
    check("裸 copy .db 会漏掉 WAL 里的行（证明不能直接 copy）",
          raw["rows"] < vac2["sourceRows"],
          f"copy={raw['rows']} vacuum={vac2['backupRows']} "
          f"source={vac2['sourceRows']} wal={wal_size}B")

    return report()


def report() -> int:
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = len(RESULTS) - passed
    payload = {
        "total": len(RESULTS), "pass": passed, "fail": failed,
        "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "python": sys.version.split()[0],
        "sqlite": sqlite3.sqlite_version,
        "cases": [{"name": name, "ok": ok, "detail": detail}
                  for name, ok, detail in RESULTS],
        "measurements": DATA,
    }
    out = Path(__file__).with_name("probe_share.json")
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    for name, ok, detail in RESULTS:
        print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail else ""))
    print(f"\nTOTAL {passed} PASS {failed} FAIL")
    print(f"明细 JSON: {out}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        shutil.rmtree(_TMP_ROOT, ignore_errors=True)
