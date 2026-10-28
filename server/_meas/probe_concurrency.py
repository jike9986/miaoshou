"""并发探针：真的开两个线程去撞同一件商品，量「谁赢、谁拿到 409、上游被打了几次」。

为什么要有这个探针：`probe_multiuser.py` 只扫源码（「有没有 revision 字段」
「有没有条件 UPDATE」），它证明不了保护真的生效 —— 加了字段但判定写错，
静态扫描照样全绿。这里用真线程 + 真 SQLite + 每个线程自己的会话，
按 `api/*.py` 的真实时序（服务函数 → 审计 → commit）跑一遍。

六组用例，对应六类真实事故：
  · 乐观锁：两人同时保存同一件商品 → 必须恰好一个成功、一个 409
  · 认领：两人同时点认领 → 上游 `claim_to_shop` 只能被调用 1 次
  · 认领时间窗：刚认领完再点一次 → 409 且文案里有时间
  · 发布：两人同时发同一件 → 上游 `publish_items` 只能被调用 1 次
  · 批次：两人同时建同一个店铺的批次 → 库里只能有 1 条 pending
  · 配置/信封：shared_mode 真的改监听地址；409 走统一错误信封

铁律（见 _meas/README.md）：`AOHUANG_DATA_DIR` 必须指到临时目录。
本探针额外强调一次：这里会真的写库，指错地方就是往用户的 355 件商品里写脏数据。

控制台只输出 ASCII（Windows 终端会把中文吃成乱码），中文细节写进
`_meas/_out/probe_concurrency.json`。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 必须在 import 应用代码之前设好：core.config 用 @lru_cache 缓存数据目录
_TMP_DIR = Path(tempfile.mkdtemp(prefix="concurrency-probe-"))
os.environ["AOHUANG_DATA_DIR"] = str(_TMP_DIR)
os.environ["TK_AUTOMATION_DATA_DIR"] = str(_TMP_DIR)

RESULTS: list[tuple[str, bool, str]] = []
# 数值证据：报告里要能引用「上游被打了几次」这种具体数字
METRICS: dict[str, object] = {}


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), str(detail)))


# --- 并发脚手架 -----------------------------------------------------------
def pair(first, second) -> list[dict]:
    """让两个函数尽量同时开跑，各自返回结果字典。

    用 Barrier 而不是 sleep：sleep 只能「大概同时」，而这里要测的正是
    「恰好同时」那一下。超时兜底是为了线程里抛异常时不把另一个永久卡住。
    """
    barrier = threading.Barrier(2)

    def wrap(fn):
        def inner() -> dict:
            try:
                barrier.wait(timeout=10)
            except threading.BrokenBarrierError:
                return {"ok": False, "code": "BARRIER", "message": "barrier broken"}
            try:
                return fn()
            except BaseException as exc:  # noqa: BLE001 - 线程里必须自己收口
                return {"ok": False, "code": type(exc).__name__,
                        "message": str(exc)[:200],
                        "trace": traceback.format_exc()[-600:]}
        return inner

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(wrap(first)), pool.submit(wrap(second))]
        return [future.result() for future in futures]


def outcome(out: dict) -> str:
    return "ok" if out.get("ok") else str(out.get("code") or "?")


def summarize(outs: list[dict]) -> str:
    return " / ".join(outcome(o) for o in outs)


class Counter:
    """线程安全的上游调用计数器。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls: list[str] = []

    def hit(self, label: str) -> None:
        with self._lock:
            self.calls.append(label)

    def __len__(self) -> int:
        with self._lock:
            return len(self.calls)


# --- 假上游 ---------------------------------------------------------------
class FakeClient:
    """够用的假妙手客户端。所有调用都记数，用来断言「上游被打了几次」。"""

    def __init__(self, counter: Counter, *, delay: float = 0.0) -> None:
        self.counter = counter
        self.delay = delay
        self.closed = False

    def claim_to_shop(self, detail_ids, shop_ids):  # noqa: ANN001
        self.counter.hit("claim_to_shop")
        if self.delay:
            import time

            time.sleep(self.delay)
        return {"data": {"detailIds": [str(v) for v in detail_ids]}}

    def publish_items(self, detail_ids, shop_ids):  # noqa: ANN001
        self.counter.hit("publish_items")
        if self.delay:
            import time

            time.sleep(self.delay)
        return {"data": {"detailIds": [str(v) for v in detail_ids]}}

    def shop_detail(self, detail_id, shop_id):  # noqa: ANN001
        self.counter.hit("shop_detail")
        return {"data": {}}

    def shop_list(self, **kwargs):  # noqa: ANN003
        self.counter.hit("shop_list")
        return {"data": {"shopList": []}}

    def close(self) -> None:
        self.closed = True


