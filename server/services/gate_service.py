"""保存前闸门：标题 review 词 / 主图 OCR 复核（老版两道硬阻断的移植）。

为什么需要这个模块：老版把这两件事做成**保存前的硬阻断**——
`shoe_workflow.prepare_shoe` 抛 "Title requires review before automated
processing"、`product_workflow.prepare_product` 抛「标题清理后仍需要人工复核，
已停止保存」、`shoe_workflow.review_main_images` 抛 "Image classification needs
review; no save performed"。新版只剩「只记录不拦」：`transform_service` 只往
report 里写 needsReview / reviewWords，`warehouse_service.update` 对 review 词
只提示，`save_from_collect_box` 两个都不看。

后果是闸门装错了地方：回写妙手那条路有 `reviewConfirmed` 硬闸
（title_save_service），入仓这条路一道都没有——含需复核词的标题、带中文或
厂家宣传语的主图可以直接进库。

本模块只回答「这件现在能不能过」，**不写任何数据**。阻断还是放行由调用方用
`allow_*` 决定，判定逻辑只有这一份，不会出现「回写有闸、入仓没闸」的分裂。

为什么不把判定塞进 `warehouse_service`：那个文件是入仓主流程，改它等于把闸门
和落库揉在一起，预览 / 批量 / 重试三条路都会被动到。闸门先独立成一层，
调用方在写库前问一次即可。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core import errors
from core.constants import MAIN_IMAGE_LIMIT
from core.utils import product_key
from models.product import Product
from services import image_review_service, product_service, transform_service

# 三条消息照抄老版措辞：用户和客服都认这几句话，换一种说法等于让排障重新开始。
TITLE_REVIEW_MESSAGE = "标题清理后仍需要人工复核，已停止保存"
IMAGE_REVIEW_MESSAGE = "主图分类需要人工复核，已停止保存"
IMAGE_ALL_REMOVED_MESSAGE = "清理后主图会全部被删掉，已停止保存"


def _as_item(info: Any) -> Mapping[str, Any]:
    """闸门只读 info，不接受 None：给它一个空映射比到处判 None 干净。"""
    return info if isinstance(info, Mapping) else {}


def _library(db: Any) -> dict[str, Any]:
    """取词库的 remove / review 两组原词。

    db 允许为 None：闸门在纯函数场景（单测、只读预览）也要能跑。
    那时自己开一个短会话用完就关——不要在模块级持有 Session，
    否则一次请求漏关就会把 SQLite 的写锁带到下一个人身上。
    """
    from services import word_service

    if db is not None:
        return word_service.library(db)
    from db.session import session_scope

    with session_scope() as session:
        return word_service.library(session)


def title_review_gate(info: Any, *, db: Any = None,
                      allow_review: bool = False) -> dict[str, Any]:
    """标题 review 词闸门。

    判定复用 `transform_service.transform_item` 的清理逻辑，不另写一套：
    老版抛错的就是它算出来的 `report["titleCheck"]["needsReview"]`，
    两边各写一份匹配规则迟早会出现「预览说有、入仓说没有」的分歧。

    返回 {blocked, reviewWords, message, cleanedTitle, removedWords,
          needsReview, warnings}

    注意 `needsReview` 与 `blocked` 不是一回事：前者还包含「清理后标题长度不在
    25–255」这一项，而长度由标题保存的硬闸（title_save_service）和 pre_save_check
    自己管。这里只按 review 词阻断，长度问题进 warnings，不重复拦。
    """
    item = _as_item(info)
    library = _library(db)
    _result, report = transform_service.transform_item(item, title_words=library)
    check = report.get("titleCheck") or {}
    review_words = [str(word) for word in (check.get("reviewWords") or []) if str(word)]
    warnings = [str(text) for text in (report.get("warnings") or [])]
    # 放行时消息留空：调用方拿 message 当「为什么被拦」用，放行还带一句话会误导
    blocked = bool(review_words) and not allow_review
    return {
        "blocked": blocked,
        "reviewWords": review_words,
        "message": TITLE_REVIEW_MESSAGE if blocked else "",
        "reason": "review" if blocked else "",
        "cleanedTitle": str(check.get("after") or ""),
        "removedWords": [str(word) for word in (check.get("removedWords") or [])],
        "needsReview": bool(check.get("needsReview")),
        "warnings": warnings,
    }


def main_image_gate(info: Any, *, allow_review: bool = False,
                    limit: int = MAIN_IMAGE_LIMIT,
                    retry: bool = False) -> dict[str, Any]:
    """主图 OCR 闸门（老版 `review_main_images`）。

    返回 {blocked, removeIndexes, needsReview, message, reason, imageCount,
          keptCount, rows, warnings}

    取图口径照抄 `product_service.main_images`：只认 http(s)、去重、截到 9 张。
    老版直接用 `item["imgUrls"]` 原数组，那样空串 / 非 URL 元素会让
    `len(rows) != min(len(images), 9)` 恒成立，整批商品都会被判「需复核」。
    """
    item = _as_item(info)
    images = product_service.main_images(item, limit=limit)
    expected = min(len(images), limit)
    if not images:
        # 没有主图不是这道闸门的事（pre_save_check 规则 5 已经拦了），
        # 这里再报一次「全部被删掉」只会让人以为图被 OCR 删没了。
        return {"blocked": False, "removeIndexes": [], "needsReview": [],
                "message": "", "reason": "", "imageCount": 0, "keptCount": 0,
                "rows": [],
                "warnings": ["商品没有可识别的主图（只认 http/https 地址），主图闸门跳过"]}

    rows = image_review_service.analyze(images, limit=limit, retry=retry)

    # 老版口径：行数对不上、任一行 error、任一行 action == "review" 都算分类不完整。
    # 行数对不上通常是 OCR 中途挂了或上游图取不到，那种结果不能当「没问题」。
    needs_review: list[dict[str, Any]] = []
    remove_indexes: list[int] = []
    for row in rows:
        action = str(row.get("action") or "review")
        index = int(row.get("index") or 0)
        if action == "remove":
            remove_indexes.append(index)
        # action 缺失按 review 处理，与 image_review_service.summarize 的口径一致
        if row.get("error") or action == "review":
            needs_review.append({
                "index": index,
                "url": str(row.get("url") or ""),
                "action": action,
                "reason": str(row.get("reason") or ""),
            })

    incomplete = len(rows) != expected
    warnings: list[str] = []
    if incomplete:
        warnings.append(f"主图识别只返回 {len(rows)} 行，应为 {expected} 行")
    for row in needs_review:
        warnings.append(f"第 {row['index']} 张主图待复核：{row['reason'] or row['action']}")

    removed = set(remove_indexes)
    kept = [url for index, url in enumerate(images, start=1) if index not in removed]

    if incomplete or needs_review:
        blocked = not allow_review
        message = IMAGE_REVIEW_MESSAGE if blocked else ""
        reason = "review" if blocked else ""
    elif not kept:
        # 「全部被删掉」不吃 allow_review：人工确认「这些复核项我看过了」不等于
        # 同意把主图清空。老版这里是无条件抛错，保持同样强度。
        blocked = True
        message = IMAGE_ALL_REMOVED_MESSAGE
        reason = "allRemoved"
    else:
        blocked, message, reason = False, "", ""

    return {
        "blocked": blocked,
        "removeIndexes": remove_indexes,
        "needsReview": needs_review,
        "message": message,
        "reason": reason,
        "imageCount": len(images),
        "keptCount": len(kept),
        "rows": rows,
        "warnings": warnings,
    }


def pre_save_gate(db: Any, info: Any, *, allow_title_review: bool = False,
                  allow_image_review: bool = False, skip_images: bool = False,
                  retry_ocr: bool = False) -> dict[str, Any]:
    """合并两道闸门。

    返回 {ok, blocks: [{gate, message}], warnings: [...], title, images}

    blocks 只收真正阻断的项，调用方拿它决定「拒绝入仓」；warnings 收「放行了
    但你该知道」的项（比如 allow_* 放行后仍然存在的复核词、OCR 漏行）。
    两者分开是因为前端要能区分「红」和「黄」。
    """
    title = title_review_gate(info, db=db, allow_review=allow_title_review)
    if skip_images:
        images = {"blocked": False, "removeIndexes": [], "needsReview": [],
                  "message": "", "reason": "", "imageCount": 0, "keptCount": 0,
                  "rows": [], "warnings": [], "skipped": True}
    else:
        images = main_image_gate(info, allow_review=allow_image_review, retry=retry_ocr)

    blocks: list[dict[str, Any]] = []
    warnings: list[str] = []
    if title["blocked"]:
        blocks.append({"gate": "titleReview", "message": title["message"]})
    elif title["reviewWords"]:
        # 放行也要说清楚放了什么：这批商品是带着复核词进库的
        warnings.append("标题命中需复核词（已确认放行）：" + "、".join(title["reviewWords"]))
    if images["blocked"]:
        blocks.append({"gate": "imageReview" if images["reason"] != "allRemoved"
                       else "imageCleanup", "message": images["message"]})
    elif images["needsReview"]:
        warnings.append("主图 OCR 待复核（已确认放行）：第 "
                        + "、".join(str(row["index"]) for row in images["needsReview"])
                        + " 张")

    # transform_service 自己产出的告警已经是完整句子，有的还以「标题」开头；
    # 一律加前缀会得到「标题：标题命中需复核词」，看着像程序拼错了。
    def _tag(prefix: str, text: Any) -> str:
        body = str(text)
        return body if body.startswith(prefix) else f"{prefix}：{body}"

    warnings.extend(_tag("标题", text) for text in title.get("warnings") or [])
    warnings.extend(_tag("主图", text) for text in images.get("warnings") or [])

    return {"ok": not blocks, "blocks": blocks, "warnings": warnings,
            "title": title, "images": images}


def load_info(db: Any, *, warehouse_id: Any = None, detail_id: Any = None,
              shop_id: Any = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """取一件商品的 info（闸门的输入），返回 (info, identity)。

    先查仓库：已入仓的商品不必再调上游，一次闸门检查不该产生网络请求。
    仓库里没有才按 detailId + shopId 去妙手拉——那正是前端最需要它的场景
    （入仓前先问一次「这件能不能过」，此时商品还不在库里）。
    """
    ident = str(warehouse_id or "").strip()
    if ident:
        from services import warehouse_service

        product = warehouse_service.resolve_product(db, ident)
        payload = product.payload if isinstance(product.payload, dict) else {}
        if not payload:
            # 老库 / 手工建的行可能只有展示字段，用它们拼一份够闸门用的最小 info
            payload = {"title": str(product.title or ""),
                       "imgUrls": list(product.images or [])}
        return dict(payload), {"source": "warehouse", "warehouseId": str(product.id),
                               "detailId": str(product.detail_id or ""),
                               "shopId": str(product.shop_id or "")}

    detail = str(detail_id or "").strip()
    shop = str(shop_id or "").strip()
    if not detail or not shop:
        raise errors.validation(
            "请给出 warehouseId，或同时给出 detailId 与 shopId", field="warehouseId")

    key = product_key(detail, shop)
    row = (db.query(Product)
           .filter(Product.warehouse_key == key, Product.deleted_at.is_(None))
           .one_or_none())
    if row is not None and isinstance(row.payload, dict) and row.payload:
        return dict(row.payload), {"source": "warehouse", "warehouseId": str(row.id),
                                   "detailId": detail, "shopId": shop}

    from services import account_service
    from services.miaoshou import MiaoshouError

    account, client = account_service.client_for(db)
    try:
        response = client.shop_detail(detail, shop)
    except MiaoshouError as exc:
        raise account_service.map_upstream_error(exc) from None
    finally:
        client.close()
    info = product_service.detail_block(response)["info"]
    if not info:
        raise errors.not_found(f"上游查无明细 {detail}", detailId=detail, shopId=shop)
    return dict(info), {"source": "upstream", "warehouseId": "",
                        "detailId": detail, "shopId": shop,
                        "accountId": str(account.id)}
