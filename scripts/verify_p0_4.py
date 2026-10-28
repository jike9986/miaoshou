"""验证 P0-4：run_due 不再因 published 是 list 抛异常（旧实现会导致同批重发 3 次）。"""
import sys

sys.path.insert(0, ".")

from db.session import SessionLocal  # noqa: E402
from services import schedule_service, publish_service  # noqa: E402

db = SessionLocal()

# 1) 旧 bug 的根因：int(list) 会 TypeError
try:
    int(["a"])
    print("int(list) 竟然成功？逻辑异常")
except TypeError as exc:
    print("根因复现 TypeError:", exc)

# 2) 新实现 _count 对 list 的处理
print("_count([1,2,3]) =", schedule_service._count([1, 2, 3]))
print("_count('3')     =", schedule_service._count("3"))
print("_count(None)    =", schedule_service._count(None))

# 3) run_due 在 publish 返回 list 形状时不应抛 TypeError
calls = []

def fake_publish(db_, payload, **kwargs):
    calls.append(payload)
    return {
        "published": ["101", "102"],
        "claimed": [],
        "failures": [{"detailId": "103", "error": "mock 失败"}],
        "mode": "direct",
        "batchId": "mock-batch",
        "repriced": [],
        "warning": None,
    }

original = publish_service.publish
publish_service.publish = fake_publish
try:
    result = schedule_service.run_due(db)
    print("run_due 返回类型:", type(result).__name__)
    if isinstance(result, dict):
        print("摘要字段:", {k: result.get(k) for k in
                          ("requested", "selected", "published", "failed", "status", "reason")
                          if k in result})
    elif isinstance(result, list):
        print("本轮执行条数:", len(result))
        for item in result:
            print("  ->", {k: item.get(k) for k in
                           ("requested", "selected", "published", "failed", "status")
                           if k in item})
    print("publish 调用次数:", len(calls), "（0 表示当前没有到期任务，属正常）")
except TypeError as exc:
    print("!! 仍然 TypeError:", exc)
finally:
    publish_service.publish = original
    db.rollback()
    db.close()
