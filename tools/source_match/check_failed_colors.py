"""确认：失败那几件的草稿颜色到底是什么，清洗前后差异。"""
import warnings

warnings.filterwarnings("ignore")

from db.session import SessionLocal
from models.account import Account
from services import account_service, warehouse_service

TARGETS = [
    ("3563343903", "19977183"),
    ("3563343858", "19977183"),
    ("3563343878", "19977183"),
    ("3563343842", "19977183"),
]


def colors(info):
    out = []
    for p in (info.get("skuPropertyList") or []):
        if not isinstance(p, dict):
            continue
        if not warehouse_service._is_color_property(p):
            continue
        for v in (p.get("attrValueList") or []):
            if isinstance(v, dict):
                out.append(str(v.get("attrValue")))
    return out


db = SessionLocal()
try:
    acc = db.query(Account).filter(Account.id == 12).one()
    for did, shop in TARGETS:
        client = account_service.account_client(acc)
        try:
            d = client.shop_detail(did, shop)
            info = (d.get("data") or {}).get("shopCollectItemInfo") or {}
        except Exception as ex:  # noqa: BLE001
            print(f"  {did} 拉取失败 {type(ex).__name__}")
            client.close()
            continue
        client.close()

        raw = colors(info)
        print("=" * 70)
        print(f"{did}  草稿颜色: {raw}")
        # 模拟清洗
        cleaned = []
        for v in raw:
            try:
                from services import color_service
                cleaned.append(color_service.clean_color_strict(v))
            except Exception:  # noqa: BLE001
                cleaned.append("<<认不出，会被删>>")
        print(f"       清洗后: {cleaned}")
        changed = [f"{a!r}→{b!r}" for a, b in zip(raw, cleaned) if a != b]
        if changed:
            print(f"       ⚠ 被改写: {changed}")
        title = str(info.get("title") or "")[:45]
        print(f"       标题: {title}")
finally:
    db.close()
