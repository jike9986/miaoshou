"""商品标准校验器 + 自动修复（对应 docs/PRODUCT_STANDARD.md）。

用途：
  1. validate(payload)        逐条检查，返回 (ok, issues, fixable)
  2. repair(payload, ctx)     按标准自动修可修项，返回修复报告
  3. compare(old, new)        重新采集后，对比新旧数据，决定用哪份

设计原则：
  · 不猜。缺关键信息（如上游已删、无规格）就标 needsRecollect，绝不编造。
  · 可修项必须能给出明确依据（价格+2MYR、图片裁到9张、品类尺寸基准）。
  · 每次修复都记录做了什么，便于追溯。

用法：
    from services.product_standard import validate, repair, compare
"""
from __future__ import annotations

import re
from decimal import Decimal, ROUND_HALF_UP
from html import unescape
from statistics import median
from typing import Any, Mapping

# --- 标准常量 -------------------------------------------------------------
PRICE_BUMP_MYR = Decimal("2.00")      # 负责人定：成本价 + 2 马币
EXCHANGE_RATE = Decimal("1.633")
RATE_MIN = Decimal("1.2")
RATE_MAX = Decimal("2.2")

MAX_MAIN_IMAGES = 9
MAX_DETAIL_IMAGES = 9
MAX_ATTR_VALUE_LEN = 60
MAX_ITEM_NUM_LEN = 50
TITLE_MIN_LEN = 4
TITLE_MAX_LEN = 255
WEIGHT_MIN = 0.4
FREIGHT_LIGHT = Decimal("10.62")
FREIGHT_HEAVY = Decimal("20.00")

PROP_ID_COLOR = "1000000001"
PROP_ID_SIZE = "1000000002"

# 品类 → (长, 宽, 高) cm, 重量上限 kg
# 负责人 2026-11-05 明确：轮滑鞋这类本身就在 2kg 以内，是正常重量，
# 不能按一般鞋类 1.2kg 压。重量上限按品类实际装箱重量给，不是统一压。
CATEGORY_SPECS: dict[str, tuple[tuple[int, int, int], float]] = {
    "运动鞋": ((33, 22, 12), 1.2),
    "运动拖鞋": ((33, 22, 12), 1.2),
    "轮滑鞋与溜冰鞋": ((35, 24, 14), 2.0),      # 轮滑鞋/溜冰鞋：整双含轮架约 1.6~2.0kg
    "暴走鞋": ((35, 24, 14), 2.0),               # 暴走鞋带轮装置，重量偏重属正常
    "运动服饰": ((30, 24, 6), 1.2),
    "海外仓定制款": ((33, 22, 12), 1.2),
}
DEFAULT_DIMS = (33, 22, 12)
DEFAULT_CAP = 1.2

SIZE_HINTS = {
    "34", "35", "36", "37", "38", "39", "40", "41", "42", "43", "44", "45",
    "46", "47", "48", "XS", "S", "M", "L", "XL", "XXL", "XXXL", "2XL", "3XL",
    "FREE", "F",
}
JUNK_TITLES = {"a", "test", "null", "none", "n/a", "-", "--", "无", "无标题"}
URL_RE = re.compile(r"https?://[^\s\"'<>\\]+", re.I)
CJK_RE = re.compile(r"[\u4e00-\u9fa5]")


def _num(value: Any) -> float | None:
    try:
        number = float(value)
        return number if number > 0 else None
    except (TypeError, ValueError):
        return None