# --- 公共夹具 -------------------------------------------------------------
SHOP_A = "18077147"
SHOP_B = "18077148"


def make_account(db) -> int:  # noqa: ANN001
    """建一个账号，店铺走本地缓存（不走网络）。

    `cached_shops` 读的是 accounts.shops 这个 JSON 列，只要它里面有店铺，
    `require_shop` 就不会回源。这是让探针不联网的关键。
    """
    from models.account import Account

    shops = [
        {"shopId": SHOP_A, "name": "店A", "site": "MY"},
        {"shopId": SHOP_B, "name": "店B", "site": "MY"},
    ]
    row = Account(id=1, name="并发探针", app_key="probe-key",
                  app_secret_enc="not-used", site="MY", status="active",
                  is_default=True, shops=json.dumps(shops, ensure_ascii=False),
                  shops_synced_at=1.0, created_at=1.0, updated_at=1.0)
    db.add(row)
    db.commit()
    return int(row.id)


def make_product(db, detail_id: str, shop_id: str, *, revision: int = 1,
                 title: str = "probe product") -> int:  # noqa: ANN001
    from core.utils import product_key
    from models.product import Product

    row = Product(warehouse_key=product_key(detail_id, shop_id),
                  account_id=1, detail_id=str(detail_id), shop_id=str(shop_id),
                  title=title, status="ready", revision=revision,
                  claimed=False, collected={}, payload={},
                  saved_at=1.0, updated_at=1.0)
    db.add(row)
    db.commit()
    return int(row.id)


# --- 1. 乐观锁：非并发语义 ------------------------------------------------
def test_revision_semantics(db) -> None:  # noqa: ANN001
    """`expected_revision` 的三种取值各自什么行为。

    这三条是「向后兼容」与「保护」之间那条线的全部内容，必须逐条钉住。
    """
    from core import errors
    from services import warehouse_service

    pid = make_product(db, "4100001", SHOP_A)

    # (a) 带对版本号 → 成功，且版本号推进到 2
    got = warehouse_service.update(db, pid, title="probe renamed one",
                                   expected_revision=1)
    db.commit()
    check("revision: correct expected -> ok",
          got["product"]["revision"] == 2, str(got["product"]["revision"]))
    check("revision: serialized revision present", "revision" in got["product"], "")

    # (b) 不带版本号 → 不校验（向后兼容），但版本号仍要推进
    got = warehouse_service.update(db, pid, title="probe renamed two")
    db.commit()
    check("revision: expected=None still writes (compat)",
          got["product"]["revision"] == 3, str(got["product"]["revision"]))

    # (c) 带错版本号 → 409，且 details 里三个键都在
    try:
        warehouse_service.update(db, pid, title="probe renamed three",
                                 expected_revision=1)
    except errors.AppError as exc:
        details = exc.details or {}
        check("revision: stale expected -> 409 REVISION_CONFLICT",
              exc.code == errors.REVISION_CONFLICT and exc.status == 409,
              f"{exc.code}/{exc.status}")
        check("revision: details carry expected/actual/hint",
              {"expected", "actual", "hint"} <= set(details),
              str(sorted(details)))
        check("revision: details numbers are right",
              details.get("expected") == 1 and details.get("actual") == 3,
              f"{details.get('expected')} vs {details.get('actual')}")
        METRICS["revision_conflict_details"] = {
            key: details.get(key) for key in ("expected", "actual", "hint")}
    else:
        check("revision: stale expected -> 409 REVISION_CONFLICT", False,
              "no exception raised")
    db.rollback()


