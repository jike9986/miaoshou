"""马来语/描述文字 → 标准颜色名映射，用于修 96 件颜色不规范的品。

问题：clean_color_strict 只认中文和英文标准色，马来语（Kopi=咖啡、Aprikot=杏）
和描述文字（Stoking=袜子、Empat roda=四轮）认不出，直接抛错。

做法：
  1. 先查下方 MALAY_COLORS 表（马来语 → 标准色）
  2. 表里没有的，从文字里提取任何英文/马来语颜色词
  3. 完全提取不到（如 Stoking / Empat roda，本身不是颜色）→ 标 needsRecollect

标准色用英文（马来站可读），与妙手兼容。
"""

# 马来语/印尼语 → 标准英文色
MALAY_COLORS = {
    # 基础色
    "hitam": "Black", "putih": "White", "kelabu": "Grey", "kelabu tua": "Dark Grey",
    "merah": "Red", "merah jambu": "Pink", "merah tua": "Dark Red",
    "biru": "Blue", "biru tua": "Dark Blue", "biru muda": "Light Blue",
    "biru langit": "Sky Blue", "hijau": "Green", "hijau tua": "Dark Green",
    "hijau muda": "Light Green", "kuning": "Yellow", "oren": "Orange",
    "jingga": "Orange", "ungu": "Purple", "violet": "Purple",
    "perang": "Brown", "coklat": "Brown", "cokelat": "Brown",
    "koko": "Brown", "kopi": "Brown", "emas": "Gold", "perak": "Silver",
    "kelabu perak": "Silver", "krim": "Cream", "krem": "Cream",
    "kuning air": "Beige", "pasir": "Beige", "beige": "Beige",
    "aprikot": "Apricot", "peach": "Peach", "pudin": "Pudding",
    "matcha": "Matcha", "kunyit": "Turmeric", "aurora": "Aurora",
    "lagoon": "Lagoon", "milan": "Milan", "bluesingle": "Blue",
    "gunabiru": "Blue", "rosered": "Rose Red", "rose": "Rose",
    # 组合
    "putih hitam": "Black White", "hitam putih": "Black White",
    "putih biru": "White Blue", "putih merah jambu": "White Pink",
    "hitam kelabu": "Black Grey", "hitam ungu": "Black Purple",
    "biru hitam": "Blue Black", "merah hitam": "Red Black",
    # 非颜色描述（用于识别「这不是颜色」）
    "stoking": None, "stokin": None, "socks": None, "socksn": None,
    "empat roda": None, "model stand": None, "model boleh": None,
    "boleh tang": None, "kipas yang": None, "kain atas": None,
    "warna kamu": None, "warna koko": "Brown", "berwarna": None,
    "bogel": None, "tepung aka": None, "flip-flops": None,
    "dz-g-pezip": None,
}

# 直接从文本里找这些词（长词优先，避免 "biru" 抢了 "biru tua"）
_COLOR_KEYS = sorted(
    (k for k, v in MALAY_COLORS.items() if v),
    key=len, reverse=True,
)

# 英文标准色（兜底）
ENGLISH_COLORS = {
    "black": "Black", "white": "White", "grey": "Grey", "gray": "Grey",
    "red": "Red", "pink": "Pink", "blue": "Blue", "green": "Green",
    "yellow": "Yellow", "orange": "Orange", "purple": "Purple",
    "brown": "Brown", "gold": "Gold", "silver": "Silver", "cream": "Cream",
    "beige": "Beige", "khaki": "Khaki", "navy": "Navy", "maroon": "Maroon",
    "apricot": "Apricot", "peach": "Peach", "matcha": "Matcha",
    "violet": "Purple", "rose": "Rose", "coffee": "Brown",
    "army": "Army Green", "olive": "Olive",
}


