"""进程内「按键互斥」锁：多人共用时挡住「同一件事被同时做两次」。

解决的是三类真实事故，它们的共同点是**上游动作不可撤销**：

  · 两个人同时点认领同一件商品 —— 妙手那边多出一条采集箱记录，本工具没有
    接口能把它退回来；
  · 两个人同时发布同一件商品 —— 平台收到两次提交，商品被建两遍；
  · 两个人同时建同一个店铺的批次 —— 两条 pending 批次覆盖同一批 detailId，
    各自从第 0 件开始跑，重量被加两次（`prepare_weight` 不幂等）。

三个刻意的选择：

  · **不用数据库行锁**。SQLite 没有 `SELECT ... FOR UPDATE`，写本来就是整库
    串行；写一个 `with_for_update()` 只会得到一段永远不生效的代码，比没有更糟
    —— 后来的人会以为有保护。

  · **不是一把全局锁**。认领第 1 件和发布第 2 件毫无关系，全局锁会让两个人
    排队等一个几秒的上游调用，表现就是「软件卡住了」。

  · **不用固定数量的哈希桶**。桶会带来假冲突：两个无关商品撞进同一个桶时
    互相阻塞，而且这种冲突是随机的、现场复现不了的。这里用「每个键一把锁」
    的字典，假冲突为 0；内存占用与出现过的键数同阶（几百把锁），可以接受。

锁只在本进程内有效（后端是单进程 uvicorn，见 run_server.py）。跨进程/重启后
靠业务侧的「时间窗」兜底：认领看 `Product.claimed_at` 5 分钟，发布看
`PublishLog.requested_at` 10 分钟。两者都不完美，但都比「完全不设防」强。

── 锁的寿命必须活到「提交」，不是活到「函数返回」 ──────────────────────────
这是实测出来的第二个洞，比锁本身更容易漏。原来的写法是：

    with <按键加锁>(...):          # 出 with 就放锁
        ... 改库、打上游 ...
    db.commit()                    # 路由里提交，锁已经没了

于是 A 认领完、还没提交时，B 拿得到锁、查本地库也看不到 A 的 claimed
（未提交的行别人读不到），于是**再打一次上游**。用 sleep 把这个窗口放大后
必现：`claim_to_shop` 被调用 2 次，两条记录都写成功。批次同理，会插出两条
pending 批次覆盖同一批 detailId。

所以这里只有**会话级**锁：挂到会话上，事务一结束（commit / rollback / close）
才放。监听的是 `after_transaction_end` 且只在最外层事务结束时动作 —— 嵌套
savepoint 回滚不该放掉外层还需要的锁（`begin_nested()` 在业务里是有的）。

函数级的「进函数加、出函数放」接口被**故意删掉**了：它看起来更简单，但在
「服务函数改库、路由里 commit」这个代码结构里它保护不了任何东西，留着只会
让下一个人照着它再写一遍同样的漏。
"""
from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterable, Iterator

from core import errors

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()

# 挂在 Session.info 上的两个键：已持有的锁、监听器是否已装
_HELD_ATTR = "core.locks.held"
_HOOKED_ATTR = "core.locks.hooked"


def lock_of(key: str) -> threading.Lock:
    """取（必要时创建）某个业务键对应的锁。

    建锁本身要加一把很短的守卫锁：两个线程同时第一次碰到同一个键时，
    不加守卫会各自造一把锁，等于两个人都以为自己是唯一持有者。
    """
    text = str(key)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(text)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[text] = lock
        return lock


# --- 会话级锁（活到事务结束） ----------------------------------------------
def _release_session_locks(db) -> None:  # noqa: ANN001
    held = db.info.pop(_HELD_ATTR, None) or {}
    for lock in reversed(list(held.values())):
        lock.release()


