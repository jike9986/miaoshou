"""在真实 payload 上验证 _payload_has_chinese，并复现是否真的跳过了清洗。"""
import warnings

warnings.filterwarnings("ignore")

from db.session import SessionLocal
from models.account import Account
from services import account_service, product_service, warehouse_service as ws

TARGETS = ["3563339780", "3563339725", "3563339835", "3563339868"]
SHOP = "19977183"

db = SessionLocal()
try:
    acc = db.query(Account).filter(Account.id == 12).one()
    for did in TARGETS:
        client = account_service.account_client(acc)
        d = client.shop_detail(did, SHOP)
        client.close()
        info = product_service.detail_block(d)["info"]
        payload = dict(info)

        has = ws._payload_has_chinese(payload)
        print("=" * 70)
        print(f"{did}  _payload_has_chinese = {has}")
        if has:
            # 打印汉字来源
            for field in ("title",):
                t = str(payload.get(field) or "")
                hits = [c for c in t if "\u4e00" <= c <= "\u9fff"]
                if hits:
                    print(f"    title 含汉字 {len(hits)}: {hits[:8]}")
                    print(f"    title = {t[:80]}")
            for prop in (payload.get("skuPropertyList") or []):
                nm = str(prop.get("attrName") or "")
                nh = [c for c in nm if "\u4e00" <= c <= "\u9fff"]
                if nh:
                    print(f"    attrName {nm!r} 含汉字 {nh[:6]}")
                for v in (prop.get("attrValueList") or []):
                    lab = str(v.get("attrValue") or "")
                    vh = [c for c in lab if "\u4e00" <= c <= "\u9fff"]
                    if vh:
                        print(f"    值 {lab!r} 含汉字 {vh[:6]}")
        else:
            print("    → 应该跳过清洗（马来语 payload）")
finally:
    db.close()