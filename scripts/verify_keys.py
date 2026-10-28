"""逐个验证老版 4 个账号哪个能连妙手。不输出任何密钥。"""
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\codex工作台\p006\tk-product-automation\src")
sys.path.insert(0, r"E:\codex工作台\p006\tk-desktop\server")

import credential_store as cs
from services.miaoshou import MiaoshouClient, system_proxy
from core.config import get_settings

print("上游:", get_settings().miaoshou_base)
print("代理:", system_proxy())
print()

for name in cs.list_saved_connections():
    try:
        key, secret = cs.load_connection(name)
    except Exception as exc:
        print(f"[{name}] 读凭据失败: {type(exc).__name__}")
        continue
    client = MiaoshouClient(key, secret)
    try:
        data = client.shop_list()
        # 返回结构 {"list": [...]} 或 {"data": {"list": [...]}}
        shops = data.get("list") or (data.get("data") or {}).get("list") or []
        label = ", ".join(str(s.get("shopName")) for s in shops[:4]) if shops else "(空)"
        print(f"[{name}] OK  店铺 {len(shops)} 家 -> {label}")
    except Exception as exc:
        print(f"[{name}] 失败 {type(exc).__name__}: {str(exc)[:140]}")
