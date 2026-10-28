"""看 shop_list 的真实返回结构（去敏，只打印键名和条数）。"""
import io
import json
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\codex工作台\p006\tk-product-automation\src")
sys.path.insert(0, r"E:\codex工作台\p006\tk-desktop\server")

import credential_store as cs
from services.miaoshou import MiaoshouClient, system_proxy

print("代理:", system_proxy())
name = cs.list_saved_connections()[1]  # 罗蔓
key, secret = cs.load_connection(name)
print("测试账号:", name)

client = MiaoshouClient(key, secret)
data = client.shop_list()
print("顶层键:", list(data.keys()) if isinstance(data, dict) else type(data))

def shape(obj, depth=0, max_depth=3):
    pad = "  " * depth
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                n = len(v)
                print(f"{pad}{k}: {type(v).__name__}({n})")
                if depth < max_depth:
                    shape(v, depth + 1, max_depth)
            else:
                print(f"{pad}{k}: {type(v).__name__} = {str(v)[:60]}")
    elif isinstance(obj, list):
        print(f"{pad}[0..{len(obj)}]")
        if obj and depth < max_depth:
            shape(obj[0], depth + 1, max_depth)

shape(data)
print()
print("--- 原始 JSON 前 800 字 ---")
s = json.dumps(data, ensure_ascii=False)
# 去掉疑似密钥的字段
print(s[:800])
