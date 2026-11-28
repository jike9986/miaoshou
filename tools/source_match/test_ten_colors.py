"""拿十件真实商品验证颜色清洗判据（负责人要求：十个都没问题才算过）。

对每件商品打印：
  · 标题（前 40 字）
  · 每个规格属性的名字 + 值
  · `_colors_need_chinese_cleaning` 判定
  · 判定是否符合预期（该洗的洗、不该洗的不洗）

判定标准由商品本身的颜色值决定：
  颜色值含汉字 → 必须 True（要洗）
  颜色值无汉字 → 必须 False（不许洗）
"""
import warnings

warnings.filterwarnings("ignore")

from db.session import SessionLocal
from models.account import Account
from services import account_service, product_service, warehouse_service as ws

SHOP = "19977183"
LIMIT = 10


def colors_of(info):
    """取出颜色维度的值列表。"""
    out = []
    for prop in (info.get("skuPropertyList") or []):
        if not isinstance(prop, dict):
            continue
        if not ws._is_color_property(prop):
            continue
        for v in (prop.get("attrValueList") or []):
            if isinstance(v, dict):
                out.append(str(v.get("attrValue") or ""))
    return out


db = SessionLocal()
try:
    acc = db.query(Account).filter(Account.id == 12).one()
    client = account_service.account_client(acc)
    resp = client.collect_box_list(status="notPublished", keyword="",
                                   page_no=1, page_size=500)
    rows = ((resp or {}).get("data") or {}).get("detailList") or []
    client.close()

    details = []
    for r in rows:
        d = r.get("collectBoxDetailId") or r.get("detailId")
        if d not in (None, "") and str(d) not in details:
            details.append(str(d))
    targets = details[:LIMIT]
    print(f"采集箱共 {len(rows)} 行，取前 {len(targets)} 件实测\n")

    ok = 0
    bad = 0
    for n, did in enumerate(targets, 1):
        client = account_service.account_client(acc)
        try:
            d = client.shop_detail(did, SHOP)
            info = ((d.get("data") or {}).get("shopCollectItemInfo") or {})
        except Exception as ex:  # noqa: BLE001
            print(f"[{n:>2}] {did}  拉取失败 {type(ex).__name__}")
            client.close()
            bad += 1
            continue
        client.close()

        title = str(info.get("title") or "")[:44]
        cols = colors_of(info)
        has_cjk = any(any("\u4e00" <= c <= "\u9fff" for c in v) for v in cols)
        got = ws._colors_need_chinese_cleaning(dict(info))

        # 期望：颜色含汉字才洗
        want = has_cjk
        mark = "OK " if got == want else "FAIL"
        if got == want:
            ok += 1
        else:
            bad += 1

        print(f"[{n:>2}] {mark} {did}")
        print(f"     标题: {title}")
        print(f"     颜色: {cols}")
        print(f"     含汉字: {has_cjk}   判定需清洗: {got}   期望: {want}")
        if got != want:
            print(f"     >>> 不符 <<<")
        print()

    print("=" * 60)
    print(f"结果: {ok}/{len(targets)} 通过，{bad} 件不符")
    print("=" * 60)
finally:
    db.close()