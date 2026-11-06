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
