#!/usr/bin/env python3
"""把 auto-temp 里的图搬到 products 成品区，并**同步改掉 payload 里的地址**。

## 这个脚本原来有两个致命毛病（2026-11-27 修）

### 1. 只改旧列、不改 payload —— 图片全 404

原版只更新 `products.detail_images` 和 `products.images` 两个**遗留列**：

    UPDATE products SET detail_images = ..., images = ...

但妙手实际拿的是 `payload`（`notes` 里的 <img src>、
`skuPropertyList[].attrValueList[].imgUrl`、`imgUrls`、尺码图）。

结果 `promote_to_product_zone()` 把文件从 auto-temp **搬走并 unlink 原文件**，
payload 里的地址却还指着 auto-temp —— 妙手打开商品全是空白框。

2026-11-27 实测：

    旧列里还有 auto-temp 的:     0 条     ← 脚本一直在"正常工作"
    payload 里还有 auto-temp 的: 84 条    ← 真正在用的地方一条没改
    auto-temp 最新文件: Nov 26 22:32     ← 之后没新文件
    products 最新文件:  Nov 27 02:01     ← 正是 02:00 这次 cron

负责人截图里「这些处理的图片全部没有了」就是这个。

### 2. 连「还在等确认」的自动化结果也搬走

`warehouse_service.save_from_collect_box()` 里搬图片的代码有明确条件：

    # 搬运挪到 admit_automation_results（用户确认「加入成品仓库」那一刻）
    # 直接入仓的路径（不走自动化结果）仍然在这里搬
    if not automation_result:
        promote_report = image_upload.promote_payload_images(payload)

设计意图很清楚：**自动化结果先留在临时区，等用户确认入库那一刻才搬**。
这个 cron 不看 `automation_result`，把所有商品的图都搬走了，等于绕过那个设计。

## 现在的口径

- `automation_result = true` 且图仍在临时区 → **跳过**，图原地不动
- `automation_result = true` 但图已被搬走（历史上被这脚本搬的）→ **只改地址，不搬文件**
- 其余商品 → 搬文件，并把 payload 里的地址一起改掉
- 默认 dry-run，加 `--apply` 才真写；`--id N` 只处理单件
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

SERVER_DIR = Path("/opt/miaoshou-listing-v2/server")
sys.path.insert(0, str(SERVER_DIR))
os.chdir(SERVER_DIR)

env_file = Path("/etc/aohuang-listing-v2.env")
if env_file.is_file():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

from services import image_upload  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

DB_URL = os.environ.get("AOHUANG_DATABASE_URL", os.environ.get("DATABASE_URL", ""))
engine = create_engine(DB_URL)

# 从 HTML 里截 URL 时，遇到这些字符就认为地址结束
URL_STOP_CHARS = ('"', "'", "\n", "\r", " ", "\t", ">")


def local_zone_dir(zone: str) -> str:
    _d, _b, local_env, _be = image_upload._zone_paths(zone)
    return os.environ.get(local_env, "").strip().rstrip("/")


def collect_auto_names(node, out: set) -> None:
    """收集 payload 里所有仍指向 auto-temp 的文件名（含 HTML 内嵌的）。"""
    base = image_upload._zone_paths("auto")[1]
    if isinstance(node, str):
        idx = 0
        while True:
            i = node.find(base, idx)
            if i < 0:
                break
            tail = node[i + len(base):]
            end = len(tail)
            for stop in URL_STOP_CHARS:
                j = tail.find(stop)
                if 0 <= j < end:
                    end = j
            name = tail[:end]
            if name and "/" not in name:
                out.add(name)
            idx = i + len(base) + len(name)
    elif isinstance(node, list):
        for item in node:
            collect_auto_names(item, out)
    elif isinstance(node, dict):
        for item in node.values():
            collect_auto_names(item, out)


def files_still_in_auto(payload) -> bool:
    """payload 引用的图是否**全部**仍在临时区。True = 不用动它。"""
    names: set = set()
    collect_auto_names(payload, names)
    if not names:
        return True
    auto_dir = local_zone_dir("auto")
    if not auto_dir:
        return True
    return all(os.path.isfile(os.path.join(auto_dir, n)) for n in names)


def move_and_rewrite(payload) -> int:
    """搬文件到成品区并返回新地址。返回改了几处。"""
    auto_base = image_upload._zone_paths("auto")[1]

    def walk(node):
        if isinstance(node, str):
            if node.startswith(auto_base):
                new = image_upload.promote_to_product_zone(node)
                if new and new != node:
                    MOVE_COUNT[0] += 1
                    return new
                return node
            if auto_base in node:
                out = node
                start = 0
                while True:
                    i = out.find(auto_base, start)
                    if i < 0:
                        break
                    tail = out[i + len(auto_base):]
                    end = len(tail)
                    for stop in URL_STOP_CHARS:
                        j = tail.find(stop)
                        if 0 <= j < end:
                            end = j
                    name = tail[:end]
                    new = image_upload.promote_to_product_zone(auto_base + name)
                    if new and new != auto_base + name:
                        out = out[:i] + new + tail[end:]
                        MOVE_COUNT[0] += 1
                        start = i + len(new)
                    else:
                        start = i + len(auto_base) + len(name)
                return out
            return node
        if isinstance(node, list):
            return [walk(item) for item in node]
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        return node

    MOVE_COUNT = [0]
    result = walk(payload)
    if MOVE_COUNT[0]:
        payload.clear()
        payload.update(result)
    return MOVE_COUNT[0]


def rewrite_zone_only(payload) -> int:
    """只把地址改成文件真实所在的分区，**不搬文件**。返回改了几处。

    文件已经不在临时区、但成品区有同名文件的，说明此前搬运过，
    只是地址没跟上。这种情况改地址就够了。
    """
    auto_base = image_upload._zone_paths("auto")[1]
    product_base = image_upload._zone_paths("product")[1]
    auto_dir = local_zone_dir("auto")
    product_dir = local_zone_dir("product")
    fixed = [0]

    def should_fix(name: str) -> bool:
        if not name or "/" in name or not auto_dir or not product_dir:
            return False
        return (not os.path.isfile(os.path.join(auto_dir, name))
                and os.path.isfile(os.path.join(product_dir, name)))

    def walk(node):
        if isinstance(node, str):
            if node.startswith(auto_base):
                name = node[len(auto_base):].strip()
                if should_fix(name):
                    fixed[0] += 1
                    return product_base + name
                return node
            # notes 是整段 HTML，地址嵌在 <img src="..."> 里。
            # 2026-11-27 实测：#639 修完 11 处后仍有 9 个 auto-temp 地址，
            # 全在 notes 里 —— 只判 startswith 对 HTML 永远为假。
            if auto_base in node:
                out = node
                start = 0
                while True:
                    i = out.find(auto_base, start)
                    if i < 0:
                        break
                    tail = out[i + len(auto_base):]
                    end = len(tail)
                    for stop in URL_STOP_CHARS:
                        j = tail.find(stop)
                        if 0 <= j < end:
                            end = j
                    name = tail[:end]
                    if should_fix(name):
                        out = out[:i] + product_base + tail
                        fixed[0] += 1
                        start = i + len(product_base) + len(name)
                    else:
                        start = i + len(auto_base) + len(name)
                return out
            return node
        if isinstance(node, list):
            return [walk(item) for item in node]
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        return node

    result = walk(payload)
    if fixed[0]:
        payload.clear()
        payload.update(result)
    return fixed[0]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="真写库；不加只统计")
    parser.add_argument("--include-pending", action="store_true",
                        help="连还没确认入库的自动化结果也搬（默认只修地址）")
    parser.add_argument("--id", type=int, action="append", default=None,
                        help="只处理指定商品 id（可重复），用来先验证单件")
    args = parser.parse_args()

    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT id, detail_id, payload, automation_result
            FROM products
            WHERE deleted_at IS NULL
              AND payload::text LIKE '%auto-temp%'
            ORDER BY id
        """)).fetchall()
        if args.id:
            wanted = set(args.id)
            rows = [r for r in rows if r[0] in wanted]

        print(f"找到 {len(rows)} 件商品的图还在临时区")

        migrated = skipped = repaired = 0
        total_moved = 0
        for r in rows:
            pid, did = r[0], r[1]
            payload = r[2] if isinstance(r[2], dict) else json.loads(r[2] or "{}")
            auto_result = bool(r[3])

            if auto_result and not args.include_pending:
                # 待确认的自动化结果：图该留在临时区，不主动搬。
                # 但如果文件已经被搬走了（历史遗留），就必须改地址，否则永远 404。
                if files_still_in_auto(payload):
                    skipped += 1
                    print(f"  跳过 #{pid} {did}（自动化结果待确认，图仍在临时区）")
                    continue
                n = rewrite_zone_only(payload)
                if n:
                    repaired += 1
                    if args.apply:
                        conn.execute(text(
                            "UPDATE products SET payload = CAST(:p AS json), "
                            "updated_at = EXTRACT(EPOCH FROM NOW()) WHERE id = :pid"
                        ), {"p": json.dumps(payload, ensure_ascii=False), "pid": pid})
                    tag = "✓" if args.apply else "[dry]"
                    print(f"  {tag} #{pid} {did}: {n} 处地址改到真实位置（未搬文件）")
                continue

            moved = move_and_rewrite(payload)
            if not moved:
                continue
            total_moved += moved
            migrated += 1
            if args.apply:
                conn.execute(text(
                    "UPDATE products SET payload = CAST(:p AS json), "
                    "updated_at = EXTRACT(EPOCH FROM NOW()) WHERE id = :pid"
                ), {"p": json.dumps(payload, ensure_ascii=False), "pid": pid})
                print(f"  ✓ #{pid} {did}: {moved} 张图已搬，payload 同步")
            else:
                print(f"  [dry] #{pid} {did}: {moved} 张图待搬")

        if args.apply:
            conn.commit()

        print()
        print(f"{'已搬' if args.apply else '[dry-run] 将搬'} {migrated} 件，"
              f"共 {total_moved} 张图")
        print(f"修复地址（文件已被搬走、只改 payload）: {repaired} 件")
        print(f"跳过（图仍在临时区、待确认）: {skipped} 件")
        if not args.apply:
            print("加 --apply 才真写。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
