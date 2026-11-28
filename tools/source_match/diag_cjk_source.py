"""为什么 _payload_has_chinese 判 True —— payload 里的汉字出自哪个字段。"""
import warnings

warnings.filterwarnings("ignore")

from db.session import SessionLocal
from models.account import Account
from services import account_service, product_service, warehouse_service

TARGETS = ["3563339780", "3563339725", "3563339835", "3563339868"]
SHOP = "19977183"


def find_chinese(text):
    hits = [(ch, i) for i, ch in enumerate(str(text)) if "\u4e00" <= ch <= "\u9fff"]
    return hits


db = SessionLocal()
try:
    acc = db.query(Account).filter(Account.id == 12).one()
    for did in TARGETS[:2]:
        client = account_service.account_client(acc)
        d = client.shop_detail(did, SHOP)
        client.close()
        info = product_service.detail_block(d)["info"]

        print("=" * 74)
        print(f"{did}")
        print("=" * 74)

        title = str(info.get("title") or "")
        hits = find_chinese(title)
        print(f"  标题: {title[:70]}")
        print(f"      汉字: {len(hits)} 个"
              + (f"  例: {[c for c, _ in hits[:6]]}" if hits else ""))

        for prop in (info.get("skuPropertyList") or []):
            name = str(prop.get("attrName") or "")
            nh = find_chinese(name)
            if nh:
                print(f"  属性名 {name!r} 含汉字: {[c for c, _ in nh[:6]]}")
            for v in (prop.get("attrValueList") or []):
                lab = str(v.get("attrValue") or "")
                vh = find_chinese(lab)
                if vh:
                    print(f"      值 {lab!r} 含汉字: {[c for c, _ in vh[:6]]}")
finally:
    db.close()