# ------------------------------------------------------------------
# 中文颜色词 → 目标语言（备用翻译：当妙手 API 不返回颜色翻译时使用）
# ------------------------------------------------------------------
# 长词优先，避免 "深蓝色" 被 "蓝" 先匹配到
_ZH_COLOR_MAP: dict[str, dict[str, str]] = {
    # 三字及以上（先匹配）
    "玫瑰金": {"ms": "Emas Ros", "en": "Rose Gold"},
    "荧光绿": {"ms": "Hijau Neon", "en": "Neon Green"},
    "荧光黄": {"ms": "Kuning Neon", "en": "Neon Yellow"},
    "海军蓝": {"ms": "Biru Tua", "en": "Navy Blue"},
    "象牙白": {"ms": "Putih Gading", "en": "Ivory White"},
    "奶油白": {"ms": "Putih Krim", "en": "Cream White"},
    "香槟金": {"ms": "Emas Champagne", "en": "Champagne Gold"},
    "天空蓝": {"ms": "Biru Langit", "en": "Sky Blue"},
    "草坪绿": {"ms": "Hijau Rumput", "en": "Grass Green"},
    "巧克力": {"ms": "Coklat", "en": "Chocolate"},
    "卡其色": {"ms": "Khaki", "en": "Khaki"},
    "深紫色": {"ms": "Ungu Tua", "en": "Dark Purple"},
    "淡紫色": {"ms": "Ungu Muda", "en": "Light Purple"},
    "浅灰色": {"ms": "Kelabu Muda", "en": "Light Grey"},
    "暗灰色": {"ms": "Kelabu Gelap", "en": "Dark Grey"},
    "深棕色": {"ms": "Coklat Tua", "en": "Dark Brown"},
    "咖啡色": {"ms": "Coklat", "en": "Coffee Brown"},
    "米黄色": {"ms": "Krim Kuning", "en": "Cream Yellow"},
    "肤色":   {"ms": "Warna Kulit", "en": "Skin"},
    "肉色":   {"ms": "Warna Kulit", "en": "Flesh"},
    # 两字
    "黑红": {"ms": "Hitam Merah", "en": "Black Red"},
    "黑白": {"ms": "Hitam Putih", "en": "Black White"},
    "黑蓝": {"ms": "Hitam Biru", "en": "Black Blue"},
    "黑灰": {"ms": "Hitam Kelabu", "en": "Black Grey"},
    "黑绿": {"ms": "Hitam Hijau", "en": "Black Green"},
    "白蓝": {"ms": "Putih Biru", "en": "White Blue"},
    "米白": {"ms": "Putih Susu", "en": "Off White"},
    "米色": {"ms": "Krim", "en": "Cream"},
    "米黄": {"ms": "Krim Kuning", "en": "Cream Yellow"},
    "奶白": {"ms": "Putih Susu", "en": "Milk White"},
    "象牙": {"ms": "Gading", "en": "Ivory"},
    "玫红": {"ms": "Merah Ros", "en": "Rose Red"},
    "玫瑰": {"ms": "Ros", "en": "Rose"},
    "香槟": {"ms": "Champagne", "en": "Champagne"},
    "深蓝": {"ms": "Biru Tua", "en": "Dark Blue"},
    "藏蓝": {"ms": "Biru Tua", "en": "Navy Blue"},
    "藏青": {"ms": "Biru Tua", "en": "Navy"},
    "浅蓝": {"ms": "Biru Muda", "en": "Light Blue"},
    "天蓝": {"ms": "Biru Langit", "en": "Sky Blue"},
    "宝蓝": {"ms": "Biru Royal", "en": "Royal Blue"},
    "深绿": {"ms": "Hijau Tua", "en": "Dark Green"},
    "墨绿": {"ms": "Hijau Tua", "en": "Dark Green"},
    "草绿": {"ms": "Hijau Rumput", "en": "Grass Green"},
    "浅绿": {"ms": "Hijau Muda", "en": "Light Green"},
    "深红": {"ms": "Merah Tua", "en": "Dark Red"},
    "粉红": {"ms": "Merah Jambu", "en": "Pink"},
    "粉色": {"ms": "Merah Jambu", "en": "Pink"},
    "卡其": {"ms": "Khaki", "en": "Khaki"},
    "咖啡": {"ms": "Coklat", "en": "Brown"},
    "深灰": {"ms": "Kelabu Gelap", "en": "Dark Grey"},
    "浅灰": {"ms": "Kelabu Muda", "en": "Light Grey"},
    "银灰": {"ms": "Kelabu Perak", "en": "Silver Grey"},
    "荧光": {"ms": "Neon", "en": "Neon"},
    "透明": {"ms": "Telus", "en": "Transparent"},
    # 单字基色（最后匹配）
    "黑": {"ms": "Hitam", "en": "Black"},
    "白": {"ms": "Putih", "en": "White"},
    "红": {"ms": "Merah", "en": "Red"},
    "蓝": {"ms": "Biru", "en": "Blue"},
    "绿": {"ms": "Hijau", "en": "Green"},
    "黄": {"ms": "Kuning", "en": "Yellow"},
    "橙": {"ms": "Oren", "en": "Orange"},
    "紫": {"ms": "Ungu", "en": "Purple"},
    "粉": {"ms": "Merah Jambu", "en": "Pink"},
    "灰": {"ms": "Kelabu", "en": "Grey"},
    "棕": {"ms": "Coklat", "en": "Brown"},
    "金": {"ms": "Emas", "en": "Gold"},
    "银": {"ms": "Perak", "en": "Silver"},
    "米": {"ms": "Krim", "en": "Cream"},
}

