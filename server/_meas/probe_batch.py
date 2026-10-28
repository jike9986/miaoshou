"""批次编排（P0-1）本地验证：不联网、不部署、不碰真实 app.db。

验证分两层：
  · 服务层直接注入假的 loader / processor——只有这样才能稳定制造
    「第 2 件失败」「同快照重复提交」这类边界；
  · 路由层用 TestClient 走一遍真实 HTTP（同样注入假实现），确认
    鉴权、状态码、camelCase 字段与错误信封都对。

控制台输出只用 ASCII（Windows 终端会把中文吃成乱码），
完整结果同时落到 _meas/_out/probe_batch.json 供回读。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 必须在 import 应用代码之前设好：core.config 用 @lru_cache 缓存数据目录
_TMP_DIR = Path(tempfile.mkdtemp(prefix="batch-probe-"))
os.environ["AOHUANG_DATA_DIR"] = str(_TMP_DIR)
os.environ["TK_AUTOMATION_DATA_DIR"] = str(_TMP_DIR)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402

from api import API_PREFIX  # noqa: E402
from core import errors  # noqa: E402
from db.session import Base, SessionLocal, engine  # noqa: E402
from main import app  # noqa: E402
from models.account import Account  # noqa: E402
from models.batch import BatchItem  # noqa: E402
from services import account_service, batch_service, crypto  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    results.append((label, bool(ok), "" if ok else detail))


# --- 假上游 ---------------------------------------------------------------
def make_loader(snapshots: dict[str, dict] | None = None):
    """返回一个假 shop_detail：同一 detailId 永远给同一份快照。"""

    def loader(client, detail_id: str, shop_id: str):
        if snapshots and detail_id in snapshots:
            return snapshots[detail_id]
        return {"data": {"shopCollectItemInfo": {
            "title": f"title-{detail_id}",
            "imgUrls": [f"https://cdn.example.com/{detail_id}-1.jpg"],
            "notes": f"<img src='https://cdn.example.com/{detail_id}-d.jpg'>",
        }}}

    return loader


def make_processor(fail_on: set[str] | None = None,
                   error_factory=None) -> tuple[object, list[str]]:
    """返回 (processor, calls)。calls 记录真正提交过的 detailId，
    用来证明幂等闸确实没让第二次提交发生。"""
    calls: list[str] = []
    fail = fail_on or set()

    def processor(db, account, client, detail_id, shop_id, settings):
        """**刻意只收 6 个位置参数**，与 `batch_service.run_once` 的公开契约一致。

        不要给这里加 `operator_id` 或 `**kwargs`：`run_once` 会用
        `_bind_operator_id()` 探测实现是否接收 `operator_id`，收不到就不绑。
        探针保持 6 形参，才真正覆盖了「自定义实现不会被硬塞内部字段」这条契约——
        加宽签名等于把被测行为掩盖掉。
        """
        if detail_id in fail:
            raise (error_factory() if error_factory else RuntimeError(f"boom-{detail_id}"))
        calls.append(detail_id)
        return {"product": {"id": f"p-{detail_id}", "detailId": detail_id,
                            "shopId": shop_id}, "images": None}

    return processor, calls


class FakeClient:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


# --- 服务层 ---------------------------------------------------------------
def test_create(db, account) -> str:
    batch = batch_service.create(
        db, account_id=account.id, shop_id="9001",
        detail_ids=["111", "222", "111", "333"], settings={"language": "ms"})
    db.commit()
    rows = batch_service.items_of(db, batch.id)
    check("create: status pending", batch.status == "pending", batch.status)
    check("create: dedupe to 3 items", len(rows) == 3, str(len(rows)))
    check("create: order kept", [r.detail_id for r in rows] == ["111", "222", "333"],
          str([r.detail_id for r in rows]))
    check("create: detail_ids deduped",
          list(batch.detail_ids) == ["111", "222", "333"], str(batch.detail_ids))
    check("create: all items pending",
          all(r.status == "pending" for r in rows), str([r.status for r in rows]))
    check("create: current_index 0", int(batch.current_index or 0) == 0,
          str(batch.current_index))

    # 同账号同店铺已有未完成批次 → 409 CONFLICT_BUSY
    try:
        batch_service.create(db, account_id=account.id, shop_id="9001",
                             detail_ids=["444"])
    except errors.AppError as exc:
        check("create: duplicate active batch -> 409",
              exc.status == 409 and exc.code == errors.CONFLICT_BUSY,
              f"{exc.status}/{exc.code}")
        check("create: busy message", "已有未完成的批次" in exc.message, exc.message)
    else:
        check("create: duplicate active batch -> 409", False, "no error")

    # 唯一约束是幂等闸的物理保证：同一批次里同一件插不进去
    try:
        with db.begin_nested():
            db.add(BatchItem(batch_id=batch.id, detail_id="111"))
            db.flush()
    except IntegrityError:
        check("create: unique(batch_id, detail_id) enforced", True)
    else:
        check("create: unique(batch_id, detail_id) enforced", False, "duplicate accepted")

    # 参数校验
    for label, kwargs in (
        ("create: empty detailIds rejected", {"detail_ids": []}),
        ("create: non-digit detailId rejected", {"detail_ids": ["abc"]}),
        ("create: empty shopId rejected", {"detail_ids": ["1"], "shop_id": ""}),
        ("create: too many items rejected",
         {"detail_ids": [str(1000 + i) for i in range(batch_service.MAX_ITEMS + 1)]}),
    ):
        payload = {"account_id": account.id, "shop_id": "9001", **kwargs}
        try:
            batch_service.create(db, **payload)
        except errors.AppError as exc:
            check(label, exc.status == 400 and exc.code == errors.VALIDATION_ERROR,
                  f"{exc.status}/{exc.code}")
        else:
            check(label, False, "accepted")

    # 不存在的账号必须是 404 而不是 commit 时炸 IntegrityError
    try:
        batch_service.create(db, account_id=987654, shop_id="9001",
                             detail_ids=["1"])
    except errors.AppError as exc:
        check("create: unknown account -> 404", exc.status == 404, f"{exc.status}")
    else:
        check("create: unknown account -> 404", False, "accepted")
    db.rollback()
    return batch.id


def test_run_to_done(db, batch_id) -> None:
    loader = make_loader()
    processor, calls = make_processor()
    outcomes = []
    for _ in range(4):
        outcomes.append(batch_service.run_once(db, batch_id, loader=loader,
                                               processor=processor))
        db.commit()
    batch = batch_service.get(db, batch_id)
    rows = batch_service.items_of(db, batch_id)
    check("run: 3 steps advanced", sum(1 for o in outcomes if o["advanced"]) == 3,
          str([o["advanced"] for o in outcomes]))
    check("run: batch done", batch.status == "done", batch.status)
    check("run: 3 items ok", [r.status for r in rows] == ["ok"] * 3,
          str([r.status for r in rows]))
    check("run: current_index at end", int(batch.current_index) == 3,
          str(batch.current_index))
    check("run: finished_at set", batch.finished_at is not None, str(batch.finished_at))
    check("run: processor called once per item", calls == ["111", "222", "333"],
          str(calls))
    check("run: results snapshot has 3 rows",
          len([r for r in (batch.results or []) if r.get("status") == "ok"]) == 3,
          str(batch.results))
    check("run: 4th step does not advance", outcomes[3]["advanced"] is False,
          str(outcomes[3]["advanced"]))
    check("run: 4th step says done", "已完成" in outcomes[3]["message"],
          outcomes[3]["message"])
    check("run: remaining reaches 0", batch_service.serialize(batch)["remaining"] == 0,
          str(batch_service.serialize(batch)["remaining"]))

    # done 之后只允许删除：step / control / retry 全部拒绝
    again = batch_service.run_once(db, batch_id, loader=loader, processor=processor)
    check("done: step refuses", again["advanced"] is False, str(again))
    for action in ("pause", "resume", "stop"):
        try:
            batch_service.control(db, batch_id, action)
        except errors.AppError as exc:
            check(f"done: control {action} refused", exc.status == 400, f"{exc.status}")
        else:
            check(f"done: control {action} refused", False, "accepted")
    try:
        batch_service.retry_failed(db, batch_id)
    except errors.AppError as exc:
        check("done: retry-failed refused", exc.status == 400, f"{exc.status}")
    else:
        check("done: retry-failed refused", False, "accepted")


def test_failure_and_retry(db, account) -> None:
    batch = batch_service.create(db, account_id=account.id, shop_id="9002",
                                 detail_ids=["501", "502", "503"])
    db.commit()
    loader = make_loader()
    processor, calls = make_processor(fail_on={"502"})

    first = batch_service.run_once(db, batch.id, loader=loader, processor=processor)
    db.commit()
    check("fail: first item ok", first["item"]["status"] == "ok", str(first["item"]))
    check("fail: index advanced to 1",
          int(batch_service.get(db, batch.id).current_index) == 1, "")

    second = batch_service.run_once(db, batch.id, loader=loader, processor=processor)
    db.commit()
    batch = batch_service.get(db, batch.id)
    rows = batch_service.items_of(db, batch.id)
    check("fail: second item failed", second["item"]["status"] == "failed",
          str(second["item"]))
    check("fail: not advanced", second["advanced"] is False, str(second["advanced"]))
    check("fail: current_index stays at 1", int(batch.current_index) == 1,
          str(batch.current_index))
    check("fail: batch status failed", batch.status == "failed", batch.status)
    check("fail: attempt counted", int(rows[1].attempt) == 1, str(rows[1].attempt))
    check("fail: message recorded", "boom-502" in rows[1].message, rows[1].message)
    check("fail: third item untouched", rows[2].status == "pending", rows[2].status)
    check("fail: errors logged", len(batch.errors or []) == 1, str(batch.errors))
    check("fail: unknown flagged", (batch.errors or [{}])[0].get("unknownResult") is True,
          str(batch.errors))
    check("fail: remaining counts unfinished", batch_service.serialize(batch)["remaining"] == 2,
          str(batch_service.serialize(batch)["remaining"]))

    # failed 状态下 step 不推进（要求先 retry-failed）
    blocked = batch_service.run_once(db, batch.id, loader=loader, processor=processor)
    check("fail: step blocked while failed", blocked["advanced"] is False,
          str(blocked["advanced"]))
    check("fail: blocked message mentions retry", "retry-failed" in blocked["message"],
          blocked["message"])

    # 结果未知的件默认不许一键重跑
    try:
        batch_service.retry_failed(db, batch.id)
    except errors.AppError as exc:
        check("fail: retry blocked for unknown result",
              exc.status == 409 and exc.code == errors.RETRY_UNSAFE,
              f"{exc.status}/{exc.code}")
    else:
        check("fail: retry blocked for unknown result", False, "accepted")

    # 人工核对后显式放行
    retried = batch_service.retry_failed(db, batch.id, allow_unknown=True)
    db.commit()
    rows = batch_service.items_of(db, batch.id)
    batch = batch_service.get(db, batch.id)
    check("retry: reported count", retried["retried"] == 1, str(retried["retried"]))
    check("retry: item back to pending", rows[1].status == "pending", rows[1].status)
    check("retry: attempt reset after manual check", int(rows[1].attempt) == 0,
          str(rows[1].attempt))
    check("retry: message cleared", rows[1].message == "", rows[1].message)
    check("retry: batch running again", batch.status == "running", batch.status)
    check("retry: current_index back to 1", int(batch.current_index) == 1,
          str(batch.current_index))

    # 重试成功后跑完整批
    processor2, calls2 = make_processor()
    for _ in range(3):
        batch_service.run_once(db, batch.id, loader=loader, processor=processor2)
        db.commit()
    batch = batch_service.get(db, batch.id)
    rows = batch_service.items_of(db, batch.id)
    check("retry: all ok after retry", [r.status for r in rows] == ["ok"] * 3,
          str([r.status for r in rows]))
    check("retry: batch done", batch.status == "done", batch.status)
    check("retry: only remaining items resubmitted", calls2 == ["502", "503"],
          str(calls2))

    # 没有失败件时 retry-failed 明确报错
    try:
        batch_service.retry_failed(db, batch.id, allow_unknown=True)
    except errors.AppError as exc:
        check("retry: no failures -> 400", exc.status == 400, f"{exc.status}")
    else:
        check("retry: no failures -> 400", False, "accepted")


def test_pause_resume(db, account) -> None:
    batch = batch_service.create(db, account_id=account.id, shop_id="9003",
                                 detail_ids=["601", "602"])
    db.commit()
    loader = make_loader()
    processor, calls = make_processor()

    paused = batch_service.control(db, batch.id, "pause")
    db.commit()
    check("pause: status paused", paused["batch"]["status"] == "paused",
          str(paused["batch"]["status"]))

    blocked = batch_service.run_once(db, batch.id, loader=loader, processor=processor)
    db.commit()
    check("pause: step does not advance", blocked["advanced"] is False,
          str(blocked["advanced"]))
    check("pause: no upstream call", calls == [], str(calls))
    check("pause: index unchanged",
          int(batch_service.get(db, batch.id).current_index) == 0, "")
    check("pause: message says paused", "暂停" in blocked["message"], blocked["message"])

    # 暂停态重复 pause 被拒
    try:
        batch_service.control(db, batch.id, "pause")
    except errors.AppError as exc:
        check("pause: pause twice refused", exc.status == 400, f"{exc.status}")
    else:
        check("pause: pause twice refused", False, "accepted")

    resumed = batch_service.control(db, batch.id, "resume")
    db.commit()
    check("resume: status running", resumed["batch"]["status"] == "running",
          str(resumed["batch"]["status"]))
    after = batch_service.run_once(db, batch.id, loader=loader, processor=processor)
    db.commit()
    check("resume: step advances", after["advanced"] is True, str(after["advanced"]))
    check("resume: one item submitted", calls == ["601"], str(calls))
    check("resume: index now 1",
          int(batch_service.get(db, batch.id).current_index) == 1, "")

    # resume 非暂停态被拒
    try:
        batch_service.control(db, batch.id, "resume")
    except errors.AppError as exc:
        check("resume: resume twice refused", exc.status == 400, f"{exc.status}")
    else:
        check("resume: resume twice refused", False, "accepted")

    # 非法动作
    try:
        batch_service.control(db, batch.id, "explode")
    except errors.AppError as exc:
        check("control: bad action -> 400", exc.status == 400, f"{exc.status}")
    else:
        check("control: bad action -> 400", False, "accepted")


def test_stop(db, account) -> None:
    batch = batch_service.create(db, account_id=account.id, shop_id="9004",
                                 detail_ids=["701", "702", "703"])
    db.commit()
    loader = make_loader()
    processor, calls = make_processor()
    batch_service.run_once(db, batch.id, loader=loader, processor=processor)
    db.commit()

    stopped = batch_service.control(db, batch.id, "stop")
    db.commit()
    rows = batch_service.items_of(db, batch.id)
    batch = batch_service.get(db, batch.id)
    check("stop: status stopped", batch.status == "stopped", batch.status)
    check("stop: unrun marked skipped", [r.status for r in rows] == ["ok", "skipped", "skipped"],
          str([r.status for r in rows]))
    check("stop: message counts skipped", "2 件标为跳过" in stopped["message"],
          stopped["message"])
    check("stop: finished_at set", batch.finished_at is not None, "")
    check("stop: no extra submission", calls == ["701"], str(calls))

    blocked = batch_service.run_once(db, batch.id, loader=loader, processor=processor)
    check("stop: step refuses", blocked["advanced"] is False, str(blocked["advanced"]))
    check("stop: message says stopped", "停止" in blocked["message"], blocked["message"])

    # stopped 可删
    deleted = batch_service.delete(db, batch.id)
    db.commit()
    check("stop: stopped batch deletable", deleted["deleted"] is True, str(deleted))
    try:
        batch_service.get(db, batch.id)
    except errors.AppError as exc:
        check("stop: deleted batch gone", exc.status == 404, f"{exc.status}")
    else:
        check("stop: deleted batch gone", False, "still there")


def test_idempotency(db, account) -> None:
    """幂等闸：同一店铺、同一 detailId、同一份快照，第二次不再提交。"""
    loader = make_loader()
    processor, calls = make_processor()

    first = batch_service.create(db, account_id=account.id, shop_id="9005",
                                 detail_ids=["801"])
    db.commit()
    batch_service.run_once(db, first.id, loader=loader, processor=processor)
    db.commit()
    check("idem: first submission happened", calls == ["801"], str(calls))
    check("idem: first batch done",
          batch_service.get(db, first.id).status == "done", "")

    second = batch_service.create(db, account_id=account.id, shop_id="9005",
                                 detail_ids=["801"])
    db.commit()
    outcome = batch_service.run_once(db, second.id, loader=loader, processor=processor)
    db.commit()
    check("idem: second submission skipped", calls == ["801"], str(calls))
    check("idem: item marked ok without resubmit", outcome["item"]["status"] == "ok",
          str(outcome["item"]))
    check("idem: message explains skip", "跳过" in outcome["message"], outcome["message"])
    check("idem: second batch done",
          batch_service.get(db, second.id).status == "done", "")

    # 快照变了 → 允许重新提交（换了图/改了标题本来就该重跑）
    changed = make_loader({"802": {"data": {"shopCollectItemInfo": {
        "title": "changed", "imgUrls": ["https://cdn.example.com/802-new.jpg"]}}}})
    third = batch_service.create(db, account_id=account.id, shop_id="9005",
                                 detail_ids=["802"])
    db.commit()
    batch_service.run_once(db, third.id, loader=loader, processor=processor)
    db.commit()
    check("idem: first hash submitted", calls == ["801", "802"], str(calls))
    fourth = batch_service.create(db, account_id=account.id, shop_id="9005",
                                  detail_ids=["802"])
    db.commit()
    batch_service.run_once(db, fourth.id, loader=changed, processor=processor)
    db.commit()
    check("idem: changed snapshot resubmitted", calls == ["801", "802", "802"],
          str(calls))

    # 快照哈希只看 data 块：外层信封变化不影响判定
    same_a = {"data": {"x": 1}, "requestId": "a", "ts": 1}
    same_b = {"data": {"x": 1}, "requestId": "b", "ts": 2}
    check("idem: hash ignores envelope",
          batch_service.snapshot_hash(same_a) == batch_service.snapshot_hash(same_b), "")
    check("idem: hash detects data change",
          batch_service.snapshot_hash({"data": {"x": 2}})
          != batch_service.snapshot_hash(same_a), "")


def test_crash_gate(db, account) -> None:
    """进程崩在提交中间：payload_hash 已写、状态还停在进行中 → 拒绝自动重跑。"""
    loader = make_loader()
    processor, calls = make_processor()
    batch = batch_service.create(db, account_id=account.id, shop_id="9006",
                                 detail_ids=["901"])
    db.commit()
    item = batch_service.items_of(db, batch.id)[0]
    digest = batch_service.snapshot_hash(loader(None, "901", "9006"))
    item.status = "running"
    item.payload_hash = digest
    db.commit()

    try:
        batch_service.run_once(db, batch.id, loader=loader, processor=processor)
    except errors.AppError as exc:
        check("crash: running item refuses rerun",
              exc.status == 409 and exc.code == errors.RETRY_UNSAFE,
              f"{exc.status}/{exc.code}")
        check("crash: message asks manual check", "核对" in exc.message, exc.message)
    else:
        check("crash: running item refuses rerun", False, "accepted")
    check("crash: nothing resubmitted", calls == [], str(calls))


def test_single_item_failure_seals(db, account) -> None:
    """最后一件失败也要盖 finishedAt：否则前端永远显示「进行中」。"""
    batch = batch_service.create(db, account_id=account.id, shop_id="9007",
                                 detail_ids=["951"])
    db.commit()
    loader = make_loader()
    processor, calls = make_processor(fail_on={"951"})
    outcome = batch_service.run_once(db, batch.id, loader=loader,
                                     processor=processor)
    db.commit()
    batch = batch_service.get(db, batch.id)
    check("seal: single item failed", outcome["item"]["status"] == "failed",
          str(outcome["item"]))
    check("seal: batch failed", batch.status == "failed", batch.status)
    check("seal: finished_at set on last failure", batch.finished_at is not None,
          str(batch.finished_at))
    check("seal: nothing submitted", calls == [], str(calls))

    retried = batch_service.retry_failed(db, batch.id, allow_unknown=True)
    db.commit()
    batch = batch_service.get(db, batch.id)
    check("seal: retry clears finished_at", batch.finished_at is None,
          str(batch.finished_at))
    check("seal: retry reopens batch", batch.status == "running", batch.status)
    check("seal: retry count", retried["retried"] == 1, str(retried["retried"]))

    ok_processor, calls2 = make_processor()
    done = batch_service.run_once(db, batch.id, loader=loader, processor=ok_processor)
    db.commit()
    batch = batch_service.get(db, batch.id)
    check("seal: retried item succeeds", done["item"]["status"] == "ok",
          str(done["item"]))
    check("seal: batch done after retry", batch.status == "done", batch.status)
    check("seal: resubmitted once", calls2 == ["951"], str(calls2))


def test_list_and_serialize(db, account) -> None:
    listing = batch_service.list_batches(db, account_id=account.id, limit=100)
    check("list: has total", "total" in listing, str(listing.keys()))
    check("list: camelCase keys",
          all("currentIndex" in row and "detailIds" in row for row in listing["items"]),
          str(listing["items"][:1]))
    check("list: total >= items", listing["total"] >= len(listing["items"]), "")
    try:
        batch_service.list_batches(db, account_id="not-a-number")
    except errors.AppError as exc:
        check("list: bad accountId -> 400", exc.status == 400, f"{exc.status}")
    else:
        check("list: bad accountId -> 400", False, "accepted")
    try:
        batch_service.get(db, "")
    except errors.AppError as exc:
        check("get: empty id -> 400", exc.status == 400, f"{exc.status}")
    else:
        check("get: empty id -> 400", False, "accepted")
    try:
        batch_service.get(db, "bat_missing")
    except errors.AppError as exc:
        check("get: unknown id -> 404", exc.status == 404, f"{exc.status}")
    else:
        check("get: unknown id -> 404", False, "accepted")


def test_http() -> None:
    from core.config import get_settings

    token = get_settings().local_secret()
    headers = {"Authorization": f"Bearer {token}"}
    base = f"{API_PREFIX}/batch"

    # 注入假上游：路由层的 step 走的是同一套 run_once
    batch_service._default_loader = make_loader()
    batch_service._default_processor = make_processor()[0]
    account_service.account_client = lambda account: FakeClient()

    with TestClient(app) as client:
        created = client.post(base, json={
            "accountId": "1", "shopId": "9500",
            "detailIds": ["1111", "1111", "2222"], "settings": {"language": "ms"},
        }, headers=headers)
        check("http: create 201", created.status_code == 201, str(created.status_code))
        body = created.json()
        check("http: create returns batch", "batch" in body and "items" in body,
              str(body)[:200])
        batch_id = body.get("batch", {}).get("id", "")
        check("http: dedupe via http", len(body.get("items") or []) == 2,
              str(len(body.get("items") or [])))
        check("http: batch camelCase", "currentIndex" in body.get("batch", {}),
              str(body.get("batch", {}).keys()))

        dup = client.post(base, json={"accountId": "1", "shopId": "9500",
                                      "detailIds": ["3333"]}, headers=headers)
        check("http: duplicate active -> 409", dup.status_code == 409,
              str(dup.status_code))
        check("http: 409 error envelope",
              dup.json().get("error", {}).get("code") == errors.CONFLICT_BUSY,
              str(dup.json())[:200])

        listing = client.get(base, headers=headers)
        check("http: list 200", listing.status_code == 200, str(listing.status_code))
        check("http: list has total", "total" in listing.json(), str(listing.json().keys()))

        detail = client.get(f"{base}/{batch_id}", headers=headers)
        check("http: detail 200", detail.status_code == 200, str(detail.status_code))
        check("http: detail items", len(detail.json().get("items") or []) == 2, "")

        missing = client.get(f"{base}/bat_nope", headers=headers)
        check("http: detail 404", missing.status_code == 404, str(missing.status_code))

        # running 批次不许删
        blocked_delete = client.request("DELETE", f"{base}/{batch_id}", headers=headers)
        check("http: delete pending refused", blocked_delete.status_code == 400,
              str(blocked_delete.status_code))

        step = client.post(f"{base}/{batch_id}/step", headers=headers)
        check("http: step 200", step.status_code == 200, str(step.status_code))
        check("http: step advanced", step.json().get("advanced") is True,
              str(step.json())[:200])
        check("http: step item ok",
              (step.json().get("item") or {}).get("status") == "ok",
              str(step.json().get("item")))

        paused = client.post(f"{base}/{batch_id}/control", json={"action": "pause"},
                             headers=headers)
        check("http: pause 200", paused.status_code == 200, str(paused.status_code))
        check("http: pause status",
              paused.json().get("batch", {}).get("status") == "paused",
              str(paused.json())[:200])
        blocked = client.post(f"{base}/{batch_id}/step", headers=headers)
        check("http: paused step does not advance",
              blocked.json().get("advanced") is False, str(blocked.json())[:200])

        bad_action = client.post(f"{base}/{batch_id}/control", json={"action": "nope"},
                                 headers=headers)
        check("http: bad action 400", bad_action.status_code == 400,
              str(bad_action.status_code))

        resumed = client.post(f"{base}/{batch_id}/control", json={"action": "resume"},
                              headers=headers)
        check("http: resume 200", resumed.status_code == 200, str(resumed.status_code))

        no_fail = client.post(f"{base}/{batch_id}/retry-failed", json={}, headers=headers)
        check("http: retry-failed with no failures -> 400",
              no_fail.status_code == 400, str(no_fail.status_code))

        client.post(f"{base}/{batch_id}/step", headers=headers)
        final = client.get(f"{base}/{batch_id}", headers=headers).json()
        check("http: batch done after steps",
              final.get("batch", {}).get("status") == "done",
              str(final.get("batch", {}).get("status")))
        check("http: all items ok",
              [i.get("status") for i in final.get("items") or []] == ["ok", "ok"],
              str([i.get("status") for i in final.get("items") or []]))

        gone = client.request("DELETE", f"{base}/{batch_id}", headers=headers)
        check("http: delete done 200", gone.status_code == 200, str(gone.status_code))
        check("http: delete reports items", gone.json().get("items") == 2,
              str(gone.json()))

        # 鉴权：无 token 一律 401
        anon = client.post(base, json={"accountId": "1", "shopId": "9501",
                                       "detailIds": ["1"]})
        check("http: no token -> 401", anon.status_code == 401, str(anon.status_code))


def main() -> int:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        account = Account(name="probe", app_key="probe-key",
                          app_secret_enc=crypto.encrypt("probe-secret"),
                          is_default=True, site="MY", status="active")
        db.add(account)
        db.commit()
        db.refresh(account)

        batch_id = test_create(db, account)
        test_run_to_done(db, batch_id)
        test_failure_and_retry(db, account)
        test_pause_resume(db, account)
        test_stop(db, account)
        test_idempotency(db, account)
        test_crash_gate(db, account)
        test_single_item_failure_seals(db, account)
        test_list_and_serialize(db, account)
    finally:
        db.close()

    test_http()

    failed = [row for row in results if not row[1]]
    for label, ok, detail in results:
        print(("PASS " if ok else "FAIL ") + label
              + (("  [" + detail + "]") if detail and not ok else ""))
    print("TOTAL %d  PASS %d  FAIL %d"
          % (len(results), len(results) - len(failed), len(failed)))

    out_dir = ROOT / "_meas" / "_out"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "probe_batch.json").write_text(json.dumps(
        {"total": len(results), "failed": len(failed),
         "rows": [{"label": l, "ok": o, "detail": d} for l, o, d in results]},
        ensure_ascii=False, indent=2), encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
