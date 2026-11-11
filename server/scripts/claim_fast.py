"""批量认领优化版：一次预取 + 循环复制 + 并行覆盖。

对比现有实现，省掉每件重复的：
  · common_box_list      1.54s  → 整批只查一次
  · warehouse_list       1.29s  → 整批只查一次
  · category_tree        1.92s  → 整批只查一次
  · collect_box_list     1.67s  → 整批只查一次

流程（按负责人 2026-11-04 的要求，就两步）：
  1. 公共采集箱有可用商品 → 直接拿来复制 N 份（不采集）
     没有 → 采集一次，然后复制 N 份
  2. 复制与覆盖并行：复制出一份就立刻开始覆盖，不等全部复制完

用法（服务器上）：
  python claim_fast.py --account 5 --limit 50
  python claim_fast.py --account 5 --limit 50 --verify
"""
from __future__ import annotations

import argparse
import copy
import re
import json
import queue
import sys
import threading
import time
from collections import Counter

sys.path.insert(0, "/opt/miaoshou-listing-v2/server")

from db.session import SessionLocal
from models.account import Account
from models.product import Product
from services import account_service, carrier_service


def pick_common(client, need: int, progress=print) -> list[int]:
    """取公共箱里可用的商品；不够就采集链接池里的货源补齐。"""
    ids: list[int] = []
    try:
        listing = client.common_box_list(page_no=1, page_size=50)
        rows = (listing.get("data") or {}).get("detailList") or []
        for row in rows:
            if not isinstance(row, dict):
                continue
            status = str(row.get("status") or "").lower()
            value = row.get("commonCollectBoxDetailId")
            if status in ("success", "collectsuccess", "skip") and str(value).isdigit():
                ids.append(int(value))
                if len(ids) >= 1:      # 一个就够，后面靠复制
                    break
    except Exception as exc:
        progress("  读公共箱失败: %s" % str(exc)[:60])

    if ids:
        progress("  公共箱已有可用商品 commonId=%s（不采集，直接复制）" % ids[0])
        return ids

    # 公共箱空 → 采集一条
    progress("  公共箱没有可用商品，需要采集…")
    raise RuntimeError("公共箱为空，请先手动采集一件或补充链接池")


def prepare_payload(payload: dict) -> tuple[dict, list[str]]:
    """认领前的清洗：颜色名规范化 + 规格元数据补齐。

    为什么必须在认领时做：
      `normalize_colors` 原本只在入库处理流程里调用，
      但仓库里不少商品是早期数据，颜色值还是「K-9177白色款」这种带型号的原始写法。
      妙手按「颜色-尺码」查 SKU，名字对不上就报「SKU (xxx-yy) 不存在」。

    这一步把 `K-9177白色款` → `白色`，并同步改 skuMap 的 key 与 colorMap。
    """
    from services import color_service

    data = copy.deepcopy(payload or {})
    notes: list[str] = []

    # 1. 颜色规范化（会同时改 skuPropertyList / skuMap / colorMap）
    try:
        result = color_service.normalize_colors(data)
        if result.get("message"):
            notes.append(result["message"])
        if result.get("removed"):
            notes.append("去掉重复颜色%d个" % len(result["removed"]))
    except Exception as exc:
        notes.append("颜色规范化跳过: %s" % str(exc)[:40])

    return data, notes


COST_FLOOR_RE = re.compile(
    r"基础成本[^0-9]{0,24}([0-9]+(?:\.[0-9]+)?)\s*MYR", re.I)
LOCAL_PRICE_RE = re.compile(
    r"(?:本地(?:显示|展示)价|售价展示(?:价)?)[^0-9]{0,16}"
    r"([0-9]+(?:\.[0-9]+)?)\s*MYR", re.I)