# --- 2. 乐观锁：真并发 ----------------------------------------------------
def test_revision_race(db) -> None:  # noqa: ANN001
    """两人同时保存同一件商品：必须恰好一个成功、一个 409。

    这是整个改动的核心断言。两个线程各开一个会话、都读到 revision=1、
    都带 expected_revision=1 去改 —— 只要判定不是原子的，两个都会成功。
    """
    from core import errors
    from db.session import SessionLocal
    from services import warehouse_service

    pid = make_product(db, "4100002", SHOP_A)

    def worker(tag: str):
        def run() -> dict:
            session = SessionLocal()
            try:
                product = warehouse_service.resolve_product(session, pid)
                seen = int(product.revision)
                result = warehouse_service.update(
                    session, pid, title=f"probe race {tag}",
                    expected_revision=seen)
                session.commit()
                return {"ok": True, "code": "ok",
                        "revision": result["product"]["revision"],
                        "seen": seen}
            except errors.AppError as exc:
                session.rollback()
                return {"ok": False, "code": exc.code, "status": exc.status,
                        "message": exc.message,
                        "details": exc.details}
            finally:
                session.close()
        return run

    outs = pair(worker("A"), worker("B"))
    ok_count = sum(1 for o in outs if o.get("ok"))
    conflict_count = sum(1 for o in outs if o.get("code") == "REVISION_CONFLICT")
    check("race: exactly one write wins", ok_count == 1, summarize(outs))
    check("race: exactly one gets 409 REVISION_CONFLICT", conflict_count == 1,
          summarize(outs))
    check("race: total outcomes equal thread count",
          ok_count + conflict_count == 2, summarize(outs))

    db.expire_all()
    from models.product import Product

    final = db.query(Product).filter(Product.id == pid).one()
    check("race: revision advanced exactly once (1 -> 2)",
          int(final.revision) == 2, str(final.revision))
    winner = next((o for o in outs if o.get("ok")), {})
    check("race: winner reports revision 2", winner.get("revision") == 2,
          str(winner.get("revision")))
    loser = next((o for o in outs if not o.get("ok")), {})
    check("race: loser sees actual=2", (loser.get("details") or {}).get("actual") == 2,
          str((loser.get("details") or {}).get("actual")))
    METRICS["revision_race"] = {
        "outcomes": [outcome(o) for o in outs],
        "final_revision": int(final.revision),
        "loser_details": loser.get("details"),
    }


def test_revision_rounds(db) -> None:  # noqa: ANN001
    """重复 20 轮，确认「恰好一个 409」不是运气，且没有 database is locked 漏出来。

    为什么单独跑这一轮：条件 UPDATE 与 SQLite 的写锁叠在一起时，最可能的
    坏结果是第二个线程拿到 `database is locked`（500）而不是 409 ——
    那等于「保护生效了，但用户看到的是服务器错误」。这 20 轮就是量这个。
    """
    from core import errors
    from db.session import SessionLocal
    from services import warehouse_service

    bad: list[str] = []
    locked = 0
    for index in range(20):
        pid = make_product(db, f"41001{index:02d}", SHOP_A)

        def worker(tag: str, product_id: int):
            def run() -> dict:
                session = SessionLocal()
                try:
                    product = warehouse_service.resolve_product(session, product_id)
                    result = warehouse_service.update(
                        session, product_id, title=f"probe round {tag}",
                        expected_revision=int(product.revision))
                    session.commit()
                    return {"ok": True, "code": "ok",
                            "revision": result["product"]["revision"]}
                except errors.AppError as exc:
                    session.rollback()
                    return {"ok": False, "code": exc.code, "message": exc.message}
                except Exception as exc:  # noqa: BLE001
                    session.rollback()
                    return {"ok": False, "code": type(exc).__name__,
                            "message": str(exc)[:120]}
                finally:
                    session.close()
            return run

        outs = pair(worker("A", pid), worker("B", pid))
        codes = sorted(outcome(o) for o in outs)
        if codes != ["REVISION_CONFLICT", "ok"]:
            bad.append(f"round {index}: {codes}")
        if any("locked" in str(o.get("message") or "").lower() for o in outs):
            locked += 1
    check("rounds: 20/20 exactly one ok + one conflict", not bad,
          "; ".join(bad[:4]))
    check("rounds: no 'database is locked' leaks", locked == 0, f"{locked} rounds")
    METRICS["revision_rounds"] = {"rounds": 20, "anomalies": len(bad),
                                  "locked_leaks": locked}


# --- 3. 认领：真并发 ------------------------------------------------------
def test_claim_race(db, counter: Counter) -> None:  # noqa: ANN001
    """两人同时点认领同一件商品：上游只能被打 1 次。

    这是「不可撤销」那条：`claim_to_shop` 打过去之后，妙手那边就多一条
    采集箱记录，本工具没有接口把它退回来。所以判据不是「界面有没有报错」，
    而是**上游调用次数**。
    """
    from core import errors
    from db.session import SessionLocal
    from services import collect_box_service

    detail = "4200001"
    make_product(db, detail, SHOP_A)

    def worker(tag: str):
        def run() -> dict:
            session = SessionLocal()
            try:
                from services.account_service import require_account

                account = require_account(session, 1)
                result = collect_box_service.claim(
                    session, account, detail_id=detail, shop_id=SHOP_A,
                    target_shop_id=SHOP_A)
                session.commit()
                return {"ok": True, "code": "ok",
                        "claimed": bool(result.get("claimed"))}
            except errors.AppError as exc:
                session.rollback()
                return {"ok": False, "code": exc.code, "status": exc.status,
                        "message": exc.message}
            finally:
                session.close()
        return run

    counter.calls.clear()
    outs = pair(worker("A"), worker("B"))
    ok_count = sum(1 for o in outs if o.get("ok"))
    busy_count = sum(1 for o in outs if o.get("code") == errors.CONFLICT_BUSY)
    check("claim race: exactly one succeeds", ok_count == 1, summarize(outs))
    check("claim race: the other gets 409 CONFLICT_BUSY", busy_count == 1,
          summarize(outs))
    check("claim race: upstream claim_to_shop called exactly once",
          len(counter) == 1, f"{len(counter)} calls")
    check("claim race: success count + failure count == threads",
          ok_count + busy_count == 2, summarize(outs))
    METRICS["claim_race"] = {"outcomes": [outcome(o) for o in outs],
                             "upstream_calls": len(counter)}


