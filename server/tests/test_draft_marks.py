"""草稿处理标记：文字 / 图片各自记一次，下次处理跳过。

负责人口径（2026-11-27）：
  「一个商品是分图片和文字处理，哪个处理过了就里面打标记，下次处理就会跳过」

## 这个测试守的是什么

2026-11-27 实测的 6 件商品全部卡在同一个死循环：

    第 1 次处理 → 中文颜色清洗把 `Meter`（马来语「米色」）当厂家广告词删掉
                → 提交时 2 个颜色只剩 1 个 → 写回妙手草稿
                → 读回核验 original(2) vs actual(1) → skuImages 不一致 → 失败
    第 2 次处理 → 草稿已是马来语，又拿中文规则洗一遍 → 同样失败
                → 而且每次都已经把草稿改坏了

根因是**没有"处理过"的标记**，所以每次重新处理都从头洗一遍。

## 两条硬要求

1. 标记挡住重复处理；
2. 标记**独立事务提交** —— 文字已经写回草稿了，读回核验失败导致调用方
   rollback 时，标记不能跟着丢，否则下次还是从头洗，死循环出不来。
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from db.session import SessionLocal, init_db
from services import draft_mark_service


@pytest.fixture(scope="module", autouse=True)
def _schema():
    """保证 draft_marks 表存在（create_all + _add_missing_columns）。"""
    init_db()


@pytest.fixture()
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _clean(account_id=12, detail_id="", shop_id="19977183"):
    """每个用例前后清掉自己的标记，避免互相污染。

    测试跑在临时库里，但同一模块内的用例共用文件，必须自己收尾。
    """
    s = SessionLocal()
    try:
        s.execute(
            text("delete from draft_marks where account_id = :a "
                 "and detail_id = :d and shop_id = :s"),
            {"a": account_id, "d": detail_id, "s": shop_id})
        s.commit()
    finally:
        s.close()


def test_text_mark_then_skip(db):
    """文字标记写入后，同样语言应判定为已处理。"""
    kw = dict(account_id=12, detail_id="3563357906", shop_id="19977183")
    _clean(**{k: v for k, v in kw.items() if k != "shop_id"},
           shop_id="19977183")
    assert not draft_mark_service.text_already_done(db, language="ms", **kw)

    draft_mark_service.mark_text_done(language="ms", **kw)
    db.expire_all()

    assert draft_mark_service.text_already_done(db, language="ms", **kw)


def test_language_change_forces_rerun(db):
    """换了目标语言必须重做 ——「中文处理过」不等于「马来语处理过」。"""
    kw = dict(account_id=12, detail_id="3563357907", shop_id="19977183")
    _clean(12, "3563357907")
    draft_mark_service.mark_text_done(language="ms", **kw)
    db.expire_all()

    assert draft_mark_service.text_already_done(db, language="ms", **kw)
    assert not draft_mark_service.text_already_done(db, language="th", **kw), \
        "换语言必须重新翻译"
    assert not draft_mark_service.text_already_done(db, language="", **kw), \
        "不翻译的路径也是另一回事"


def test_text_mark_survives_caller_rollback():
    """标记必须独立于调用方事务 —— 整个设计的关键。

    真实场景：文字已经写回妙手草稿，但读回核验判不一致，调用方 rollback。
    若标记跟着回滚，下次处理又从头洗一遍（2026-11-27 那 6 件正是如此）。
    """
    kw = dict(account_id=12, detail_id="3563357908", shop_id="19977183")
    _clean(12, "3563357908")

    outer = SessionLocal()
    try:
        outer.execute(text("select 1"))
        draft_mark_service.mark_text_done(language="ms", **kw)
        outer.rollback()          # 模拟读回核验失败后的回滚
    finally:
        outer.close()

    fresh = SessionLocal()
    try:
        assert draft_mark_service.text_already_done(
            fresh, language="ms", **kw), "标记不该被调用方回滚带走"
    finally:
        fresh.close()


def test_image_mark_respects_total(db):
    """图片标记要认张数：商品换图后必须重翻。"""
    kw = dict(account_id=12, detail_id="3563357863", shop_id="19977183")
    _clean(12, "3563357863")
    assert not draft_mark_service.image_already_done(db, **kw)

    draft_mark_service.mark_image_done(total=15, **kw)
    db.expire_all()

    assert draft_mark_service.image_already_done(db, expected_total=15, **kw)
    assert not draft_mark_service.image_already_done(
        db, expected_total=9, **kw), "张数变了说明换图，必须重翻"
    assert draft_mark_service.image_already_done(db, **kw), \
        "不传张数时只认「跑完过」"


def test_clear_forces_rerun(db):
    """手动失效后必须重跑（运营改了图 / 换了语言）。"""
    kw = dict(account_id=12, detail_id="3563353396", shop_id="19977183")
    _clean(12, "3563353396")
    draft_mark_service.mark_text_done(language="ms", **kw)
    draft_mark_service.mark_image_done(total=7, **kw)
    db.expire_all()
    assert draft_mark_service.text_already_done(db, language="ms", **kw)
    assert draft_mark_service.image_already_done(db, **kw)

    draft_mark_service.clear_text(**kw)
    draft_mark_service.clear_image(**kw)
    db.expire_all()

    assert not draft_mark_service.text_already_done(db, language="ms", **kw)
    assert not draft_mark_service.image_already_done(db, **kw)


def test_marks_isolated_by_shop(db):
    """同一 detail_id 在不同店铺是两件草稿，标记不能串。"""
    _clean(12, "3563357820", "19977183")
    _clean(12, "3563357820", "18077147")
    draft_mark_service.mark_text_done(
        account_id=12, detail_id="3563357820", shop_id="19977183",
        language="ms")
    db.expire_all()

    assert draft_mark_service.text_already_done(
        db, account_id=12, detail_id="3563357820",
        shop_id="19977183", language="ms")
    assert not draft_mark_service.text_already_done(
        db, account_id=12, detail_id="3563357820",
        shop_id="18077147", language="ms"), "不同店铺是不同的草稿"


def test_marks_isolated_by_account(db):
    """不同账号即使 detail_id 一样也是两件事。"""
    _clean(3, "3533606979", "18749000")
    _clean(5, "3533606979", "18749000")
    draft_mark_service.mark_image_done(
        account_id=3, detail_id="3533606979", shop_id="18749000", total=5)
    db.expire_all()

    assert draft_mark_service.image_already_done(
        db, account_id=3, detail_id="3533606979", shop_id="18749000")
    assert not draft_mark_service.image_already_done(
        db, account_id=5, detail_id="3533606979", shop_id="18749000")


def test_mark_write_failure_does_not_raise(monkeypatch):
    """标记写失败只记日志，不能让整件商品处理失败。

    标记写不进去最多导致下次多处理一遍，比让商品处理失败轻得多。
    """
    _clean(12, "3563357851")

    def boom(*args, **kwargs):
        raise RuntimeError("模拟写库失败")

    monkeypatch.setattr(draft_mark_service, "_upsert", boom)
    # 不应抛异常
    draft_mark_service.mark_text_done(
        account_id=12, detail_id="3563357851", shop_id="19977183",
        language="ms")