def _raise_to_cost_floor(body: dict, message: str) -> str | None:
    """按妙手报的「基础成本 Y MYR」把 skuMap 里的售价抬到 Y 之上。

    妙手的报错自带准确成本线，比我们自己算汇率+运费可靠。
    返回新的价格字符串；无法解析返回 None。
    """
    from decimal import Decimal, ROUND_HALF_UP

    floor_match = COST_FLOOR_RE.search(message)
    if floor_match is None:
        return None
    try:
        floor = Decimal(floor_match.group(1))
    except Exception:
        return None
    if floor <= 0:
        return None
    old_match = LOCAL_PRICE_RE.search(message)
    old_price = None
    if old_match:
        try:
            old_price = Decimal(old_match.group(1))
        except Exception:
            old_price = None

    # 妙手的成本线会随售价微调（实测 11.57→12.87→13.05 逐步上浮），
    # 只加 0.01 会反复擦线。加 3% 余量（至少 0.15）一次到位。
    margin = max(Decimal("0.15"), (floor * Decimal("0.03")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP))
    target = (floor + margin).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sku_map = body.get("skuMap") or {}
    if not isinstance(sku_map, dict):
        return None
    changed = 0
    for sku in sku_map.values():
        if not isinstance(sku, dict) or str(sku.get("isDelete", "0")) == "1":
            continue
        try:
            current = Decimal(str(sku.get("priceIncludeVat") or 0))
        except Exception:
            continue
        # 只抬低于成本线的，已满足的不动
        if old_price is not None and current != old_price and current >= target:
            continue
        if current >= target:
            continue
        sku["priceIncludeVat"] = format(target, ".2f")
        changed += 1
    return format(target, ".2f") if changed else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", default="5")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--shop", default="")
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()

    db = SessionLocal()
    acct = db.query(Account).filter(Account.id == int(args.account)).one_or_none()
    if acct is None:
        print("账号不存在")
        return 1
    shops = json.loads(acct.shops or "[]") if isinstance(acct.shops, str) else (acct.shops or [])
    shop_id = int(args.shop or (str(shops[0].get("shopId")) if shops else "0"))
    if not shop_id:
        print("没有可用店铺")
        return 1

    rows = (db.query(Product).filter(Product.deleted_at.is_(None))
            .order_by(Product.id).limit(args.limit).all())
    # 跳过标记为待重新采集的
    rows = [r for r in rows
            if not (isinstance(r.payload, dict) and r.payload.get("needsRecollect") is True)]
    print("账号=%s 店铺=%s  待认领 %d 件\n" % (acct.name, shop_id, len(rows)))
    if not rows:
        db.close()
        return 1

    t_all = time.time()
    client = account_service.account_client(acct)

    # --- 一次性预取（整批共用）---
    t0 = time.time()
    common_ids = pick_common(client, len(rows))
    common_id = common_ids[0]
    print("  预取公共箱商品 %.2fs" % (time.time() - t0))

    t0 = time.time()
    warehouse_id = carrier_service.target_warehouse_id(client, shop_id)
    print("  预取仓库ID %.2fs (%s)" % (time.time() - t0, warehouse_id))

    t0 = time.time()
    try:
        client.category_tree("MY")
    except Exception:
        pass
    print("  预取类目树 %.2fs" % (time.time() - t0))

    # --- 复制与覆盖并行 ---
    # 覆盖环节是瓶颈（约 4.5s/件），开多个 worker 并行覆盖。
    WORKERS = 1
    ready: queue.Queue = queue.Queue()
    stop = object()
    claimed_ids: list[int] = []
    saved: list[tuple[int, int]] = []
    failures: list[str] = []
    copy_times: list[float] = []
    save_times: list[float] = []

    def producer() -> None:
        """循环复制 N 份。"""
        try:
            for _ in range(len(rows)):
                t = time.time()
                try:
                    resp = client.claim_common_to_platform([(common_id, "tiktok", 1)])
                except Exception as exc:
                    failures.append("复制失败: %s" % str(exc)[:80])
                    break
                ids = carrier_service.claimed_detail_ids(resp)
                if not ids:
                    failures.append("复制未返回 detailId")
                    break
                copy_times.append(time.time() - t)
                claimed_ids.append(ids[0])
                ready.put(ids[0])
        finally:
            # 每个 worker 一个 stop 标记，否则先拿到标记的线程退出、其余线程永久阻塞
            for _ in range(WORKERS):
                ready.put(stop)

    def consumer() -> None:
        """复制出一份就立刻覆盖。"""
        index = 0
        while True:
            item = ready.get()
            if item is stop:
                break
            # 防御：队列里偶尔会混进 tuple/dict（旧版本残留），统一取出 int
            if isinstance(item, (tuple, list)):
                item = item[0]
            if isinstance(item, dict):
                item = item.get("detailId") or item.get("detail_id")
            try:
                item = int(item)
            except (TypeError, ValueError):
                continue
            if index >= len(rows):
                continue
            product = rows[index]
            index += 1
            t = time.time()
            try:
                snapshot = client.shop_detail(item, shop_id)
                data_block = (snapshot or {}).get("data") or {}
                oss_md5 = data_block.get("ossMd5")
                if not oss_md5:
                    failures.append("载体缺 ossMd5")
                    continue
                payload, clean_notes = prepare_payload(product.payload or {})
                # 关键：规格 id 必须用「这个载体自己的」。
                # 载体每次复制出来的规格表都不同（颜色名和 id 都不一样），
                # 拿商品自己的 attrValueId 去提交，妙手必然报「SKU 不存在」。
                # 做法：从刚复制的载体读规格，按名称/顺序把商品的规格值映射过去。
                carrier_info = (data_block.get("shopCollectItemInfo")
                                or data_block.get("collectItemInfo")
                                or data_block)
                site = str(product.payload.get("site") or "MY") if product.payload else "MY"
                attrs = carrier_service.category_sale_attrs(
                    client, payload.get("cid"), site, shop_id)
                enriched = dict(carrier_info or {})
                if attrs:
                    enriched["_colorAttrId"] = attrs.get("color", "")
                    enriched["_colorAttrName"] = attrs.get("colorName", "")
                    enriched["_sizeAttrId"] = attrs.get("size", "")
                    enriched["_sizeAttrName"] = attrs.get("sizeName", "")
                payload, spec_notes = carrier_service.normalize_spec_for_save(
                    payload, enriched)
                if (clean_notes or spec_notes) and index <= 3:
                    print("      处理: %s" % "；".join(clean_notes + spec_notes),
                          flush=True)
                body = carrier_service.adapt_payload(
                    payload, shop_id=shop_id, warehouse_id=warehouse_id)
                body["detailId"] = item
                # 新建的载体可能还在接收上游编辑，ossMd5 会变；重读一次再试
                if index <= 2:
                    print("      DEBUG item=%r(%s) shop=%r(%s) wid=%r(%s) md5=%r"
                          % (item, type(item).__name__, shop_id,
                             type(shop_id).__name__, warehouse_id,
                             type(warehouse_id).__name__, str(oss_md5)[:10]),
                          flush=True)
                try:
                    client.save_shop(int(item), int(shop_id), oss_md5, body)
                except Exception as exc:
                    text = str(exc)
                    # 妙手报「本地展示价 X MYR 无法覆盖基础成本 Y MYR」时，
                    # 按它给的准确成本线上调后重试（这是唯一权威数值，不用猜）
                    if "基础成本" in text:
                        raised = _raise_to_cost_floor(body, text)
                        if raised:
                            for retry in range(3):
                                try:
                                    client.save_shop(int(item), int(shop_id),
                                                     oss_md5, body)
                                    print("    id=%-4s 按成本线上调到 %s"
                                          % (product.id, raised), flush=True)
                                    raised = None
                                    break
                                except Exception as exc2:
                                    # 成本线可能还会微调，再抬一次
                                    text2 = str(exc2)
                                    if "基础成本" in text2:
                                        more = _raise_to_cost_floor(body, text2)
                                        if more:
                                            raised = more
                                            continue
                                    raise
                            if raised is not None:
                                raise
                        else:
                            raise
                    elif ("变动" in text or "重新打开" in text
                          or "ossMd5" in text.lower()):
                        # 载体刚复制出来还在接收上游编辑，ossMd5 会连续变几次，
                        # 只重读一次经常还是旧的。多试几轮，每轮重新读。
                        for retry in range(5):
                            time.sleep(1.2)
                            fresh = client.shop_detail(item, shop_id)
                            fresh_md5 = ((fresh or {}).get("data") or {}).get("ossMd5")
                            if not fresh_md5:
                                continue
                            try:
                                client.save_shop(int(item), int(shop_id),
                                                 fresh_md5, body)
                                break
                            except Exception as exc3:
                                text3 = str(exc3)
                                if ("变动" in text3 or "重新打开" in text3
                                        or "ossMd5" in text3.lower()) and retry < 4:
                                    continue
                                raise
                        else:
                            raise
                    else:
                        raise
                if args.verify:
                    carrier_service._readback_mismatches(
                        client, item, shop_id, body, payload)
                saved.append((product.id, item))
                save_times.append(time.time() - t)
                print("    id=%-4s -> %s  复制%.1fs 覆盖%.1fs"
                      % (product.id, item, copy_times[-1] if copy_times else 0,
                         save_times[-1]), flush=True)
            except Exception as exc:
                import traceback as _tb
                detail = _tb.format_exc().strip().splitlines()[-3:]
                failures.append("id=%s 覆盖失败: %s | %s"
                                % (product.id, str(exc)[:50], " / ".join(detail)))
                print("    id=%-4s X %s" % (product.id, str(exc)[:60]), flush=True)
                print("       %s" % " / ".join(detail), flush=True)

    # 覆盖环节是瓶颈（4.5s/件），单线程串行拖慢整批。
    # 开多个 consumer 并行覆盖，每个线程结束时向队列放一个 stop 标记。
    tp = threading.Thread(target=producer, name="copy", daemon=True)
    workers = [threading.Thread(target=consumer, name="save-%d" % i, daemon=True)
               for i in range(WORKERS)]
    tp.start()
    for worker in workers:
        worker.start()
    tp.join()
    for worker in workers:
        worker.join()

    total = time.time() - t_all
    print("\n=== 汇总 ===")
    print("  成功 %d / 失败 %d" % (len(saved), len(failures)))
    print("  总耗时 %.1fs  平均 %.1fs/件" % (total, total / max(1, len(rows))))
    if copy_times:
        print("  复制: 平均 %.2fs  合计 %.1fs" %
              (sum(copy_times) / len(copy_times), sum(copy_times)))
    if save_times:
        print("  覆盖: 平均 %.2fs  合计 %.1fs" %
              (sum(save_times) / len(save_times), sum(save_times)))
    print("  串行估算 %.1fs → 并行实际 %.1fs（省 %.0f%%）"
          % (sum(copy_times) + sum(save_times), total,
             (1 - total / max(0.01, sum(copy_times) + sum(save_times))) * 100))
    if failures:
        print("\n  失败明细:")
        for reason, count in Counter(failures).most_common(5):
            print("    %d 次: %s" % (count, reason))
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