def test_claim_recent(db, counter: Counter) -> None:  # noqa: ANN001
    """刚认领完再点一次：409，且文案里有「什么时候被认领的」。

    这一层是锁管不到的场合的兜底（进程重启、有人另起一个实例）。
    只说「忙」没有用 —— 人需要知道该等多久、或者去找谁问。
    """
    from core import errors
    from db.session import SessionLocal
    from services import collect_box_service

    detail = "4200002"
    make_product(db, detail, SHOP_A)
    from services.account_service import require_account

    session = SessionLocal()
    try:
        account = require_account(session, 1)
        collect_box_service.claim(session, account, detail_id=detail,
                                  shop_id=SHOP_A, target_shop_id=SHOP_A)
        session.commit()
    finally:
        session.close()

    counter.calls.clear()
    session = SessionLocal()
    try:
        account = require_account(session, 1)
        collect_box_service.claim(session, account, detail_id=detail,
                                  shop_id=SHOP_A, target_shop_id=SHOP_A)
        check("claim recent: second claim -> 409", False, "no exception raised")
    except errors.AppError as exc:
        check("claim recent: second claim -> 409 CONFLICT_BUSY",
              exc.code == errors.CONFLICT_BUSY and exc.status == 409,
              f"{exc.code}/{exc.status}")
        check("claim recent: message names when it was claimed",
              "刚被认领" in exc.message, exc.message[:80])
        check("claim recent: details carry retryAfterSeconds",
              "retryAfterSeconds" in (exc.details or {}),
              str(sorted(exc.details or {})))
        METRICS["claim_recent"] = {"message": exc.message,
                                   "details": exc.details}
    finally:
        session.rollback()
        session.close()
    check("claim recent: blocked locally, upstream not called",
          len(counter) == 0, f"{len(counter)} calls")


def test_claim_different(db, counter: Counter) -> None:  # noqa: ANN001
    """两件不同的商品同时认领：都该成功。

    这条是防「过度互斥」的。锁键选错（比如用了全局锁或账号级键），
    两个人认领两件毫无关系的商品就会互相排队 —— 表现是「软件卡住了」。
    """
    from core import errors
    from db.session import SessionLocal
    from services import collect_box_service

    first_detail, second_detail = "4200003", "4200004"
    make_product(db, first_detail, SHOP_A)
    make_product(db, second_detail, SHOP_A)

    def worker(detail: str):
        def run() -> dict:
            session = SessionLocal()
            try:
                from services.account_service import require_account

                account = require_account(session, 1)
                result = collect_box_service.claim(
                    session, account, detail_id=detail, shop_id=SHOP_A,
                    target_shop_id=SHOP_A)
                session.commit()
                return {"ok": True, "code": "ok",
                        "claimed": bool(result.get("claimed"))}
            except errors.AppError as exc:
                session.rollback()
                return {"ok": False, "code": exc.code, "message": exc.message}
            finally:
                session.close()
        return run

    counter.calls.clear()
    outs = pair(worker(first_detail), worker(second_detail))
    check("claim different: both succeed", all(o.get("ok") for o in outs),
          summarize(outs))
    check("claim different: upstream called twice", len(counter) == 2,
          f"{len(counter)} calls")
    METRICS["claim_different"] = {"outcomes": [outcome(o) for o in outs],
                                  "upstream_calls": len(counter)}


# --- 4. 发布：真并发 ------------------------------------------------------
def publish_payload(detail: str) -> dict:
    return {"confirm": True, "shopId": SHOP_A, "accountId": "1",
            "items": [{"detailId": detail, "shopId": SHOP_A}],
            "language": "ms"}


