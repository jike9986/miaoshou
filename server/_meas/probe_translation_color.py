"""测试翻译接口是否实际返回翻译后的颜色值。

用法（在 server/ 目录下运行）：
  python -I _meas/probe_translation_color.py

功能：
  1. 从 app.db 拿最近一条已处理商品的 payload 和对应账号的 miaoshou token
  2. 调 translate_collect_item（isTranslateSkuProperty=1）
  3. 打印上游返回的 skuPropertyList 颜色维度的值
  4. 把完整结果写到 _meas/probe_translation_color.json
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT = ROOT / "_meas" / "probe_translation_color.json"
DB_PATH = Path(os.environ.get("AOHUANG_DATA_DIR") or "") / "app.db"
if not DB_PATH.exists():
    DB_PATH = Path(os.environ.get("LOCALAPPDATA", "")) / "aohuang-desktop" / "app.db"
if not DB_PATH.exists():
    print("ERROR: 找不到 app.db，请设置 AOHUANG_DATA_DIR")
    sys.exit(1)

print(f"使用数据库: {DB_PATH}")

import sqlite3

conn = sqlite3.connect(str(DB_PATH))
conn.row_factory = sqlite3.Row

# 找最近一条有 skuPropertyList 的已处理商品
cur = conn.execute("""
    SELECT p.payload, a.miaoshou_token
    FROM products p
    JOIN accounts a ON a.id = p.account_id
    WHERE p.status IN ('ready', 'processed', 'done')
      AND p.deleted_at IS NULL
    ORDER BY p.id DESC
    LIMIT 50
""")
rows = cur.fetchall()
conn.close()

target_payload = None
token = None
for row in rows:
    try:
        payload = json.loads(row["payload"])
    except Exception:
        continue
    props = payload.get("skuPropertyList") or []
    # 找有颜色维度（attrId=100000 或 attrName 含颜色词）的商品
    has_color = any(
        str(p.get("attrId") or "") == "100000"
        or any(w in str(p.get("attrName") or "").lower()
               for w in ("color", "warna", "颜色", "色彩"))
        for p in props if isinstance(p, dict)
    )
    if has_color and row["miaoshou_token"]:
        target_payload = payload
        token = row["miaoshou_token"]
        break

if not target_payload:
    print("ERROR: 没找到带颜色维度的已处理商品，或账号没有 token")
    sys.exit(1)

props = target_payload.get("skuPropertyList") or []
print(f"\n商品标题: {target_payload.get('title','')[:60]}")
print("处理前 skuPropertyList 颜色值:")
for p in props:
    if not isinstance(p, dict):
        continue
    vals = [v.get("attrValue") for v in (p.get("attrValueList") or []) if isinstance(v, dict)]
    print(f"  attrName={p.get('attrName')} attrId={p.get('attrId')} propId={p.get('propId')} values={vals[:5]}")

# 只给翻译接口发必要字段，避免多余字段触发平台校验
translate_input = {
    "title": target_payload.get("title", ""),
    "description": target_payload.get("description", ""),
    "skuPropertyList": props,
}

from services.miaoshou import MiaoshouClient

client = MiaoshouClient(token=token)
print(f"\n调用 translate_collect_item (isTranslateSkuProperty=1)...")
try:
    resp = client.translate_collect_item(
        translate_input,
        target_language="ms",
        translate_sku_property=True,
    )
except Exception as exc:
    print(f"ERROR: {exc}")
    sys.exit(1)

data = (resp or {}).get("data") or {}
translated_info = (data.get("shopCollectItemInfo")
                   or data.get("collectItemInfo")
                   or {})

translated_props = translated_info.get("skuPropertyList") or []
print(f"\n上游返回 skuPropertyList 条数: {len(translated_props)}")
result = []
for p in translated_props:
    if not isinstance(p, dict):
        continue
    vals = [v.get("attrValue") for v in (p.get("attrValueList") or []) if isinstance(v, dict)]
    ids  = [v.get("attrValueId") for v in (p.get("attrValueList") or []) if isinstance(v, dict)]
    info = {
        "attrName": p.get("attrName"),
        "attrId": p.get("attrId"),
        "propId": p.get("propId"),
        "propName": p.get("propName"),
        "values": vals[:8],
        "valueIds": ids[:8],
    }
    result.append(info)
    print(f"  attrName={p.get('attrName')} propName={p.get('propName')} attrId={p.get('attrId')} propId={p.get('propId')}")
    print(f"    values: {vals[:8]}")
    print(f"    ids:    {ids[:8]}")

print(f"\n翻译后标题: {translated_info.get('title','')[:80]}")

OUT.write_text(json.dumps({
    "title_before": target_payload.get("title", "")[:100],
    "title_after": translated_info.get("title", "")[:100],
    "props_before": [
        {"attrName": p.get("attrName"), "attrId": p.get("attrId"),
         "values": [v.get("attrValue") for v in (p.get("attrValueList") or []) if isinstance(v, dict)][:8]}
        for p in props if isinstance(p, dict)
    ],
    "props_after": result,
    "raw_response_data_keys": list(data.keys()),
    "translated_info_keys": list(translated_info.keys()),
}, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\n完整结果已写入: {OUT}")
