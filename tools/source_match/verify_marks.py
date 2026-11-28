"""在服务器上验证标记机制：写标记 → 再读 → 判定跳过。"""
import os
import sys

import psycopg

from db.session import SessionLocal
from services import draft_mark_service

url = os.environ["AOHUANG_DATABASE_URL"].replace(
    "postgresql+psycopg://", "postgresql://")

DETAIL = "3563357906"
SHOP = "19977183"
ACC = 12

db = SessionLocal()
try:
    print("=" * 70)
    print("① 标记前：应判定未处理")
    print("=" * 70)
    t = draft_mark_service.text_already_done(
        db, account_id=ACC, detail_id=DETAIL, shop_id=SHOP, language="ms")
    i = draft_mark_service.image_already_done(
        db, account_id=ACC, detail_id=DETAIL, shop_id=SHOP)
    print(f"  text_already_done  = {t}")
    print(f"  image_already_done = {i}")

    print()
    print("=" * 70)
    print("② 写标记（模拟处理成功后的置位）")
    print("=" * 70)
    draft_mark_service.mark_text_done(
        account_id=ACC, detail_id=DETAIL, shop_id=SHOP, language="ms")
    draft_mark_service.mark_image_done(
        account_id=ACC, detail_id=DETAIL, shop_id=SHOP, total=15)
    db.expire_all()

    print()
    print("=" * 70)
    print("③ 标记后：应判定已处理 → 下次跳过")
    print("=" * 70)
    t2 = draft_mark_service.text_already_done(
        db, account_id=ACC, detail_id=DETAIL, shop_id=SHOP, language="ms")
    i2 = draft_mark_service.image_already_done(
        db, account_id=ACC, detail_id=DETAIL, shop_id=SHOP)
    print(f"  text_already_done  = {t2}   {'✓ 会跳过文字' if t2 else '✗'}")
    print(f"  image_already_done = {i2}   {'✓ 会跳过图片' if i2 else '✗'}")

    print()
    print("  换语言（th）→ 必须重翻:", not draft_mark_service.text_already_done(
        db, account_id=ACC, detail_id=DETAIL, shop_id=SHOP, language="th"))
    print("  图片张数变 9 → 必须重翻:", not draft_mark_service.image_already_done(
        db, account_id=ACC, detail_id=DETAIL, shop_id=SHOP, expected_total=9))

    print()
    print("=" * 70)
    print("④ 独立事务验证：本 session 回滚后标记仍在")
    print("=" * 70)
    db.rollback()
    db.expire_all()
    t3 = draft_mark_service.text_already_done(
        db, account_id=ACC, detail_id=DETAIL, shop_id=SHOP, language="ms")
    print(f"  rollback 后 text_already_done = {t3}   "
          f"{'✓ 标记未被回滚带走' if t3 else '✗ 标记丢了'}")

    print()
    print("=" * 70)
    print("⑤ 库里实际内容")
    print("=" * 70)
    with psycopg.connect(url) as conn:
        for r in conn.execute("""
            select account_id, detail_id, shop_id, text_done, text_language,
                   image_done, image_total
            from draft_marks order by id desc limit 5""").fetchall():
            print(f"  acc={r[0]} detail={r[1]} shop={r[2]} "
                  f"text={r[3]}({r[4]}) image={r[5]}({r[6]}张)")
finally:
    db.close()