def test_publish_race(db, counter: Counter) -> None:  # noqa: ANN001
    """两人同时发布同一件商品到同一店铺：上游只能被打 1 次。

    发布比认领更不可撤销：平台收到两次提交，商品就被建两遍。
    """
    from core import errors
    from db.session import SessionLocal
    from services import publish_service

    detail = "4300001"
    make_product(db, detail, SHOP_A)

    def worker(tag: str):
        def run() -> dict:
            session = SessionLocal()
            try:
                result = publish_service.publish(session, publish_payload(detail))
                session.commit()
                return {"ok": True, "code": "ok",
                        "published": result.get("published")}
            except errors.AppError as exc:
                session.rollback()
                return {"ok": False, "code": exc.code, "status": exc.status,
                        "message": exc.message}
            finally:
                session.close()
        return run

    counter.calls.clear()
    outs = pair(worker("A"), worker("B"))
    ok_count = sum(1 for o in outs if o.get("ok"))
    busy_count = sum(1 for o in outs if o.get("code") == errors.CONFLICT_BUSY)
    check("publish race: exactly one succeeds", ok_count == 1, summarize(outs))
    check("publish race: the other gets 409 CONFLICT_BUSY", busy_count == 1,
          summarize(outs))
    check("publish race: upstream publish_items called exactly once",
          len(counter) == 1, f"{len(counter)} calls")
    METRICS["publish_race"] = {"outcomes": [outcome(o) for o in outs],
                               "upstream_calls": len(counter)}


def test_publish_different(db, counter: Counter) -> None:  # noqa: ANN001
    """两件不同商品同时发布：都该成功（防止过度互斥）。"""
    from core import errors
    from db.session import SessionLocal
    from services import publish_service

    first_detail, second_detail = "4300002", "4300003"
    make_product(db, first_detail, SHOP_A)
    make_product(db, second_detail, SHOP_A)

    def worker(detail: str):
        def run() -> dict:
            session = SessionLocal()
            try:
                result = publish_service.publish(session, publish_payload(detail))
                session.commit()
                return {"ok": True, "code": "ok",
                        "published": result.get("published")}
            except errors.AppError as exc:
                session.rollback()
                return {"ok": False, "code": exc.code, "message": exc.message}
            finally:
                session.close()
        return run

    counter.calls.clear()
    outs = pair(worker(first_detail), worker(second_detail))
    check("publish different: both succeed", all(o.get("ok") for o in outs),
          summarize(outs))
    check("publish different: upstream called twice", len(counter) == 2,
          f"{len(counter)} calls")
    METRICS["publish_different"] = {"outcomes": [outcome(o) for o in outs],
                                    "upstream_calls": len(counter)}


# --- 5. 批次：真并发 ------------------------------------------------------
def test_batch_race(db) -> None:  # noqa: ANN001
    """两人同时给同一个店铺建批次：库里只能有 1 条 pending。

    两条 pending 覆盖同一批 detailId 的后果不是「多一条记录」——它们会
    各自从第 0 件开始跑，`prepare_weight` 不幂等，重量被加两次。
    """
    from core import errors
    from db.session import SessionLocal
    from models.batch import Batch
    from services import batch_service

    shop = "9701"

    def worker(tag: str):
        # detailId 必须是数字（服务层会校验），所以用 9701 / 9702 而不是 970A/970B
        detail = f"970{1 if tag == 'A' else 2}"

        def run() -> dict:
            session = SessionLocal()
            try:
                batch = batch_service.create(
                    session, account_id=1, shop_id=shop, detail_ids=[detail])
                session.commit()
                return {"ok": True, "code": "ok", "batchId": batch.id}
            except errors.AppError as exc:
                session.rollback()
                return {"ok": False, "code": exc.code, "status": exc.status,
                        "message": exc.message}
            finally:
                session.close()
        return run

    outs = pair(worker("A"), worker("B"))
    ok_count = sum(1 for o in outs if o.get("ok"))
    busy_count = sum(1 for o in outs if o.get("code") == errors.CONFLICT_BUSY)
    check("batch race: exactly one succeeds", ok_count == 1, summarize(outs))
    check("batch race: the other gets 409 CONFLICT_BUSY", busy_count == 1,
          summarize(outs))
    db.expire_all()
    rows = db.query(Batch).filter(Batch.shop_id == shop).all()
    check("batch race: only one pending batch in db", len(rows) == 1,
          f"{len(rows)} rows: {[r.id for r in rows]}")
    METRICS["batch_race"] = {"outcomes": [outcome(o) for o in outs],
                             "batches_in_db": len(rows)}


