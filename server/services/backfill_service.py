"""回填仓库行的账号归属（老版 backfill_warehouse_accounts.py 的移植）。

老版这份工具存在的原因：早期版本的仓库条目没记 `account`，而**认领商品必须
经过账号**——归属错了，下一次操作就发到别人的连接上。所以它坚持「不猜」：
店铺 id 必须能对上某个已保存连接的店铺清单，对不上就报出来、原样留着。

新版有 SQLite 和 accounts 表，映射来源从「读连接文件」变成「读 accounts.shops」，
判定规则一致：
  · 一个店铺只属于一个账号 → 回填
  · 同一店铺被多个账号声明 → 判冲突，不动（随便挑一个是错的）
  · 谁都对不上 → 报出来，不动

默认只出报告，`apply=True` 才写库。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from models.account import Account
from models.product import Product

logger = logging.getLogger(__name__)


def shop_map(db: Session) -> tuple[dict[str, int], dict[str, str], list[dict[str, Any]]]:
    """建立 shopId -> accountId 映射。

    返回 (映射, 店铺名, 冲突清单)。被多个账号声明的店铺从映射里剔除并报告。
    """
    mapping: dict[str, int] = {}
    names: dict[str, str] = {}
    conflicts: dict[str, set[int]] = {}
    problems: list[dict[str, Any]] = []

    for account in db.query(Account).all():
        raw = account.shops
        shops: Any = raw
        if isinstance(raw, str):
            try:
                shops = json.loads(raw) if raw else []
            except ValueError:
                problems.append({"accountId": account.id, "accountName": account.name,
                                 "message": "店铺清单不是有效 JSON，已跳过"})
                continue
        if not isinstance(shops, list):
            continue
        for shop in shops:
            if not isinstance(shop, dict):
                continue
            shop_id = str(shop.get("shopId") or "").strip()
            if not shop_id:
                continue
            names.setdefault(shop_id, str(shop.get("name") or ""))
            if shop_id in mapping and mapping[shop_id] != account.id:
                conflicts.setdefault(shop_id, {mapping[shop_id]}).add(account.id)
            else:
                mapping.setdefault(shop_id, account.id)

    for shop_id, accounts in conflicts.items():
        mapping.pop(shop_id, None)
        problems.append({"shopId": shop_id, "accountIds": sorted(accounts),
                         "message": "这个店铺被多个账号声明，无法判定归属，已跳过"})
    return mapping, names, problems


def survey(db: Session) -> dict[str, Any]:
    """只统计不回填：给出「能修多少 / 修不了多少」。"""
    mapping, names, problems = shop_map(db)
    rows = (db.query(Product.shop_id, func.count(Product.id))
            .filter(Product.account_id.is_(None))
            .group_by(Product.shop_id).all())
    fixable = 0
    unknown: list[dict[str, Any]] = []
    for shop_id, count in rows:
        key = str(shop_id or "")
        if key in mapping:
            fixable += count
        else:
            unknown.append({"shopId": key, "count": count,
                            "shopName": names.get(key, "")})
    return {
        "missingAccount": sum(count for _shop, count in rows),
        "fixable": fixable,
        "unknownShops": unknown,
        "conflicts": problems,
        "mappedShops": dict(mapping.items()),
    }


def apply(db: Session, *, dry_run: bool = True) -> dict[str, Any]:
    """按映射回填；dry_run=True 时只统计不写库。"""
    report = survey(db)
    mapping = report.pop("mappedShops")
    updated = 0
    if not dry_run:
        for shop_id, account_id in mapping.items():
            # 只补空的：已经有账号的行不动，避免覆盖人工修正过的归属
            count = (db.query(Product)
                     .filter(Product.account_id.is_(None),
                             Product.shop_id == shop_id)
                     .update({Product.account_id: account_id},
                             synchronize_session=False))
            updated += int(count or 0)
        db.flush()
        logger.info("回填仓库账号：更新 %s 行", updated)
    return {**report, "dryRun": dry_run, "updated": updated,
            "shopCount": len(mapping)}
