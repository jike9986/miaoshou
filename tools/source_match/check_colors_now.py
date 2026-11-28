"""确认：妙手草稿现在到底是什么颜色？为什么 original 报 2 个、实际拉回来 1 个。

上一轮 trace 显示：重新 shop_detail 拉到的是 ['Hitam']（1 个），
但读回核验时 original 是 ['Beras hitam.', 'Meter']（2 个）。

两种可能：
  A. 我们自己的提交把妙手草稿改了（Beras hitam./Meter → Hitam）
  B. original 与 actual 来自不同的读取时机/来源，缓存不一致
"""
import json
import re
import warnings

warnings.filterwarnings("ignore")

from db.session import SessionLocal
from models.account import Account
from services import account_service

DETAIL = "3563357906"
SHOP = "19977183"

db = SessionLocal()
try:
    acc = db.query(Account).filter(Account.id == 12).one()
    client = account_service.account_client(acc)

    print("=" * 74)
    print("连续拉 3 次妙手草稿，看颜色是否稳定")
    print("=" * 74)
    for i in range(3):
        d = client.shop_detail(DETAIL, SHOP)
        info = (d.get("data") or {}).get("shopCollectItemInfo") or {}
        props = info.get("skuPropertyList") or []
        desc = []
        for p in props:
            if not isinstance(p, dict):
                continue
            vals = p.get("attrValueList") or []
            labs = [str(v.get("attrValue")) for v in vals if isinstance(v, dict)]
            desc.append(f"{p.get('attrName')}(attrId={p.get('attrId')})={labs}")
        print(f"  第{i+1}次: {desc}")

    print()
    print("=" * 74)
    print("skuMap 的 key（含颜色文本）")
    print("=" * 74)
    d = client.shop_detail(DETAIL, SHOP)
    info = (d.get("data") or {}).get("shopCollectItemInfo") or {}
    sku_map = info.get("skuMap") or {}
    if isinstance(sku_map, str):
        try:
            sku_map = json.loads(sku_map)
        except Exception:  # noqa: BLE001
            sku_map = {}
    print(f"  skuMap 条数: {len(sku_map)}")
    for k in list(sku_map)[:8]:
        print(f"    {k}")

    print()
    print("=" * 74)
    print("标题 / 时间戳（看草稿是否被我们改过）")
    print("=" * 74)
    print(f"  title = {str(info.get('title'))[:60]}")
    for k in ("updateTime", "modifyTime", "gmtModified", "createTime"):
        if k in info:
            print(f"  {k} = {info.get(k)}")

    client.close()
finally:
    db.close()