def test_lock_scope() -> None:
    """锁必须活到 commit，而不是活到服务函数返回。

    这是实测出来的第二个洞，静态扫描看不出来：`with 锁: ... ; db.commit()`
    这种写法看起来锁住了整段，实际上出 with 就放锁，而那时候改动还没提交，
    第二个人读不到、照样能做同一件事（上游被打两次、库里两条 pending）。

    判据落在源码上：三处必须用会话级锁（`session_lock` / `hold_for_session`），
    且不允许再出现「函数级 with 锁 + 外面 commit」的写法。
    """
    import inspect

    from services import batch_service, collect_box_service, publish_service

    claim_src = inspect.getsource(collect_box_service.claim)
    check("lock scope: claim uses session-scoped lock",
          "session_lock(" in claim_src, "")
    batch_src = inspect.getsource(batch_service.create)
    check("lock scope: batch.create uses session-scoped lock",
          "session_lock(" in batch_src, "")
    publish_src = inspect.getsource(publish_service.publish)
    check("lock scope: publish holds lock for the session",
          "hold_for_session(" in publish_src, "")
    check("lock scope: publish has no premature release in finally",
          "release_keys(" not in publish_src, "")

    # 路由里的 commit 必须在服务调用之后（现状如此，钉住它防止被改成自动提交）
    from api import batch as batch_api
    from api import publish as publish_api

    route_src = inspect.getsource(batch_api.create_batch)
    check("lock scope: batch route commits after service call",
          route_src.index("batch_service.create") < route_src.index("db.commit()"),
          "")
    publish_route = inspect.getsource(publish_api.publish)
    check("lock scope: publish route commits after service call",
          publish_route.index("publish_service.publish")
          < publish_route.index("db.commit()"), "")


# --- 6. 配置：shared_mode 真的改监听地址 ----------------------------------
def test_config() -> None:
    """`shared_mode` / `bind_host` 四个组合的监听地址。

    这条以前是纯摆设：开关打开只影响「自动更新」，后端照样只听 127.0.0.1，
    别人连不上，只能每人装一份 —— 于是「两个人同时改同一件商品」变成必然。
    """
    from core.config import Settings

    loop = Settings(shared_mode=False, bind_host="")
    check("config: shared_mode off -> loopback",
          loop.listen_host == "127.0.0.1", loop.listen_host)

    shared = Settings(shared_mode=True, bind_host="")
    check("config: shared_mode on -> all interfaces",
          shared.listen_host == "0.0.0.0", shared.listen_host)

    explicit = Settings(shared_mode=True, bind_host="192.168.1.5")
    check("config: explicit bind_host wins over shared_mode",
          explicit.listen_host == "192.168.1.5", explicit.listen_host)

    pinned = Settings(shared_mode=False, bind_host="10.0.0.9")
    check("config: bind_host works with shared_mode off",
          pinned.listen_host == "10.0.0.9", pinned.listen_host)

    # 打包入口和源码入口必须算出同一个地址，否则「源码能连、exe 连不上」
    run_server_src = (ROOT / "run_server.py").read_text(encoding="utf-8")
    main_src = (ROOT / "main.py").read_text(encoding="utf-8")
    check("config: run_server.py reads settings.listen_host",
          "settings.listen_host" in run_server_src, "")
    check("config: main.py reads settings.listen_host",
          "settings.listen_host" in main_src, "")

    METRICS["config"] = {
        "off": loop.listen_host, "on": shared.listen_host,
        "explicit": explicit.listen_host, "pinned": pinned.listen_host,
    }