def _install_hook(db) -> None:  # noqa: ANN001
    """给会话装一次「事务结束就放锁」的监听。

    为什么是 `after_transaction_end` 而不是 `after_commit` / `after_rollback`：
    后两个只在正常收尾时触发，`Session.close()` 不触发 —— 一个没提交就返回的
    请求会把锁永久泄漏，那个键从此再也没人抢得到。实测确认（commit / rollback
    会触发，close 不会）。

    为什么还要判 `transaction.parent is None` 和 `in_transaction()`：
    `begin_nested()` 产生的 savepoint 结束时 `parent` 非空，那不是事务真的结束；
    只在最外层且会话已经不在事务里时才放锁，避免「回滚了一个 savepoint 就把
    外层还要用的锁放了」。
    """
    if db.info.get(_HOOKED_ATTR):
        return
    from sqlalchemy import event

    def _on_end(session, transaction) -> None:  # noqa: ANN001
        if transaction.parent is None and not session.in_transaction():
            _release_session_locks(session)

    event.listen(db, "after_transaction_end", _on_end)
    db.info[_HOOKED_ATTR] = True


def _hold_for_session(db, keys: Iterable[str], *, blocking: bool,  # noqa: ANN001
                      busy_message: str,
                      busy_details: dict | None) -> None:
    """把锁记到会话上，等事务结束时才放。

    同一个键在同一个会话里已经持有就不再抢（`threading.Lock` 不可重入，
    再抢一次就是自己把自己卡死）；这种情况直接复用。
    """
    held: dict[str, threading.Lock] = db.info.setdefault(_HELD_ATTR, {})
    mine: list[threading.Lock] = []
    for key in sorted({str(key) for key in keys if str(key)}):
        if key in held:
            continue
        lock = lock_of(key)
        if not lock.acquire(blocking=blocking):
            # 只放回**本次调用**刚抢到的，别碰本会话之前就持有的那些：
            # 批量认领一次处理 50 件，第 2 件抢不到不该把第 1 件的锁也丢掉
            # （第 1 件的改动还在这同一个未提交的事务里）。
            for got in reversed(mine):
                got.release()
            details = dict(busy_details or {})
            details.setdefault("busyKey", key)
            raise errors.busy(busy_message, **details)
        held[key] = lock
        mine.append(lock)
    _install_hook(db)
    # 装完监听后必须确保会话真的开了事务，否则「什么都不写」的请求不会触发
    # after_transaction_end，锁会留到下一次事务结束。
    if not db.in_transaction():
        db.begin()


@contextlib.contextmanager
def session_lock(db, keys: Iterable[str], *, blocking: bool = True,  # noqa: ANN001
                 busy_message: str = "这件事正在被处理，请稍后重试",
                 busy_details: dict | None = None) -> Iterator[None]:
    """按业务键加锁，**锁活到事务结束**（推荐用法）。

    `db` 上的事务一结束（commit / rollback / close）就自动放锁，不需要
    `finally`。这正是「服务函数改库、路由里 commit」这个代码结构需要的语义：
    函数返回时就放锁的话，改动还没提交，第二个人读不到、照样能做同一件事
    （详见模块头部的实测说明）。
    """
    _hold_for_session(db, keys, blocking=blocking, busy_message=busy_message,
                      busy_details=busy_details)
    yield
    # 这里**不释放**：故意的。锁的释放交给事务结束事件，
    # 否则又回到「函数返回就放锁」的老问题。


def hold_for_session(db, keys: Iterable[str], *, blocking: bool = True,  # noqa: ANN001
                     busy_message: str = "这件事正在被处理，请稍后重试",
                     busy_details: dict | None = None) -> None:
    """`session_lock` 的函数式版本：只加锁、不配对释放。

    给那些函数体已经有一大段 `try/except/finally` 的调用方（发布就是），
    用 with 得把几百行重新缩进一次，review 时看不出真正改了什么。
    """
    _hold_for_session(db, keys, blocking=blocking, busy_message=busy_message,
                      busy_details=busy_details)
