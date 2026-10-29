"""标题保存到妙手草稿（老版 title_editor.save 的核心安全逻辑）。

新版此前只有一个「只改本地库」的 PATCH——妙手侧标题不会变。要把标题真正
写进妙手，必须补上老版那五道闸门，少一道都会出事：

  1. **显式确认**：`confirm is True` 且非只读模式。写错标题是不可撤销的。
  2. **长度硬闸**：25–255 字符。妙手保存接口不接受范围外的标题，
     而且这个范围是平台文档写死的。
  3. **标题仍含待清理词 → 拒绝**：调用方必须先把清理做完再保存，
     否则等于把脏标题原样写回。
  4. **`reviewConfirmed`**：命中需复核词时，必须显式确认已人工核对过。
     不确认就保存，等于把需要人看的东西直接推到线上。
  5. **`_revision` 并发闸**：读到的快照与即将提交的 `ossMd5` 必须是同一版。
     两个人同时改同一件商品时，后提交的会覆盖前一个——而操作者并不知道。
     老版用 sha256(shopCollectItemInfo) 做 revision 比对来拦这个。

另外两件事老版做了，新版也得做：
  · 保存前把**完整快照备份**成 `title-before-<detailId>-<ts>.json`，
    出问题能还原；
  · 保存后**回读**确认标题真的变了（`verified`），不能只看 save 接口不报错。
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from core import errors
from core.config import get_settings

logger = logging.getLogger(__name__)

TITLE_MIN_LEN = 25
TITLE_MAX_LEN = 255
BACKUP_DIRNAME = "title-backups"


def revision_of(item: Mapping[str, Any]) -> str:
    """快照指纹：对商品详情主体做规范化哈希。

    键排序 + 不转义中文，保证同一份内容在不同时间算出同一个值。
    """
    content = json.dumps(item, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _backup(snapshot: Mapping[str, Any], detail_id: Any) -> str:
    """保存前落一份完整快照，返回文件名（不返回绝对路径，避免泄露目录结构）。"""
    directory = get_settings().data_dir / BACKUP_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    name = f"title-before-{detail_id}-{time.time_ns()}.json"
    path = directory / name
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return name


def save_title(db: Session, *, account_id: Any, detail_id: Any, shop_id: Any,
               title: str, confirm: bool = False, review_confirmed: bool = False,
               expected_revision: str = "",
               rules: Mapping[str, str] | None = None) -> dict[str, Any]:
    """把标题写回妙手草稿并回读确认。"""
    from services import account_service, title_service

    wanted = str(title or "").strip()
    if not confirm:
        raise errors.validation("保存标题需要显式确认", field="confirm")
    if not TITLE_MIN_LEN <= len(wanted) <= TITLE_MAX_LEN:
        raise errors.AppError(
            errors.VALIDATION_ERROR,
            f"标题须为 {TITLE_MIN_LEN} 到 {TITLE_MAX_LEN} 字符，当前 {len(wanted)}",
            status=400, details={"field": "title", "length": len(wanted)})

    # 待清理词必须先清掉：把脏标题写回去等于白做一次清理。
    # 用词库判定，口径与 clean_title 一致（NFKC + 空格/连字符变体）。
    from services import word_service

    pairs = (list(word_service.matcher(db)) if rules is None
             else [(str(word).casefold(), action)
                   for word, action in rules.items()])
    hits_remove = [word for word, action in word_service.matched_words(wanted, pairs)
                   if action == "remove"]
    review_words = title_service.review_hits(wanted, pairs)
    if hits_remove:
        raise errors.AppError(
            errors.VALIDATION_ERROR,
            "标题仍含待清理词（" + "、".join(hits_remove) + "），"
            "请先自动清理并核对结果",
            status=400,
            details={"field": "title", "removedWords": hits_remove})
    if review_words and not review_confirmed:
        raise errors.AppError(
            errors.VALIDATION_ERROR,
            "请先确认已核查复核词及商品内容：" + "、".join(review_words),
            status=400,
            details={"field": "reviewConfirmed", "reviewWords": review_words})

    account, client = account_service.client_for(db, account_id)
    try:
        snapshot = client.shop_detail(detail_id, shop_id)
        block = (snapshot.get("data") or {})
        item = block.get("shopCollectItemInfo") or {}
        # 身份与版本字段必须齐全，否则后面拿什么做并发闸都不成立
        if not block.get("ossMd5"):
            raise errors.AppError(
                errors.UPSTREAM_ERROR, "妙手返回的商品详情缺少版本号，已停止保存",
                status=502)
        if (str(item.get("detailId")) != str(detail_id)
                or str(item.get("shopId")) != str(shop_id)):
            raise errors.AppError(
                errors.UPSTREAM_ERROR,
                "妙手返回的商品详情与请求的不是同一件，已停止保存", status=502)

        current = revision_of(item)
        if expected_revision and expected_revision != current:
            raise errors.AppError(
                errors.VALIDATION_ERROR,
                "商品在你编辑期间已被改动（版本不一致），请刷新后重试",
                status=409,
                details={"expectedRevision": expected_revision, "currentRevision": current})
        if str(item.get("title") or "") == wanted:
            return {"verified": True, "message": "标题没有变化，无需保存",
                    "revision": current}

        payload = dict(item)
        payload["title"] = wanted
        # 读接口不返回这个字段，但保存接口要求它有值且只接受 default
        if not payload.get("deliveryOptionSetType"):
            payload["deliveryOptionSetType"] = "default"

        backup_name = _backup(snapshot, detail_id)
        logger.info("保存标题 detailId=%s 备份=%s 改动=「%s」→「%s」",
                    detail_id, backup_name, item.get("title"), wanted)
        try:
            client.save_shop(detail_id, shop_id, block["ossMd5"], payload)
        except Exception as exc:  # noqa: BLE001 - 结果可能未知
            logger.exception("保存标题失败或结果未知 detailId=%s", detail_id)
            raise errors.AppError(
                errors.UPSTREAM_UNKNOWN_RESULT,
                f"保存请求结果未确认（{type(exc).__name__}），请刷新商品核对，"
                "不要直接重试",
                status=502, details={"backup": backup_name}) from None

        # 回读确认：只看 save 接口不报错是不够的
        try:
            fresh = client.shop_detail(detail_id, shop_id)
            fresh_item = ((fresh.get("data") or {}).get("shopCollectItemInfo") or {})
            verified = str(fresh_item.get("title") or "") == wanted
        except Exception:  # noqa: BLE001 - 回读失败不影响「已提交」
            return {"verified": False, "backup": backup_name,
                    "message": "保存请求已提交，但回读未确认，请刷新商品核对结果"}
        if verified:
            return {"verified": True, "backup": backup_name,
                    "message": "标题已保存并回读确认",
                    "before": item.get("title"), "after": wanted}
        return {"verified": False, "backup": backup_name,
                "message": "保存请求已提交，回读标题暂未一致，请刷新后核对"}
    finally:
        client.close()