def test_shared_warning() -> None:
    """共享模式的启动告示：文案要点 + 真的以 warning 级别写进日志。

    只说「共享模式已开启」没有用 —— 人需要知道这是**暴露面变化**、
    该确认什么（局域网可信、每人自己的 token）、以及怎么关掉。
    所以文案里必须出现这三件事，且必须走 warning 而不是 info。
    """
    import logging

    from core.config import Settings, get_settings
    import main as app_main

    loop_notice = app_main.shared_mode_warning(Settings(shared_mode=False, bind_host=""))
    check("warning: silent on loopback", loop_notice == "", repr(loop_notice))

    notice = app_main.shared_mode_warning(Settings(shared_mode=True, bind_host=""))
    check("warning: mentions all-interfaces exposure",
          "后端已对所有网卡开放" in notice, notice[:80])
    check("warning: tells you to trust the LAN", "局域网可信" in notice, "")
    check("warning: tells you everyone needs their own token",
          "每个人有自己的操作员 token" in notice, "")
    check("warning: tells you how to turn it off",
          "AOHUANG_SHARED_MODE" in notice, "")
    METRICS["shared_warning"] = notice

    # 真的跑一次 lifespan，确认它以 warning 级别落进日志（不是 info、不是 print）
    records: list[logging.LogRecord] = []

    class Grab(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    grab = Grab()
    target = logging.getLogger("aohuang.api")
    target.addHandler(grab)
    previous = os.environ.get("AOHUANG_SHARED_MODE")
    os.environ["AOHUANG_SHARED_MODE"] = "1"
    get_settings.cache_clear()
    try:
        import asyncio

        async def go() -> None:
            async with app_main.lifespan(app_main.app):
                return

        asyncio.run(go())
    except Exception as exc:  # noqa: BLE001 - 启动失败要如实记，不要吞
        check("warning: lifespan runs with shared_mode on", False,
              f"{type(exc).__name__}: {exc}"[:160])
    else:
        check("warning: lifespan runs with shared_mode on", True, "")
    finally:
        if previous is None:
            os.environ.pop("AOHUANG_SHARED_MODE", None)
        else:
            os.environ["AOHUANG_SHARED_MODE"] = previous
        get_settings.cache_clear()
        get_settings()
        target.removeHandler(grab)

    hits = [r for r in records
            if r.levelno >= logging.WARNING and "所有网卡" in r.getMessage()]
    check("warning: logged at WARNING level by lifespan", bool(hits),
          f"{len(records)} records captured")


# --- 7. 错误信封 ----------------------------------------------------------
def test_error_envelope(db) -> None:  # noqa: ANN001
    """409 必须走 `main.py` 的统一错误信封，不是 FastAPI 默认的 {"detail": ...}。

    前端是按 `error.code` 分支的：拿不到 code 就分不清「等一会儿再点」
    （CONFLICT_BUSY）和「刷新后重改」（REVISION_CONFLICT），只能给一句
    笼统的失败提示 —— 那等于这层保护对用户不存在。
    """
    from fastapi.testclient import TestClient
    from core.config import get_settings
    from db.session import SessionLocal
    from services import setting_service
    import main as app_main

    session = SessionLocal()
    try:
        setting_service.set_read_only(session, False)
        session.commit()
    finally:
        session.close()

    client = TestClient(app_main.app)
    headers = {"Authorization": f"Bearer {get_settings().local_secret()}"}

    # REVISION_CONFLICT：带一个肯定对不上的版本号
    pid = make_product(db, "4400001", SHOP_A)
    response = client.patch(f"/api/v1/warehouse/{pid}", headers=headers,
                            json={"title": "probe envelope", "expectedRevision": 99})
    body = response.json()
    error = (body or {}).get("error") or {}
    check("envelope: PATCH stale revision -> 409", response.status_code == 409,
          str(response.status_code))
    check("envelope: REVISION_CONFLICT code reaches the client",
          error.get("code") == "REVISION_CONFLICT", str(error.get("code")))
    check("envelope: envelope has all four keys",
          {"code", "message", "details", "requestId"} <= set(error),
          str(sorted(error)))
    check("envelope: details keep expected/actual/hint",
          {"expected", "actual", "hint"} <= set(error.get("details") or {}),
          str(sorted(error.get("details") or {})))
    METRICS["envelope_revision"] = {"status": response.status_code,
                                    "error": error}

    # CONFLICT_BUSY：同一个店铺连建两次批次
    first = client.post("/api/v1/batch", headers=headers,
                        json={"accountId": "1", "shopId": "9702",
                              "detailIds": ["4400002"]})
    second = client.post("/api/v1/batch", headers=headers,
                         json={"accountId": "1", "shopId": "9702",
                               "detailIds": ["4400003"]})
    busy = (second.json() or {}).get("error") or {}
    check("envelope: first batch created", first.status_code == 201,
          str(first.status_code))
    check("envelope: second batch -> 409", second.status_code == 409,
          str(second.status_code))
    check("envelope: CONFLICT_BUSY code reaches the client",
          busy.get("code") == "CONFLICT_BUSY", str(busy.get("code")))
    check("envelope: busy envelope has all four keys",
          {"code", "message", "details", "requestId"} <= set(busy),
          str(sorted(busy)))
    METRICS["envelope_busy"] = {"status": second.status_code, "error": busy}

    # 失败审计：409 之后 audit_logs 里必须留下 result="failed" 的一行。
    # 这条以前会整个丢掉（operator 行随 rollback 消失 → 外键失败），
    # 而「第一条失败」恰恰是最需要留痕的那条。
    from models.audit_log import AuditLog

    db.expire_all()
    rows = (db.query(AuditLog)
            .filter(AuditLog.action == "warehouse.patch")
            .filter(AuditLog.result == "failed")
            .order_by(AuditLog.id.desc()).all())
    check("envelope: failed patch leaves an audit row", bool(rows),
          f"{len(rows)} rows")
    if rows:
        check("envelope: audit row names the operator",
              bool(rows[0].operator_name), repr(rows[0].operator_name))
        check("envelope: audit row carries the error code",
              "REVISION_CONFLICT" in (rows[0].message or ""),
              (rows[0].message or "")[:80])
        METRICS["audit_failure"] = {"operatorName": rows[0].operator_name,
                                    "operatorId": rows[0].operator_id,
                                    "message": rows[0].message}


# --- 8. 其余两项（时间窗与 busy_timeout） ---------------------------------
def test_constants() -> None:
    """两个「时间窗」常量的取值，以及 SQLite 等锁上限。

    这两个数字不是随便写的，改动它们等于改动「多久算僵尸记录」：
    认领 5 分钟、发布 10 分钟；SQLite 等锁 15 秒。
    """
    from services import collect_box_service, publish_service
    from db import session as db_session_module

    check("constants: claim window is 300s",
          collect_box_service.CLAIM_RECENT_SECONDS == 300.0,
          str(collect_box_service.CLAIM_RECENT_SECONDS))
    check("constants: publish window is 600s",
          publish_service.PUBLISH_PROCESSING_SECONDS == 600.0,
          str(publish_service.PUBLISH_PROCESSING_SECONDS))
    check("constants: sqlite busy_timeout is 15s",
          db_session_module.BUSY_TIMEOUT_SECONDS == 15.0,
          str(db_session_module.BUSY_TIMEOUT_SECONDS))

    src = (ROOT / "db" / "session.py").read_text(encoding="utf-8")
    check("constants: busy_timeout set in both places (timeout + PRAGMA)",
          'connect_args={"check_same_thread": False, "timeout"' in src
          and "PRAGMA busy_timeout" in src, "")

    # 上游与本地时间窗必须成对存在：锁只管同进程，窗口管跨进程/重启
    claim_src = (ROOT / "services" / "collect_box_service.py").read_text(encoding="utf-8")
    check("constants: claim has a local claimed_at backstop",
          "claimed_at" in claim_src and "_recent_claim_blocker" in claim_src, "")
    publish_src = (ROOT / "services" / "publish_service.py").read_text(encoding="utf-8")
    check("constants: publish has an in-flight ledger backstop",
          "_in_flight_blocker" in publish_src and "processing" in publish_src, "")

    # SQLite 没有 `SELECT ... FOR UPDATE`：写一段 with_for_update() 只会得到
    # 一段永远不生效的代码，后来的人会以为有保护。这里钉住「不要写它」。
    offenders = []
    for path in sorted((ROOT / "services").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "with_for_update" in text:
            offenders.append(path.name)
    check("constants: no with_for_update anywhere in services/",
          not offenders, str(offenders))


# --- 报告 -----------------------------------------------------------------
def report() -> int:
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = len(RESULTS) - passed
    out_dir = ROOT / "_meas" / "_out"
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "total": len(RESULTS), "pass": passed, "fail": failed,
        "dataDir": str(_TMP_DIR),
        "metrics": METRICS,
        "cases": [{"name": name, "ok": ok, "detail": detail}
                  for name, ok, detail in RESULTS],
    }
    (out_dir / "probe_concurrency.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    for name, ok, detail in RESULTS:
        print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail else ""))
    print(f"\nTOTAL {len(RESULTS)}  PASS {passed}  FAIL {failed}")
    for key, value in METRICS.items():
        if isinstance(value, (int, float, str)):
            print(f"  {key}: {value}")
    return 0 if failed == 0 else 1


def main() -> int:
    from db.session import SessionLocal, init_db

    init_db()
    db = SessionLocal()
    try:
        from services import collect_box_service, publish_service, setting_service

        make_account(db)
        setting_service.set_read_only(db, False)
        setting_service.set_publish_enabled(db, True)
        db.commit()

        counter = Counter()
        collect_box_service.account_client = lambda account: FakeClient(counter)
        publish_service.account_client = lambda account: FakeClient(counter)

        test_revision_semantics(db)
        test_revision_race(db)
        test_revision_rounds(db)
        test_claim_race(db, counter)
        test_claim_recent(db, counter)
        test_claim_different(db, counter)
        test_publish_race(db, counter)
        test_publish_different(db, counter)
        test_batch_race(db)
    finally:
        db.close()

    test_lock_scope()
    test_config()
    test_shared_warning()

    # 信封用例要新开会话：上面那个已经关了，而这里要读 409 之后落下的审计行
    envelope_db = SessionLocal()
    try:
        test_error_envelope(envelope_db)
    finally:
        envelope_db.close()

    test_constants()
    return report()


if __name__ == "__main__":
    raise SystemExit(main())