# 按键长降序排列，确保长词优先匹配（"深蓝" 先于 "蓝"）
_ZH_SORTED_KEYS = sorted(_ZH_COLOR_MAP.keys(), key=len, reverse=True)


def translate_chinese_color(text: str, language: str = "ms") -> str | None:
    """把中文颜色名翻译为目标语言（ms=马来语 / en=英语）。

    仅处理含有 CJK 字符的字符串；翻译不出时返回 None。
    支持复合色：「黑红」→「Hitam Merah」。
    """
    s = str(text or "").strip()
    if not s:
        return None
    # 只处理含中文的字符串
    if not any("一" <= c <= "鿿" for c in s):
        return None

    lang = (language or "ms").lower()[:2]
    if lang not in ("ms", "en"):
        lang = "ms"

    # 直接命中（含「黑色」→ strip 掉尾部「色」后再试一次）
    for candidate in (s, s.rstrip("色")):
        entry = _ZH_COLOR_MAP.get(candidate)
        if entry:
            return entry.get(lang)

    # 逐段最长前缀匹配，拼接复合译名
    parts: list[str] = []
    remaining = s.rstrip("色")          # 去掉末尾助词「色」
    while remaining:
        matched = False
        for key in _ZH_SORTED_KEYS:
            if remaining.startswith(key):
                val = _ZH_COLOR_MAP[key].get(lang)
                if val:
                    parts.append(val)
                remaining = remaining[len(key):]
                matched = True
                break
        if not matched:
            remaining = remaining[1:]   # 跳过无法识别的字符

    if parts:
        return " ".join(parts)
    return None


def to_standard_color(raw: str) -> str | None:
    """把任意颜色写法转成标准英文色；转不出返回 None（表示这不是颜色）。"""
    text = str(raw or "").strip()
    if not text:
        return None
    low = text.casefold()

    # 1. 整串直接命中
    if low in MALAY_COLORS:
        return MALAY_COLORS[low]
    if low in ENGLISH_COLORS:
        return ENGLISH_COLORS[low]

    # 2. 找出文本里出现的颜色词（可能多个）
    hits: list[str] = []
    for key in _COLOR_KEYS:
        if key in low and MALAY_COLORS[key]:
            value = MALAY_COLORS[key]
            if value not in hits:
                hits.append(value)
    for key, value in ENGLISH_COLORS.items():
        if key in low and value not in hits:
            hits.append(value)
    if hits:
        # 组合色最多取两个，避免名字过长
        return " ".join(hits[:2])

    # 3. 没有语义描述词、也没有颜色词 → 判断是不是纯「非颜色」
    for key, value in MALAY_COLORS.items():
        if value is None and key in low:
            return None

    return None
