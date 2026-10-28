"""闸门自测：标题 review 词 + 主图 OCR（老版两道硬阻断的移植）。

运行：python _meas/probe_gate.py（从 server 目录）
结果写 _meas/probe_gate.json —— 中文在 PowerShell 控制台会乱码，看文件。

全程只用临时数据目录，绝不碰真实 app.db。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.getcwd())
_TMP = tempfile.mkdtemp(prefix="probe_gate_")
os.environ["AOHUANG_DATA_DIR"] = _TMP

from db.session import Base, SessionLocal, engine  # noqa: E402
from services import gate_service, image_review_service, pre_save_check  # noqa: E402

Base.metadata.create_all(engine)
db = SessionLocal()

OK = 0
FAIL = 0
CASES = []


def record(name, passed, detail):
    global OK, FAIL
    if passed:
        OK += 1
    else:
        FAIL += 1
    CASES.append({"name": name, "result": "PASS" if passed else "FAIL", "detail": detail})
    print(("  PASS " if passed else "  FAIL ") + name)


# --- 词库：造一个 review 词和一个 remove 词 ---------------------------------
from models.violation_word import ViolationWord  # noqa: E402
from core.utils import now_ts  # noqa: E402

REVIEW_WORD = "限量版"
REMOVE_WORD = "包邮"
for word, action in ((REVIEW_WORD, "review"), (REMOVE_WORD, "remove")):
    db.add(ViolationWord(word=word, normalized=word.casefold(), action=action,
                         source="probe", enabled=True, created_at=now_ts()))
db.commit()

CLEAN_TITLE = "Baju Sukan Lelaki Kasual Musim Panas Berkualiti Tinggi"
DIRTY_TITLE = CLEAN_TITLE + " " + REVIEW_WORD
MAIN_IMAGES = [f"https://cbu01.alicdn.com/img/{i}.jpg" for i in range(1, 4)]


def item(title=CLEAN_TITLE, images=None):
    return {"title": title, "imgUrls": list(MAIN_IMAGES if images is None else images)}


# --- 缺口 1：标题 review 词 -------------------------------------------------
print("缺口 1：标题 review 词闸门")

result = gate_service.title_review_gate(item(), db=db)
record("标题无 review 词 → blocked=False", result["blocked"] is False, result)

result = gate_service.title_review_gate(item(DIRTY_TITLE), db=db)
record("标题有 review 词 → blocked=True",
       result["blocked"] is True and REVIEW_WORD in result["reviewWords"], result)
record("message 照抄老版措辞",
       result["message"] == "标题清理后仍需要人工复核，已停止保存", result["message"])

result = gate_service.title_review_gate(item(DIRTY_TITLE), db=db, allow_review=True)
record("allow_review=True → blocked=False 但 reviewWords 仍有内容",
       result["blocked"] is False and REVIEW_WORD in result["reviewWords"], result)

# remove 词不该触发复核闸（它是直接删，不是等人看）
result = gate_service.title_review_gate(item(CLEAN_TITLE + " " + REMOVE_WORD), db=db)
record("只有 remove 词 → blocked=False 且标题已清理",
       result["blocked"] is False and REMOVE_WORD not in result["cleanedTitle"], result)

# db=None 也要能跑（纯函数场景）
result = gate_service.title_review_gate(item(DIRTY_TITLE))
record("db=None 时自开会话仍能判定", result["blocked"] is True, result)

# --- 缺口 2：主图 OCR ------------------------------------------------------
print("缺口 2：主图 OCR 闸门")

_original_analyze = image_review_service.analyze


def fake_analyze(rows_factory):
    def _fake(images, **_kwargs):
        return rows_factory(list(images))
    return _fake


def all_ok(images):
    return [{"index": i, "url": url, "action": "keep", "reason": "未识别到中文",
             "hasChinese": False, "chineseText": "", "text": "", "error": False}
            for i, url in enumerate(images, 1)]


def one_review(images):
    rows = all_ok(images)
    rows[0].update({"action": "review", "reason": "图上没识别到文字"})
    return rows


def one_error(images):
    rows = all_ok(images)
    rows[1].update({"action": "review", "error": True, "reason": "本地 OCR 未完成"})
    return rows


def short_rows(images):
    return all_ok(images)[:2]


def one_remove(images):
    rows = all_ok(images)
    rows[1].update({"action": "remove", "reason": "疑似厂家宣传：厂家直销"})
    return rows


def all_remove(images):
    rows = all_ok(images)
    for row in rows:
        row.update({"action": "remove", "reason": "疑似厂家宣传：厂家直销"})
    return rows


def run_image_case(name, factory, *, allow_review=False, images=None,
                   want_blocked=None, want_removed=None, message_has=""):
    image_review_service.analyze = fake_analyze(factory)
    try:
        result = gate_service.main_image_gate(
            item(images=images), allow_review=allow_review)
    finally:
        image_review_service.analyze = _original_analyze
    passed = result["blocked"] is want_blocked
    if want_removed is not None:
        passed = passed and result["removeIndexes"] == want_removed
    if message_has:
        passed = passed and message_has in result["message"]
    record(name, passed, result)
    return result


run_image_case("全部 ok → blocked=False", all_ok, want_blocked=False)

run_image_case("一行 action=review → blocked=True", one_review, want_blocked=True,
               message_has="主图分类需要人工复核，已停止保存")

run_image_case("一行 error=xxx → blocked=True", one_error, want_blocked=True)

run_image_case("返回行数少于图片数 → blocked=True", short_rows, want_blocked=True)

run_image_case("一行 action=remove → removeIndexes 含该 index 且 blocked=False",
               one_remove, want_blocked=False, want_removed=[2])

run_image_case("全部 action=remove → blocked=True + 全部被删掉",
               all_remove, want_blocked=True, want_removed=[1, 2, 3],
               message_has="全部被删掉")

run_image_case("全部 remove 时 allow_review 也不放行（与老版同强度）",
               all_remove, allow_review=True, want_blocked=True,
               message_has="全部被删掉")

run_image_case("allow_review=True 时 review 行放行但 needsReview 仍在",
               one_review, allow_review=True, want_blocked=False)

# 空主图：不归这道闸门管（规则 5 已拦），不该报「全部被删掉」
result = gate_service.main_image_gate(item(images=[]))
record("无主图 → 不阻断也不报全部被删掉",
       result["blocked"] is False and "全部被删掉" not in result["message"], result)

# --- 合并闸门 --------------------------------------------------------------
print("合并闸门 pre_save_gate")


def merged(title, factory, *, allow_title=False, allow_image=False):
    image_review_service.analyze = fake_analyze(factory)
    try:
        return gate_service.pre_save_gate(
            db, item(title), allow_title_review=allow_title,
            allow_image_review=allow_image)
    finally:
        image_review_service.analyze = _original_analyze


result = merged(DIRTY_TITLE, one_review)
record("两个都 blocked → blocks 有 2 条",
       result["ok"] is False and len(result["blocks"]) == 2, result)

result = merged(DIRTY_TITLE, all_ok)
record("只有标题 blocked → blocks 有 1 条",
       result["ok"] is False and len(result["blocks"]) == 1
       and result["blocks"][0]["gate"] == "titleReview", result)

result = merged(CLEAN_TITLE, all_remove)
record("只有主图 blocked → gate 名为 imageCleanup",
       result["ok"] is False and result["blocks"][0]["gate"] == "imageCleanup", result)

result = merged(CLEAN_TITLE, all_ok)
record("两个都放行 → ok=True 且无 blocks",
       result["ok"] is True and not result["blocks"], result)

result = merged(DIRTY_TITLE, one_review, allow_title=True, allow_image=True)
record("都 allow → ok=True，但 warnings 里仍有复核词",
       result["ok"] is True and any(REVIEW_WORD in w for w in result["warnings"]), result)

result = gate_service.pre_save_gate(db, item(CLEAN_TITLE), skip_images=True)
record("skip_images=True → 不跑 OCR", result["ok"] is True
       and result["images"].get("skipped") is True, result)

# --- pre_save_check 向后兼容 ------------------------------------------------
print("pre_save_check 规则 13/14 与向后兼容")


def baseline():
    original = {
        "cid": "603751",
        "imgUrls": ["https://a/b.jpg"],
        "notes": '<img src="https://a/d1.jpg">',
        "packageLength": 30, "packageWidth": 20, "packageHeight": 10,
        "sizeChart": "https://a/chart.png",
        "skuMap": {"s1": {"originPrice": "17.00", "price": "36.63"}},
        "skuPropertyList": [],
    }
    payload = dict(original, title="Baju Sukan")
    coverage = {"complete": True, "missingFields": []}
    return original, payload, coverage


o, p, c = baseline()
# 老场景：不传新参数，结果是 0 条 issue（与改动前一致）
legacy = pre_save_check.pre_save_issues(
    o, p, c, main_limit=9, detail_limit=30,
    package_dimensions={"packageLength": 30, "packageWidth": 20, "packageHeight": 10},
    size_chart_url="https://a/chart.png")
record("不传新参数 → 合格件仍 0 条 issue", legacy == [], legacy)

# 老场景 2：一个会命中老规则的件，issue 数量必须与改动前一致（1 条）
o2, p2, c2 = baseline()
p2 = dict(p2, cid="")
legacy2 = pre_save_check.pre_save_issues(o2, p2, c2)
record("不传新参数 → 老规则仍只报 1 条", len(legacy2) == 1, legacy2)

# 传了但为空 / 未阻断：不该多出 issue
same = pre_save_check.pre_save_issues(
    o, p, c, main_limit=9, detail_limit=30,
    package_dimensions={"packageLength": 30, "packageWidth": 20, "packageHeight": 10},
    size_chart_url="https://a/chart.png",
    title_review_words=[], image_review_pending={"blocked": False})
record("传空 reviewWords + 未阻断结果 → 仍 0 条", same == [], same)

# 规则 13 命中
rule13 = pre_save_check.pre_save_issues(o, p, c, title_review_words=[REVIEW_WORD])
record("规则 13：review 词 → issue 命中",
       len(rule13) == 1 and "require review" in rule13[0], rule13)

# 规则 14 命中
rule14 = pre_save_check.pre_save_issues(
    o, p, c, image_review_pending={"blocked": True,
                                   "message": "主图分类需要人工复核，已停止保存"})
record("规则 14：主图 blocked → issue 命中",
       len(rule14) == 1 and "main image classification" in rule14[0], rule14)

# 两条同时命中
both = pre_save_check.pre_save_issues(
    o, p, c, title_review_words=[REVIEW_WORD],
    image_review_pending={"blocked": True, "message": "x"})
record("规则 13+14 同时命中 → 2 条", len(both) == 2, both)

# 真实串联：gate_service 的结果直接喂给 pre_save_issues
image_review_service.analyze = fake_analyze(one_review)
try:
    gate = gate_service.pre_save_gate(db, item(DIRTY_TITLE))
finally:
    image_review_service.analyze = _original_analyze
chained = pre_save_check.pre_save_issues(
    o, p, c, title_review_words=gate["title"]["reviewWords"],
    image_review_pending=gate["images"])
record("串联：闸门结果喂进 pre_save_issues 得 2 条",
       len(chained) == 2, chained)

db.close()

SUMMARY = {"ok": OK, "fail": FAIL, "cases": CASES, "dataDir": _TMP}
with open(os.path.join("_meas", "probe_gate.json"), "w", encoding="utf-8") as fh:
    json.dump(SUMMARY, fh, ensure_ascii=False, indent=2)
print(f"\n结果：{OK} 通过，{FAIL} 失败 -> _meas/probe_gate.json")
sys.exit(1 if FAIL else 0)
