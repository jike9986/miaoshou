"""颜色清洗与校验：老版 shoe_workflow.py 三重保护的移植（审查 P0-3 / P0-5）。

为什么单独成一个模块：老版对颜色有三重硬保护，
  1. `clean_color`（老版 :183）——真清洗，认不出**抛错**；
  2. `_color_attribute_problem`（老版 :228）——颜色属性数不对时给**根因**，
     而不是一句「没有颜色属性」；
  3. `normalize_colors`（老版 :252）——去重 / 上限 10 / 删对应 SKU / 全删保护。

新版此前退化成了「尽力抽取 + 失败静默 + 只改文本」：`product_service.clean_color`
取不到颜色就原样返回，`warehouse_service._clean_color_props` 只就地改 attrValue，
去重、上限、SKU 剪枝、全删保护四项全缺，属性数 0/>1/id 缺失/id 重复全部静默通过。
后果是颜色没认出来的商品照样入仓，上架时在平台侧被拒，且没有任何线索指回源头。

调用方（下一轮接线）：`warehouse_service._clean_color_props` 改为调用
`normalize_colors(payload)`；本模块不依赖任何其它 service，避免循环 import。
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

# 平台侧颜色取值上限：超过这个数量妙手保存会失败，所以必须自己先截断
MAX_COLORS = 10

# 颜色属性名集合（老版 normalize_colors :254 的内联集合，逐字照抄）。
# 马来语 warna 是翻译后商品的属性名，漏掉它会让译文商品整批「没有颜色属性」。
COLOR_NAMES = frozenset({"颜色", "色彩", "color", "colour", "warna"})

# 中文颜色词表（老版 shoe_workflow.COLOR_NAMES :25，逐字照抄，顺序也不能动：
# 正则用 | 拼接，长词必须排在短词前面，否则「咖啡」会被「咖」抢先匹配）。
COLOR_WORDS = (
    "咖啡", "卡其", "藏青", "米白", "乳白", "玫红", "酒红", "天蓝",
    "军绿", "墨绿", "杏", "棕", "褐", "驼", "青", "黑", "白", "灰",
    "红", "橙", "黄", "绿", "蓝", "紫", "粉", "金", "银", "米",
)

# 颜色词 = 可选深浅/荧光修饰 + 颜色字 + 可选「色」（老版 :30）
COLOR_TOKEN = re.compile(r"(?:浅|深|暗|亮|荧光)?(?:" + "|".join(COLOR_WORDS) + r")色?")

# 英文颜色词（老版 :28，逐字照抄）
ENGLISH_COLOR = re.compile(
    r"\b(?:black|white|beige|grey|gray|apricot|nude|pink|red|blue|green|brown|khaki"
    r"|navy|purple|yellow|orange|silver|gold|cream|coffee|wine|rose|leopard|camel"
    r"|ivory|burgundy|olive|tan|multicolor)\b", re.I)

# 马来语颜色词：老版没有（它处理的是翻译前的中文源），新版属性值大量是译文，
# 少了这一层，译文商品的颜色会全部落到「认不出 → 抛错」，等于把整批挡住。
MALAY_COLOR_WORDS = (
    "hitam", "putih", "kelabu", "merah", "biru", "hijau", "kuning", "ungu",
    "oren", "jingga", "emas", "perak", "perang", "coklat", "krem",
    "merah jambu", "biru muda", "hijau muda",
)
MALAY_COLOR = re.compile(
    r"\b(?:" + "|".join(sorted(MALAY_COLOR_WORDS, key=len, reverse=True)) + r")\b", re.I)

# 型号 / 货号码（老版 :38，逐字照抄）。三条分支各自对应供应商的一种写法：
#   G22 绿色      → 字母+数字
#   1987A 白     → 数字+字母
#   625*- 灰     → 数字带 * 或 # 符号（符号是它「是货号」的唯一证据）
COLOR_CODE = re.compile(
    r"[A-Za-z]+[-_ ]*\d+[A-Za-z0-9_*-]*"
    r"|\d+[-_ ]*[A-Za-z]+[A-Za-z0-9_*-]*"
    r"|\d+[A-Za-z0-9_*-]*[*#]+[-_ ]*"
)

# 前导纯数字型号（老版 :45，逐字照抄）：`1987白色`。
# 后面必须紧跟中文，裸数字不会被这条吞掉——否则「1987」会被当成颜色放行。
LEADING_CODE = re.compile(r"^(\d[\dA-Za-z_*-]*[-_ ]*)+(?=[\u4e00-\u9fff])")

# 变体后缀（老版 :46，逐字照抄）：它们是商品真实差异（加绒/加厚/透气…），
# 清洗时要保留，不能当备注词删掉，否则同色不同工艺的 SKU 会被合并成一个。
COLOR_VARIANTS = (
    "加厚", "加绒", "加棉", "保暖", "毛里", "棉里", "绒里",
    "绒面", "毛绒", "网面", "透气", "防滑", "软底", "厚底", "加宽",
    "宽版", "轻便", "轻量",
)
COLOR_VARIANT_PATTERN = "|".join(sorted(COLOR_VARIANTS, key=len, reverse=True))

# 「X鞋-红色」这种「鞋类前缀 + 分隔符 + 颜色」的写法（老版 :195）。
# 老版只认纯中文前缀；供应商实际还会写 `X鞋-红色`（拉丁型号 + 鞋），
# 所以前缀字符集放宽到拉丁字母。两道守卫不变：前缀里不能含颜色词、
# 分隔符后面必须真的是颜色，避免把 `红色-黑色` 这种拼色拆错。
_SHOE_PREFIX = re.compile(r"([\u4e00-\u9fffA-Za-z]{1,12}鞋)\s*[-－—_:：/]\s*(.+)")

# 括号里带包装说明的整段（老版 :198）
_PACK_BRACKET = re.compile(r"[（(【\[][^）)】\]]*(?:盒装|袋装|包装|装盒)[^）)】\]]*[）)】\]]")
# 包装词本身（老版 :199）
_PACK_WORDS = re.compile(
    r"盒装|袋装|礼盒装|礼盒|套装|组合装|两双装|一双装|单只装|独立包装|普通包装|纸盒包装|包装")
# 括号里是变体后缀的，只留后缀（老版 :200）
_BRACKET_VARIANT = re.compile(
    rf"[（(【\[]\s*({COLOR_VARIANT_PATTERN})(?:版|款)?\s*[）)】\]]")
# 括号里是英文颜色的，把词留下（老版 :203）：`Y18[beige]` → beige
_BRACKET_ENGLISH = re.compile(r"[（(【\[]\s*([A-Za-z][A-Za-z\s-]{1,24})\s*[）)】\]]")
# 剩下的括号备注一律删（老版 :204）
_BRACKET_ANY = re.compile(r"[（(【\[][^）)】\]]*[）)】\]]")
# 版次词（老版 :205）
_STYLE_WORDS = re.compile(r"升级版|标准版|新款|款")
# 变体后缀后面跟的「版 / 款」（老版 :206）
_VARIANT_VERSION = re.compile(rf"({COLOR_VARIANT_PATTERN})(?:版|款)")

# 判断「颜色和尺码被压成一个属性」用：名字同时含颜色词与尺码词（如「颜色尺码」）。
# 老版只认「规格型号」这一种压平写法（没选类目时的表现），供应商自己的合并
# 命名还有「颜色尺码」「颜色/尺码」，一并认出来才能给出真正的根因。
_COLOR_NAME_HINT = re.compile(r"颜色|色彩|色|colo(?:u)?r|warna", re.I)
_SIZE_NAME_HINT = re.compile(r"尺码|尺寸|码数|规格|型号|size|saiz", re.I)


class ColorError(ValueError):
    """颜色无法确定时抛，消息里带根因，直接给人看。"""


def clean_color_strict(value: Any) -> str:
    """清理单个颜色值；认不出就抛 ColorError，绝不原样返回。

    处理顺序照抄老版 clean_color（:183-225），每一步都在解决供应商的一种真实写法：
      1. 去空白：`黑色 加绒` / 全角空格 / 不换行空格都没有语义；
      2. 去型号码：`G22绿色`、`Y18[beige]`、`625*-灰色`；
      3. 去前导数字型号：`1987白色` → `白色`（裸数字不吞，见 LEADING_CODE 注释）；
      4. 拆鞋类前缀：`X鞋-红色` → `红色`；
      5. 去包装词与包装括号：`黑色（盒装）加绒` → `黑色加绒`；
      6. 括号内的变体后缀 / 英文颜色提取，其余括号整段删；
      7. 去版次词（升级版/标准版/新款/款）；
      8. 剥掉两端分隔符；
      9. 摘出结尾的变体后缀暂存，取颜色词，再拼回去；
     10. 中文认不出时走英文兜底；仍认不出 → 抛。
    """
    original = str(value or "").strip()
    if not original:
        raise ColorError(f"颜色为空，无法确定：{value!r}")
    text = re.sub(r"\s+", "", original)
    text = COLOR_CODE.sub("", text)

    leading = LEADING_CODE.match(text)
    if leading and COLOR_TOKEN.match(text[leading.end():]):
        text = text[leading.end():]

    prefix = _SHOE_PREFIX.fullmatch(text)
    if prefix and not COLOR_TOKEN.search(prefix.group(1)) and COLOR_TOKEN.match(prefix.group(2)):
        text = prefix.group(2)

    text = _PACK_BRACKET.sub("", text)
    text = _PACK_WORDS.sub("", text)
    text = _BRACKET_VARIANT.sub(lambda match: match.group(1), text)
    text = _BRACKET_ENGLISH.sub(lambda match: match.group(1), text)
    text = _BRACKET_ANY.sub("", text)
    text = _STYLE_WORDS.sub("", text)
    text = _VARIANT_VERSION.sub(r"\1", text)
    text = text.strip(" -_/:：,，()（）[]【】")

    variant = next((suffix for suffix in COLOR_VARIANTS if text.endswith(suffix)), "")
    if variant:
        text = text[:-len(variant)]

    tokens = COLOR_TOKEN.findall(text)
    residue = COLOR_TOKEN.sub("", text)

    if not tokens:
        # 经销商把颜色写成英文（常混套装/版次说明）：`Beige + Black [Two-Piece Set]`。
        found = ENGLISH_COLOR.findall(original)
        if found:
            return " ".join(dict.fromkeys(word.capitalize() for word in found))
        found_my = MALAY_COLOR.findall(original)
        if found_my:
            return " ".join(dict.fromkeys(word.capitalize() for word in found_my))
        # 老版这里还有一条「纯拉丁文本就按词首字母大写放行」，它会把 `zzz`
        # 变成 `Zzz` 当成颜色交上去——正是要修掉的静默放行。改成直接抛。
        raise ColorError(f"认不出颜色，需要人工确认：{value!r}")

    # 颜色词之外还剩实词（`红色牛皮款`、`蓝白拼色` 之外的东西）说明这串里混了
    # 工艺/材质，交给人工比猜更安全。分隔符与「配拼撞色」这类连接词不算残留。
    if residue.strip(" -_+/、,，配拼撞色"):
        raise ColorError(f"认不出颜色，需要人工确认：{value!r}")
    return "".join(tokens) + variant


def _problem_text(names: list[str], matched: int, *, category_selected: bool) -> str:
    """老版 _color_attribute_problem（:228-249）的正文，逐句照抄。

    单独拆出来是因为 color_attribute_problem 要先判断「没问题」，而
    normalize_colors 只想在真的有问题时抛错。
    """
    if matched > 1:
        return f"商品有 {matched} 个颜色属性（{'、'.join(names)}），无法确定用哪一个"
    merged = [name for name in names
              if name.casefold() in {"规格型号", "规格", "型号", "specification"}
              or (_COLOR_NAME_HINT.search(name) and _SIZE_NAME_HINT.search(name))]
    if not category_selected:
        detail = f"现有属性是「{'、'.join(names)}」，" if names else ""
        return ("该商品还没有选择类目，妙手因此没有展开颜色/尺码/重量"
                f"（{detail}属性被压成一个）。请先在妙手给这件商品选好类目，再来处理")
    if not names:
        return "商品没有 SKU 属性，无法按颜色处理"
    if merged:
        return (f"商品的 SKU 属性只有「{'、'.join(names)}」，没有独立的颜色属性。"
                "这类商品把颜色和尺码合并在同一个属性里（例如「蓝色牛皮款;35」），"
                "无法自动拆分，请在妙手改成独立的颜色属性后再处理")
    return f"商品没有名为「颜色」的 SKU 属性，现有属性是「{'、'.join(names)}」，无法按颜色处理"


def _as_props(props: Any) -> list[dict[str, Any]]:
    if isinstance(props, Mapping):
        props = props.get("skuPropertyList")
    if not isinstance(props, list):
        return []
    return [prop for prop in props if isinstance(prop, dict)]


def _color_matches(props: Any) -> list[dict[str, Any]]:
    return [prop for prop in _as_props(props)
            if str(prop.get("attrName") or "").casefold() in COLOR_NAMES]


def color_attribute_problem(props: Any, *, category_selected: bool = True) -> str:
    """返回颜色属性的根因说明；空串表示没问题。

    覆盖老版能识别的全部情况：0 个颜色属性 / 多于 1 个 / 属性被压成「规格型号」/
    颜色和尺码合并在同一个属性里 / 商品根本没有 SKU 属性 / 没选类目导致的压平。
    """
    properties = _as_props(props)
    matched = len(_color_matches(properties))
    if matched == 1:
        return ""
    names = [str(prop.get("attrName") or "(无名)") for prop in properties]
    return _problem_text(names, matched, category_selected=category_selected)


def normalize_colors(props: Any, sku_map: Mapping[str, Any] | None = None, *,
                     category_selected: bool | None = None) -> dict[str, Any]:
    """规范化颜色属性：要求恰好 1 个，去重合并，最多 10 色，剪掉被删颜色的 SKU。

    参数：
      props            skuPropertyList 列表；也可以直接传整个 payload（字典），
                       这时 skuMap / cid 从 payload 里取，并就地写回。
      sku_map          skuMap 字典；不传时用 payload 里的。
      category_selected 是否已选类目；None = 从 payload 的 cid 推断。

    返回：{properties, skuMap, colors, removed, removedSkuKeys, message}
      removed       被删掉的 attrValueId 列表
      removedSkuKeys 被剪掉的 SKU 键
      message       降级/截断说明，空串表示无话可说

    校验顺序与老版一致：属性数 → 取值与 id 齐全 → id 不重复 → 清洗/去重 →
    截断 → SKU 剪枝（映射必须唯一）→ 全删保护。前一步不过就绝不改数据。
    """
    payload: dict[str, Any] | None = props if isinstance(props, Mapping) else None
    properties = _as_props(props)
    if payload is not None:
        if sku_map is None:
            candidate = payload.get("skuMap")
            sku_map = candidate if isinstance(candidate, Mapping) else None
        if category_selected is None:
            category_selected = bool(str(payload.get("cid") or "").strip())
    if category_selected is None:
        category_selected = True

    matches = _color_matches(properties)
    if len(matches) != 1:
        raise ColorError(_problem_text(
            [str(prop.get("attrName") or "(无名)") for prop in properties],
            len(matches), category_selected=bool(category_selected)))

    attr = matches[0]
    values = attr.get("attrValueList") or []
    if not isinstance(values, list) or not values:
        raise ColorError("颜色属性没有取值，无法按颜色处理")
    if any(not isinstance(value, Mapping) or not value.get("attrValueId") for value in values):
        raise ColorError("颜色取值缺少 attrValueId，无法安全去重或剪 SKU")
    ids = [str(value["attrValueId"]) for value in values]
    if len(set(ids)) != len(ids):
        raise ColorError("颜色 attrValueId 有重复，无法安全去重或剪 SKU")

    # 同色不同写法合并成一组：`白色` / `白色 ` / `白` 视为同一个颜色。
    # key 去掉「色」是让「黄」和「黄色」归并（老版 :270 的注释）。
    # 下标一起带进组里，后面要靠它把输出顺序还原成原顺序。
    groups: dict[str, list[tuple[int, Mapping[str, Any], str]]] = {}
    for index, value in enumerate(values):
        name = clean_color_strict(value.get("attrValue"))
        groups.setdefault(name.replace("色", ""), []).append((index, value, name))

    # 每组挑一个代表：优先取「原文本来就是这个写法」的那个，改写量最小，
    # 也保证同名 SKU 的展示文案不会因为清洗顺序而漂移（老版 :274）。
    #
    # 顺序上多做一步老版没有的处理：截断按「首次出现顺序」决定留谁（与老版
    # `list(groups.values())[:10]` 一致），但**输出**按原 attrValueList 下标排序。
    # 原因在 pre_save_check 规则 8：它要求保留下来的 id 顺序与原来一致
    # （`after_ids == [v for v in before_ids if v in retained]`），而组代表可能
    # 是组里靠后的那个元素，不重排就会让整件商品被那条规则挡在入库前。
    chosen: list[tuple[int, dict[str, Any]]] = []
    for candidates in list(groups.values())[:MAX_COLORS]:
        index, value, name = min(
            candidates,
            key=lambda pair: str(pair[1].get("attrValue") or "").strip() != pair[2])
        chosen.append((index, {**dict(value), "attrValue": name}))
    chosen.sort(key=lambda pair: pair[0])
    chosen_values = [item for _, item in chosen]

    colors = [item["attrValue"] for item in chosen_values]
    messages: list[str] = []
    if len(groups) > MAX_COLORS:
        # 老版是直接截断（`list(groups.values())[:10]`），这里保持一致，
        # 但把截断这件事说出来——静默少颜色比报错更难查。
        messages.append(
            f"颜色 {len(groups)} 个超过上限 {MAX_COLORS}，已按顺序保留前 {MAX_COLORS} 个")

    kept_ids = {str(item["attrValueId"]) for item in chosen_values}
    removed_ids = [value_id for value_id in ids if value_id not in kept_ids]

    sku_map_out: dict[str, Any] | None = None
    removed_sku_keys: list[str] = []
    if sku_map is not None:
        # SKU 键形如 `颜色id;尺码id`。只剪「颜色被删掉」的键；一个键对应不上
        # 唯一颜色说明数据本身有问题，宁可抛错也不能瞎剪（老版 :281-285）。
        id_set = set(ids)
        sku_map_out = {}
        for key, sku in sku_map.items():
            color_ids = set(str(key).strip(";").split(";")) & id_set
            if len(color_ids) != 1:
                raise ColorError(f"SKU 键与颜色取值的对应关系不明确：{key}")
            if color_ids & set(removed_ids):
                removed_sku_keys.append(str(key))
                continue
            sku_map_out[key] = sku
        # 全删保护：颜色清理不许把商品清成没有在售 SKU（老版 :286-287）。
        if not any(str((sku or {}).get("isDelete", "0")) != "1"
                   for sku in sku_map_out.values() if isinstance(sku, Mapping)):
            raise ColorError("颜色清理会删掉所有在售 SKU，已停止")
    else:
        # 调用方手上只有属性、没有 skuMap（例如预览）：不做剪枝，但要留痕，
        # 否则「为什么没剪 SKU」会变成下一个静默缺口。
        messages.append("未提供 skuMap，本次跳过 SKU 剪枝与全删保护")

    # 就地更新：老版也是直接改 attr["attrValueList"] / item["skuMap"]，
    # 调用方手上的 payload 立刻生效，不必再手工拼回。
    attr["attrValueList"] = chosen_values
    if payload is not None:
        payload["skuPropertyList"] = properties
        if sku_map_out is not None:
            payload["skuMap"] = sku_map_out

    return {
        "properties": properties,
        "skuMap": sku_map_out if sku_map_out is not None else (sku_map or {}),
        "colors": colors,
        "removed": removed_ids,
        "removedSkuKeys": removed_sku_keys,
        "message": "；".join(messages),
    }