def _dec(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
        return number if number > 0 else None
    except Exception:
        return None


def _looks_like_size(text: str) -> bool:
    value = str(text or "").strip().upper()
    if not value:
        return False
    return value in SIZE_HINTS or (any(c.isdigit() for c in value) and len(value) <= 6)


def detail_image_urls(notes: Any) -> list[str]:
    return URL_RE.findall(str(notes or ""))


def detail_image_unique(notes: Any) -> list[str]:
    """详情图 URL 去重。

    有些 notes 里同一个 URL 会被正则切出多段（1688 图床地址里含 `!!391898001`
    这类片段，或相邻标签粘连），直接按匹配个数判断会误报「17 张超限」，
    实际只有 9 张。判超限必须用去重后的数量。
    """
    return list(dict.fromkeys(detail_image_urls(notes)))


def _clean_notes_html(notes: str) -> str:
    """清理描述 HTML：去掉空段与嵌套 <p>，只保留图片序列。

    1688 采集来的 notes 常见形态：
        <p><p></p><p>        </p><img ...><img ...></p>
    嵌套 <p> 与空白段会让富文本编辑器解析异常（图不显示、冒出空白）。
    这里统一拍平成 <img> 序列，不增删图片。
    """
    text = str(notes or "")
    if not text:
        return text
    # 1. 去掉纯空白/空段落
    text = re.sub(r"<p>(?:\s|&nbsp;|<br\s*/?>)*</p>", "", text, flags=re.I)
    # 2. 把「只含图片的 <p>」拍平成裸 img
    text = re.sub(
        r"<p>((?:\s|&nbsp;)*(?:<img[^>]*>(?:\s|&nbsp;)*)+)</p>",
        lambda m: "".join(re.findall(r"<img[^>]*>", m.group(1), flags=re.I)),
        text, flags=re.I,
    )
    # 3. 连续空行/空白压缩
    text = re.sub(r"(\s|&nbsp;){2,}", " ", text)
    return text.strip()


def _images_html(urls: list[str]) -> str:
    """把图片 URL 拼成描述 HTML（只有图，没有文字）。"""
    return "".join('<p><img src="%s" /></p>' % str(u) for u in urls if str(u).strip())


def category_spec(category_name: str) -> tuple[tuple[int, int, int], float]:
    return CATEGORY_SPECS.get(str(category_name or "").strip(), (DEFAULT_DIMS, DEFAULT_CAP))


# --- 校验 -----------------------------------------------------------------
def validate(payload: Mapping[str, Any], *, category_name: str = "",
             language: str = "") -> dict[str, Any]:
    """逐条检查，返回 {ok, issues, fixable, blocked, needsRecollect}。

    issues     : 所有问题描述
    fixable    : 能按标准自动修的项（repair 会处理）
    blocked    : 数据自相矛盾、无法自动修
    needsRecollect : 必须重新采集
    """
    issues: list[str] = []
    fixable: list[str] = []
    blocked: list[str] = []
    need_recollect: list[str] = []

    data = dict(payload or {})

    # 1. 标题
    title = str(data.get("title") or "").strip()
    if not title:
        issues.append("标题为空")
        need_recollect.append("标题为空")
    elif len(title) < TITLE_MIN_LEN:
        issues.append("标题过短(%r)" % title)
        fixable.append("标题过短")
    elif title.casefold() in JUNK_TITLES:
        issues.append("标题是占位值(%r)" % title)
        fixable.append("标题占位")
    if len(title) > TITLE_MAX_LEN:
        issues.append("标题超长(%d)" % len(title))
        fixable.append("标题超长")
    lang = str(language or data.get("language") or "").lower()
    if lang in ("ms", "en", "vi", "id", "th") and CJK_RE.search(title):
        issues.append("外语商品标题含中文")
        fixable.append("标题含中文")

    # 2. 图片
    main_imgs = [u for u in (data.get("imgUrls") or []) if str(u).startswith("http")]
    if not main_imgs:
        issues.append("无主图")
        need_recollect.append("无主图")
    elif len(main_imgs) > MAX_MAIN_IMAGES:
        issues.append("主图%d张超限" % len(main_imgs))
        fixable.append("主图超限")
    if len(set(main_imgs)) != len(main_imgs):
        issues.append("主图有重复")
        fixable.append("主图重复")

    det_imgs = detail_image_unique(data.get("notes"))
    if len(det_imgs) > MAX_DETAIL_IMAGES:
        issues.append("详情图%d张超限" % len(det_imgs))
        fixable.append("详情图超限")

    # 3. 类目
    if not str(data.get("cid") or "").strip():
        issues.append("缺类目")
        need_recollect.append("缺类目")

    # 4. 价格
    sku_map = data.get("skuMap") or {}
    active_skus = [v for v in sku_map.values()
                   if isinstance(v, dict) and str(v.get("isDelete", "0")) != "1"] \
        if isinstance(sku_map, dict) else []
    if not active_skus:
        issues.append("无有效SKU")
        need_recollect.append("无有效SKU")
    else:
        price_low = 0
        for sku in active_skus:
            origin = _dec(sku.get("originPrice"))
            price = _dec(sku.get("price"))
            vat = _dec(sku.get("priceIncludeVat"))
            if origin is None and price is None:
                price_low += 1
                continue
            base = None
            if origin is not None:
                base = (origin / EXCHANGE_RATE).quantize(Decimal("0.01"),
                                                         rounding=ROUND_HALF_UP)
            elif price is not None:
                base = price
            if base is None:
                continue
            target = (base + PRICE_BUMP_MYR).quantize(Decimal("0.01"),
                                                      rounding=ROUND_HALF_UP)
            if vat is None or vat < target:
                price_low += 1
        if price_low:
            issues.append("售价低于标准(%d条SKU)" % price_low)
            fixable.append("价格偏低")

    # 5. 包裹尺寸
    dims = [_num(data.get(k)) for k in
            ("packageLength", "packageWidth", "packageHeight")]
    if any(d is None for d in dims):
        issues.append("缺包裹尺寸")
        fixable.append("缺尺寸")
    elif max(dims) > 80 or sum(dims) > 150:
        issues.append("尺寸不合理(%s)" % dims)
        fixable.append("尺寸异常")

    # 6. 重量
    weight = _num(data.get("weight"))
    _, cap = category_spec(category_name)
    if weight is None:
        # 看 SKU 有没有真实重量
        sku_weights = [_num(s.get("weight")) for s in active_skus]
        sku_weights = [w for w in sku_weights if w]
        if sku_weights:
            issues.append("缺重量(可从SKU补)")
            fixable.append("缺重量可补")
        else:
            issues.append("缺重量且无依据")
            fixable.append("缺重量需估算")
    elif weight < WEIGHT_MIN:
        issues.append("重量过轻(%.2f)" % weight)
        fixable.append("重量过轻")
    elif weight > cap:
        issues.append("重量超品类上限(%.2f>%.2f)" % (weight, cap))
        fixable.append("重量超限")

    # 7. 规格
    props = [p for p in (data.get("skuPropertyList") or []) if isinstance(p, dict)]
    if not props:
        issues.append("无规格信息(skuPropertyList)")
        need_recollect.append("无规格信息")
    else:
        known_ids: set[str] = set()
        for dim, prop in enumerate(props):
            if not str(prop.get("propName") or "").strip():
                issues.append("维度%d缺propName" % dim)
                fixable.append("缺propName")
            if not str(prop.get("propId") or "").strip():
                issues.append("维度%d缺propId" % dim)
                fixable.append("缺propId")
            values = [av for av in (prop.get("attrValueList") or [])
                      if isinstance(av, dict)]
            if not values:
                issues.append("维度%d无属性值" % dim)
                need_recollect.append("维度%d无属性值" % dim)
                continue
            seen: dict[str, int] = {}
            for av in values:
                label = str(av.get("attrValue") or "").strip()
                vid = str(av.get("attrValueId") or "").strip()
                if vid:
                    known_ids.add(vid)
                if not label:
                    issues.append("维度%d有空属性值" % dim)
                    fixable.append("空属性值")
                    continue
                if len(label) > MAX_ATTR_VALUE_LEN:
                    issues.append("属性值超长(%d)" % len(label))
                    fixable.append("属性值超长")
                key = label.casefold()
                seen[key] = seen.get(key, 0) + 1
            dup = [k for k, c in seen.items() if c > 1]
            if dup:
                issues.append("维度%d属性值重复%s" % (dim, dup[:2]))
                fixable.append("属性值重复")
            # 颜色必须有图
            name = str(prop.get("propName") or "")
            if name not in ("尺码", "尺寸"):
                for av in values:
                    if not (av.get("imageUrl") or (av.get("imgUrls") or [])):
                        issues.append("维度%d颜色值缺图" % dim)
                        fixable.append("颜色缺图")
                        break

        # skuMap 引用完整性
        if isinstance(sku_map, dict) and sku_map:
            # 每个维度各自的 id 集合（校验「段位置是否正确」）
            dim_ids: list[set[str]] = []
            for prop in props:
                ids = set()
                for av in (prop.get("attrValueList") or []):
                    if isinstance(av, dict):
                        vid = str(av.get("attrValueId") or "").strip()
                        if vid:
                            ids.add(vid)
                dim_ids.append(ids)

            referenced: set[str] = set()
            pos_errors = 0
            for key in sku_map.keys():
                parts = [x.strip() for x in str(key).split(";") if x.strip()]
                for part in parts:
                    referenced.add(part)
                # 段数必须等于维度数
                if len(parts) != len(dim_ids):
                    pos_errors += 1
                    continue
                # 每段的 id 必须属于对应维度
                for pos, part in enumerate(parts):
                    if part not in dim_ids[pos]:
                        pos_errors += 1
                        break
            if pos_errors:
                issues.append("skuMap 有%d条key的段位置与规格维度错位" % pos_errors)
                blocked.append("skuMap段位置错位")

            missing = referenced - known_ids
            if missing:
                issues.append("skuMap引用不存在的属性值%s" % sorted(missing)[:3])
                blocked.append("skuMap引用缺失")
        # itemNum 长度
        for sku in active_skus:
            item_num = sku.get("itemNum")
            if item_num is not None and len(str(item_num)) > MAX_ITEM_NUM_LEN:
                issues.append("itemNum超长(%d)" % len(str(item_num)))
                fixable.append("itemNum超长")
                break

    # 8. 其他
    cod = data.get("isCodOpen")
    if not (isinstance(cod, str) and cod in ("0", "1")):
        issues.append("isCodOpen不是字符串'0'/'1'(%r)" % (cod,))
        fixable.append("COD格式")

    if not str(data.get("notes") or "").strip():
        issues.append("缺描述")
        fixable.append("缺描述")
    else:
        # 描述页只放图片（负责人 2026-11-05 定）：
        #   · 有文字说明 → 报「描述含文字」
        #   · 有嵌套空段 → 报「描述结构需清理」
        notes_text = str(data.get("notes") or "")
        visible = re.sub(r"<[^>]*>", "", notes_text)
        visible = unescape(visible).replace("&nbsp;", " ").strip()
        if visible:
            issues.append("描述含文字(%r)" % visible[:20])
            fixable.append("描述含文字")
        if _clean_notes_html(notes_text) != notes_text:
            issues.append("描述结构需清理")
            fixable.append("描述结构需清理")
        if not detail_image_urls(notes_text):
            issues.append("描述无图片")
            fixable.append("描述无图片")

    return {
        "ok": not issues,
        "issues": issues,
        "fixable": fixable,
        "blocked": blocked,
        "needsRecollect": need_recollect,
    }


# --- 自动修复 -------------------------------------------------------------
def _title_is_mostly_localized(title: str) -> bool:
    """标题是否已经本地化（拉丁字母为主），只是混进了中文。

    全中文标题返回 False —— 那是待翻译的中文标题，不是「混入中文的译文」，
    不能把中文删掉。只有拉丁字母明显多于中文时才清理。
    """
    cjk = len(CJK_RE.findall(title or ""))
    latin = len(re.findall(r"[A-Za-z]", title or ""))
    return bool(latin) and latin > cjk


def repair(payload: Mapping[str, Any], *, category_name: str = "",
           language: str = "") -> tuple[dict[str, Any], list[str]]:
    """按标准修复可修项，返回 (新payload, 修复记录)。"""
    data = dict(payload or {})
    done: list[str] = []
    dims_spec, cap = category_spec(category_name)
    main_imgs = [u for u in (data.get("imgUrls") or []) if str(u).startswith("http")]

    # 标题
    title = str(data.get("title") or "").strip()
    if title and len(title) > TITLE_MAX_LEN:
        data["title"] = title[:TITLE_MAX_LEN]
        done.append("标题截断")
    elif title and title.casefold() in JUNK_TITLES:
        # 不猜真标题，只标记
        data["needsRecollect"] = True
        data["needsRecollectReason"] = "标题是占位值(%r)" % title
        done.append("标题占位→标记重采")
    lang = str(language or data.get("language") or "").lower()
    raw_title = str(data.get("title") or "")
    if (lang in ("ms", "en", "vi", "id", "th")
            and CJK_RE.search(raw_title)
            and _title_is_mostly_localized(raw_title)):
        # 只清「本地化标题里混进来的中文」。
        #
        # 为什么要有 _title_is_mostly_localized 这道判断：翻译已挪到流程最后
        # （负责人 2026-11-09 定：中文阶段先处理颜色/图片/标题/价格，最后统一翻译），
        # 所以走到这里时标题**本来就是中文**。老写法无条件 CJK_RE.sub 会把
        # 「2025新款夏季高端男鞋潮流百搭网面透气厚底青少年跑步运动休闲鞋」
        # 删成只剩「2025」，妙手保存直接报
        # 「shopCollectItemInfo.title 长度不能少于25个字符」（实测）。
        # 判据：拉丁字母数量多于中文字符数，才算「已本地化、混入零星中文」。
        data["title"] = CJK_RE.sub("", raw_title).strip() or raw_title
        done.append("标题去中文")

    # 主图：去重 + 裁到 9
    if main_imgs:
        unique = list(dict.fromkeys(main_imgs))
        if len(unique) != len(main_imgs):
            done.append("主图去重")
        if len(unique) > MAX_MAIN_IMAGES:
            unique = unique[:MAX_MAIN_IMAGES]
            done.append("主图裁到%d" % MAX_MAIN_IMAGES)
        data["imgUrls"] = unique
        main_imgs = unique

    # 详情图裁到 9
    notes = str(data.get("notes") or "")
    if notes:
        urls = detail_image_unique(notes)
        if len(urls) > MAX_DETAIL_IMAGES:
            last = notes.rfind(urls[MAX_DETAIL_IMAGES - 1])
            if last > 0:
                data["notes"] = notes[:last + len(urls[MAX_DETAIL_IMAGES - 1])]
                done.append("详情图裁到%d" % MAX_DETAIL_IMAGES)

    # 描述兜底
    # 描述（负责人 2026-11-05 明确）：描述页只放图片，不放文字。
    # 之前「描述为空就用标题兜底」会在描述页塞一段标题文字，
    # 和图片混排、还带语言问题。现在改为：
    #   1. 清理 notes 的无效嵌套与空白段；
    #   2. 描述没有图时，用主图补（同一张图主图和详情可共用）；
    #   3. 绝不注入文字。
    notes_raw = str(data.get("notes") or "")
    if notes_raw:
        cleaned_notes = _clean_notes_html(notes_raw)
        if cleaned_notes != notes_raw:
            data["notes"] = cleaned_notes
            done.append("描述结构清理")
    else:
        cleaned_notes = ""
    if not detail_image_urls(cleaned_notes) and main_imgs:
        # 描述里没有图：用主图补（最多 9 张，与详情图上限一致）
        data["notes"] = _images_html(main_imgs[:MAX_DETAIL_IMAGES])
        done.append("描述用主图补图(%d张)" % min(len(main_imgs), MAX_DETAIL_IMAGES))

    # 尺寸
    if any(_num(data.get(k)) is None
           for k in ("packageLength", "packageWidth", "packageHeight")):
        for key, value in zip(("packageLength", "packageWidth", "packageHeight"), dims_spec):
            if _num(data.get(key)) is None:
                data[key] = value
        done.append("补包裹尺寸%s" % (dims_spec,))

    # 重量
    weight = _num(data.get("weight"))
    sku_map = data.get("skuMap") or {}
    active = [v for v in sku_map.values()
              if isinstance(v, dict) and str(v.get("isDelete", "0")) != "1"] \
        if isinstance(sku_map, dict) else []
    if weight is None:
        sku_weights = [_num(s.get("weight")) for s in active]
        sku_weights = [w for w in sku_weights if w]
        if sku_weights:
            data["weight"] = round(median(sku_weights), 2)
            done.append("从SKU补重量(中位数)")
            weight = data["weight"]
        else:
            data["weight"] = max(WEIGHT_MIN, min(0.6, cap))
            data["weightEstimated"] = True
            done.append("重量估算(标记)")
            weight = data["weight"]
    if weight is not None and weight < WEIGHT_MIN:
        data["weightBeforeFix"] = weight
        data["weight"] = WEIGHT_MIN
        done.append("重量抬到%.2f" % WEIGHT_MIN)
        weight = WEIGHT_MIN
    if weight is not None and weight > cap:
        data["weightBeforeCap"] = weight
        data["weight"] = cap
        done.append("重量压到上限%.2f" % cap)

    # 价格
    if isinstance(sku_map, dict):
        changed = 0
        for sku in sku_map.values():
            if not isinstance(sku, dict) or str(sku.get("isDelete", "0")) == "1":
                continue
            origin = _dec(sku.get("originPrice"))
            price = _dec(sku.get("price"))
            vat = _dec(sku.get("priceIncludeVat"))
            base = None
            if origin is not None:
                base = (origin / EXCHANGE_RATE).quantize(Decimal("0.01"),
                                                         rounding=ROUND_HALF_UP)
            elif price is not None:
                base = price
            elif vat is not None:
                base = vat
            if base is None:
                continue
            target = (base + PRICE_BUMP_MYR).quantize(Decimal("0.01"),
                                                      rounding=ROUND_HALF_UP)
            if vat is None or vat < target:
                sku["priceIncludeVat"] = format(target, ".2f")
                changed += 1
        if changed:
            done.append("价格+2MYR(%d条)" % changed)

    # COD
    cod = data.get("isCodOpen")
    if isinstance(cod, bool):
        data["isCodOpen"] = "1" if cod else "0"
        done.append("COD转字符串")
    elif not (isinstance(cod, str) and cod in ("0", "1")):
        data["isCodOpen"] = "1"
        done.append("COD补默认值")

    # 规格
    props = [p for p in (data.get("skuPropertyList") or []) if isinstance(p, dict)]
    if props:
        for dim, prop in enumerate(props):
            values = [av for av in (prop.get("attrValueList") or []) if isinstance(av, dict)]
            size_like = sum(1 for av in values
                            if _looks_like_size(str(av.get("attrValue") or "")))
            is_size = bool(values) and size_like * 2 >= len(values)
            if not str(prop.get("propName") or "").strip():
                prop["propName"] = "尺码" if is_size else "颜色"
                done.append("补propName")
            if not str(prop.get("propId") or "").strip():
                prop["propId"] = PROP_ID_SIZE if is_size else PROP_ID_COLOR
                done.append("补propId")
            # 截断超长
            for av in values:
                label = str(av.get("attrValue") or "").strip()
                if len(label) > MAX_ATTR_VALUE_LEN:
                    av["attrValue"] = label[:MAX_ATTR_VALUE_LEN]
                    done.append("属性值截断")
            # 颜色补图
            if str(prop.get("propName")) not in ("尺码", "尺寸"):
                for index, av in enumerate(values):
                    if av.get("imageUrl") or (av.get("imgUrls") or []):
                        continue
                    if main_imgs:
                        url = main_imgs[index % len(main_imgs)]
                        av["imageUrl"] = url
                        av["imgUrls"] = [url]
                        if not av.get("imgUrl"):
                            av["imgUrl"] = url
                        done.append("颜色补图")
            prop["attrValueList"] = values
        data["skuPropertyList"] = props

        # colorMap / sizeMap
        color_map: dict[str, Any] = {}
        size_map: dict[str, Any] = {}
        for prop in props:
            name = str(prop.get("propName") or "")
            for av in (prop.get("attrValueList") or []):
                if not isinstance(av, dict):
                    continue
                label = str(av.get("attrValue") or "").strip()
                if not label:
                    continue
                if name in ("尺码", "尺寸"):
                    size_map[label] = {"name": label}
                else:
                    url = av.get("imageUrl") or (av.get("imgUrls") or [None])[0]
                    entry: dict[str, Any] = {"name": label}
                    if url:
                        entry["imgUrls"] = [url]
                        entry["imgUrl"] = url
                    color_map[label] = entry
        if color_map:
            data["colorMap"] = color_map
            data["colorPropName"] = "颜色"
        if size_map:
            data["sizeMap"] = size_map
            data["sizePropName"] = "尺码"

    # itemNum 截断
    if isinstance(sku_map, dict):
        for sku in sku_map.values():
            if not isinstance(sku, dict):
                continue
            item_num = sku.get("itemNum")
            if item_num is not None and len(str(item_num)) > MAX_ITEM_NUM_LEN:
                sku["itemNum"] = str(item_num)[:MAX_ITEM_NUM_LEN]
                done.append("itemNum截断")

    # skuMap key 段位置重排（对齐 skuPropertyList 的维度顺序）
    props_now = [p for p in (data.get("skuPropertyList") or []) if isinstance(p, dict)]
    if props_now and isinstance(sku_map, dict) and sku_map:
        dim_ids: list[set[str]] = []
        for prop in props_now:
            ids = set()
            for av in (prop.get("attrValueList") or []):
                if isinstance(av, dict):
                    vid = str(av.get("attrValueId") or "").strip()
                    if vid:
                        ids.add(vid)
            dim_ids.append(ids)

        reordered = 0
        dropped = 0
        new_map: dict[str, Any] = {}
        for key, value in sku_map.items():
            parts = [x.strip() for x in str(key).split(";") if x.strip()]
            if len(parts) != len(dim_ids):
                # 段数不符：直接丢弃（这种 key 上游必然拒绝）
                dropped += 1
                continue
            # 按「每个 part 属于哪个维度」重排
            ordered: list[str | None] = [None] * len(dim_ids)
            leftover: list[str] = []
            for part in parts:
                placed = False
                for dim, ids in enumerate(dim_ids):
                    if ordered[dim] is None and part in ids:
                        ordered[dim] = part
                        placed = True
                        break
                if not placed:
                    leftover.append(part)
            if any(o is None for o in ordered):
                # 有段找不到归属维度
                for dim, ids in enumerate(dim_ids):
                    if ordered[dim] is None and leftover:
                        ordered[dim] = leftover.pop(0)
            if any(o is None for o in ordered):
                dropped += 1
                continue
            new_key = ";" + ";".join(str(o) for o in ordered) + ";"
            if new_key != key:
                reordered += 1
            new_map[new_key] = value
        if reordered or dropped:
            data["skuMap"] = new_map
            if reordered:
                done.append("skuMap段位置重排(%d条)" % reordered)
            if dropped:
                done.append("skuMap丢弃无法对齐的key(%d条)" % dropped)

    return data, done


# --- 新旧对比（重新采集后用）---------------------------------------------
def compare(old: Mapping[str, Any], new: Mapping[str, Any]) -> dict[str, Any]:
    """对比重新采集的数据与现有数据，决定用哪份。

    规则：
      · 新数据在某字段上「完整度更高」就用新的（例如原来缺规格、现在有）
      · 新数据更差就保留旧的
      · 两者都有值时，优先用新采集的（上游最新）
    """
    old_d = dict(old or {})
    new_d = dict(new or {})
    decisions: dict[str, str] = {}

    def score(data: Mapping[str, Any]) -> int:
        """字段完整度打分，越高越完整。"""
        total = 0
        if str(data.get("title") or "").strip():
            total += 2
        if [u for u in (data.get("imgUrls") or []) if u]:
            total += 2
        if detail_image_unique(data.get("notes")):
            total += 1
        if str(data.get("cid") or "").strip():
            total += 2
        if isinstance(data.get("skuMap"), dict) and data["skuMap"]:
            total += 3
        if [p for p in (data.get("skuPropertyList") or []) if isinstance(p, dict)]:
            total += 3
        if _num(data.get("weight")):
            total += 2
        if all(_num(data.get(k)) for k in
               ("packageLength", "packageWidth", "packageHeight")):
            total += 2
        return total

    old_score = score(old_d)
    new_score = score(new_d)

    # 逐字段决定
    for field in ("title", "cid", "imgUrls", "notes", "skuMap",
                  "skuPropertyList", "weight", "packageLength",
                  "packageWidth", "packageHeight", "colorMap", "sizeMap"):
        old_val = old_d.get(field)
        new_val = new_d.get(field)
        old_has = bool(old_val) if not isinstance(old_val, (int, float)) else bool(_num(old_val))
        new_has = bool(new_val) if not isinstance(new_val, (int, float)) else bool(_num(new_val))
        if new_has and not old_has:
            decisions[field] = "new"
        elif old_has and not new_has:
            decisions[field] = "old"
        elif new_has and old_has:
            decisions[field] = "new"      # 都有值用最新的
        else:
            decisions[field] = "none"

    return {
        "oldScore": old_score,
        "newScore": new_score,
        "prefer": "new" if new_score >= old_score else "old",
        "decisions": decisions,
    }


def merge(old: Mapping[str, Any], new: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """按 compare 的决策合并，返回 (合并结果, 变更说明)。"""
    result = dict(old or {})
    verdict = compare(old, new)
    changes: list[str] = []
    for field, choice in verdict["decisions"].items():
        if choice == "new":
            if result.get(field) != (new or {}).get(field):
                result[field] = (new or {}).get(field)
                changes.append("更新%s" % field)
    # 新采集的必带字段
    for field in ("shopId", "language", "site", "detailId"):
        if (new or {}).get(field) not in (None, ""):
            result[field] = new[field]
    # 清掉重采标记
    result.pop("needsRecollect", None)
    result.pop("needsRecollectReason", None)
    return result, changes
