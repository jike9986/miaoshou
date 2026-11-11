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

# 平台侧颜色取值上限，同时也是 SKU 图上限（负责人 2026-11-09 定）：
# 一种颜色一张 SKU 图，9 色正好对应 9 张，超出的颜色没有图可配。
MAX_COLORS = 9

# 颜色属性名集合（老版 normalize_colors :254 的内联集合，逐字照抄）。
# 马来语 warna 是翻译后商品的属性名，漏掉它会让译文商品整批「没有颜色属性」。
COLOR_NAMES = frozenset({"颜色", "色彩", "color", "colour", "warna"})

# --- 扩展颜色词表（来源：github userElaina/json-colorsname，769 色含中文名）---
# 只收 1-3 字的基词：4 字以上的多是「XX 玫瑰金」这类具体命名，塞进正则
# 会把正常颜色串误判成两个颜色。原始词表只有 30 个常用字，遇到
# 「抹茶灰」「雾霾蓝」「黑橘色」这种供应商写法直接认不出，整件商品报
# 「颜色全部无法识别」被拦下（2026-11-10 实测）。
COLOR_WORDS_EXTENDED = (
    "丁子茶", "中春绿", "中暗蓝", "亚麻布", "亮天蓝", "亮菊黄", "亮蓝灰", "亮钢蓝",
    "仙斎茶", "兰花紫", "军服蓝", "军校蓝", "利休鼠", "勿忘草", "千歳緑", "午夜蓝",
    "卡其布", "卡叽布", "印度红", "古代紫", "古董白", "向日葵", "土著白", "宝石绿",
    "巧克力", "幽灵白", "御纳戸", "旧蕾丝", "暗灰蓝", "暗瓦灰", "暗黄褐", "李子紫",
    "板岩蓝", "柠檬绸", "森林绿", "橄榄褐", "水浅葱", "江戸紫", "浅玫瑰", "浅粉红",
    "浅苏芳", "海军蓝", "海洋绿", "海老茶", "海贝壳", "淡珊瑚", "淡紫红", "淡钢蓝",
    "深天蓝", "深洋红", "深粉红", "深鲑鱼", "火药蓝", "火药青", "灰菊黄", "玉米丝",
    "玫瑰棕", "玫瑰红", "琉璃绀", "番木瓜", "番茄红", "皇军蓝", "皇家蓝", "石板灰",
    "秋麒麟", "紫罗兰", "纳瓦白", "绀桔梗", "绿宝石", "老饰带", "耐火砖", "花的白",
    "苍麒麟", "茄子绀", "草坪绿", "葡萄鼠", "蒲公英", "薄红梅", "藤纳戸", "赤白橡",
    "路考茶", "道奇蓝", "酸橙绿", "金菊黄", "银煤竹", "锖浅葱", "闪光绿", "陶坯黄",
    "青白橡", "马鞍棕", "鸠羽鼠", "鹿皮靴", "鹿皮鞋", "黄土赭", "黄枯茶", "丁子",
    "中紫", "中蓝", "中黄", "乳白", "二蓝", "亜麻", "亮蓝", "今様",
    "代赭", "伽罗", "军蓝", "刈安", "利久", "千草", "国防", "土器",
    "天蓝", "媚茶", "宝蓝", "小豆", "小麦", "山吹", "山鸠", "常盘",
    "弁柄", "弱绿", "抹茶", "新桥", "春绿", "晒黑", "暗紫", "暗红",
    "暗绿", "暗蓝", "暗青", "木兰", "木贼", "朽叶", "李子", "杜若",
    "松叶", "枯茶", "柑子", "栀子", "栗梅", "桃肉", "桔梗", "桦茶",
    "桧皮", "梅染", "橄榄", "橙红", "水绿", "水鸭", "沙棕", "洋李",
    "洋红", "洗朱", "洗柿", "浅灰", "浅緑", "浅绿", "浅缥", "浅葱",
    "浅褐", "浅黄", "浓蓝", "浓鼠", "海松", "淡灰", "淡紫", "淡绿",
    "淡蓝", "淡青", "淡黄", "深橙", "深灰", "深粉", "深緑", "深红",
    "深绯", "深绿", "深蓝", "深青", "火砖", "灭紫", "灰棕", "灰白",
    "灰石", "灰茶", "炼瓦", "焦茶", "焦香", "煤竹", "牡丹", "猩红",
    "玉米", "珊瑚", "琥珀", "瑠璃", "瓮覗", "生壁", "生成", "番木",
    "番茄", "白杏", "白烟", "白緑", "白群", "真红", "真赭", "石竹",
    "石蓝", "硬木", "碧绿", "秘鲁", "粉红", "粉蓝", "紫兰", "紫红",
    "紫绀", "紫黒", "緑青", "緑黄", "红桦", "红梅", "红绯", "红赤",
    "纯白", "纯红", "纯绿", "纯蓝", "纯黄", "纯黑", "绀蓝", "绀青",
    "绿玉", "绿黄", "群青", "老花", "肉桂", "胡桃", "胡粉", "臙脂",
    "芥子", "苏芳", "若竹", "若緑", "若草", "茶鼠", "草绿", "莺茶",
    "菖蒲", "萌葱", "萌黄", "萱草", "葡萄", "蓝铁", "蓝鼠", "蔚蓝",
    "蔷薇", "薄墨", "薄柿", "薄緑", "薄缥", "薄花", "薄钝", "薄鼠",
    "藤紫", "蜂蜜", "蜜柑", "蜜瓜", "褐返", "象牙", "赤丹", "赤橙",
    "赤紫", "赤茶", "赤褐", "赤铜", "赤锖", "踯躅", "退红", "郁金",
    "酸橙", "重褐", "金茶", "金赤", "钢蓝", "铁绀", "铁青", "铅丹",
    "银朱", "银灰", "银白", "银鼠", "锖鼠", "闪兰", "雪白", "露草",
    "青丹", "青磁", "青竹", "青紫", "青緑", "青蓝", "青褐", "青钝",
    "靛青", "韩红", "骆驼", "鲑鱼", "鶸茶", "鸟子", "鸠羽", "鹿皮",
    "麹尘", "黄丹", "黄土", "黄栌", "黄橡", "黄檗", "黄緑", "黄绿",
    "黄茶", "黄褐", "黄赤", "黄金", "黒橡", "黒茶", "黒铁", "丹",
    "人", "卵", "土", "墨", "曙", "朱", "杏", "枯",
    "柳", "柿", "栗", "桃", "桜", "桦", "棕", "橙",
    "水", "油", "浓", "涅", "灰", "狐", "白", "砂",
    "秘", "空", "米", "紫", "緑", "红", "绀", "练",
    "绯", "缥", "肉", "肌", "胜", "花", "苔", "苗",
    "茜", "茶", "草", "莺", "菫", "蓝", "蓟", "薄",
    "藤", "蜜", "蝋", "褐", "赤", "赭", "金", "钝",
    "铁", "铅", "锖", "雪", "青", "革", "饴", "香",
    "鶸", "鸢", "鸨", "黄", "黒", "鼠",
)

# 中文颜色词表（老版 shoe_workflow.COLOR_NAMES :25，逐字照抄，顺序也不能动：
# 正则用 | 拼接，长词必须排在短词前面，否则「咖啡」会被「咖」抢先匹配）。
# 供应商实际写的「修饰 + 颜色」组合词。数据源里的 398 个基词覆盖不到这些
# （数据源按标准色名整理，供应商用的是口语写法），不补就会把「雾霾蓝」拆成
# residue「雾霾」+ 蓝 两个 token，最后被判「认不出颜色」（2026-11-10 实测）。
COLOR_WORDS_COMPOUND = (
    # 程度/质感修饰
    "雾霾蓝", "雾霾灰", "烟灰", "烟蓝", "烟粉", "烟紫", "烟绿",
    "藕粉", "藕荷", "豆沙", "奶白", "奶黄", "奶咖", "奶茶",
    "抹茶绿", "抹茶灰", "抹茶", "牛油果绿", "牛油果", "焦糖",
    "燕麦", "燕麦色", "燕麦灰", "水泥灰", "岩灰", "石墨灰",
    "香槟金", "玫瑰金", "古铜", "古铜色", "琥珀",
    "樱花粉", "蜜桃粉", "珊瑚粉", "珊瑚橘", "珊瑚红",
    "天空蓝", "雾蓝", "冰蓝", "冰白", "月光白", "珍珠白",
    "森林绿", "橄榄绿", "军旅绿", "薄荷绿", "薄荷",
    "酒红", "枣红", "砖红", "铁锈红", "枫叶红",
    "藏蓝", "冥蓝", "宝蓝", "湖蓝", "海蓝", "牛仔蓝",
    "柠檬黄", "芥末黄", "姜黄", "鹅黄", "香芋紫", "葡萄紫",
    "黑兰", "黑蓝", "黑橘", "白绿", "白橘", "白蓝", "白粉",
    "红黑", "蓝白", "灰白", "黑白", "黑红", "黑金", "黑银",
    "米白", "米灰", "米黄", "米咖", "杏色", "杏粉",
)

COLOR_WORDS = (
    # 组合词最长，必须排最前（正则用 | 拼接，长词优先）
    *COLOR_WORDS_COMPOUND,
    # 扩展基词（抹茶/雾霾/藕…）
    *COLOR_WORDS_EXTENDED,
    # 老版常用色字兜底
    "咖啡", "卡其", "藏青", "米白", "乳白", "玫红", "酒红", "天蓝",
    "军绿", "墨绿", "杏", "棕", "褐", "驼", "青", "黑", "白", "灰",
    "红", "橙", "黄", "绿", "蓝", "紫", "粉", "金", "银", "米", "橘",
)


# 颜色前面的程度/材质修饰字。出现在颜色词旁边不构成「认不出」，
# 例如「雾霾蓝」「藕粉」「烟灰」「奶白」「焦糖棕」。
_COLOR_MODIFIER_CHARS = frozenset(
    "雾霾烟藕粉奶抹茶焦糖燕麦水泥岩石墨香槟玫瑰古铜琥珀樱花蜜桃珊瑚"
    "天空冰月珍珠森林橄榄军旅薄荷柠檬芥末姜鹅香芋葡萄牛油果"
    "深浅暗亮荧光明暗淡浓灰白黑红橙黄绿蓝紫金银米杏咖棕褐驼青"
    "彩色高级低调新款款版加厚加绒网面透气防滑软底厚"
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
    # 后置深浅修饰：颜色词 + air/muda(浅) / pekat/tua(深) / susu(奶白)
    # 实测 HD866 kuning air 只收 kuning 会被清成 Kuning，与另一个颜色撞名
    "kuning air", "kuning muda", "kuning pekat", "kuning tua",
    "hitam pekat", "hitam muda", "putih susu", "putih tulang",
    "biru tua", "biru laut", "merah hati", "merah tua", "merah bata",
    "hijau tua", "hijau army", "kelabu tua", "kelabu muda", "perang muda",
    "emas tua", "perak muda", "coklat muda", "coklat tua",
)
MALAY_COLOR = re.compile(
    r"\b(?:" + "|".join(sorted(MALAY_COLOR_WORDS, key=len, reverse=True)) + r")\b", re.I)
# 「可选的型号码前缀 + 完整的马来语颜色短语」整体匹配。
# clean_color_strict 先用它拦一次，命中就直接返回整段短语（保留 air/muda 这种
# 后置深浅修饰）：等 COLOR_CODE 把型号剥掉再说，`kuning air` 就只剩 `kuning` 了。
_MALAY_FULL = re.compile(
    r"^\s*(?:[A-Za-z]*\d+[A-Za-z0-9_*-]*\s+)?"
    r"(" + "|".join(sorted(MALAY_COLOR_WORDS, key=len, reverse=True)) + r")\s*$",
    re.I)

# 颜色 + 款号后缀：白黑色（A19）/ 黑金【A19】/ 蓝红(A19)。
# 括号里是字母数字款号，属于商品身份，清洗时必须整串保留。
_CODE_SUFFIX = re.compile(
    r"^.*[一-鿿].*[（(【\[]\s*[A-Za-z]{0,4}\d{1,6}[A-Za-z0-9-]*\s*[）)】\]]\s*$")

# 型号 / 货号码（老版 :38，逐字照抄）。三条分支各自对应供应商的一种写法：
#   G22 绿色      → 字母+数字
#   1987A 白     → 数字+字母
#   625*- 灰     → 数字带 * 或 # 符号（符号是它「是货号」的唯一证据）
COLOR_CODE = re.compile(
    r"[A-Za-z]+[-_ ]*\d+[A-Za-z0-9_*-]*"
    r"|\d+[-_ ]*[A-Za-z]+[A-Za-z0-9_*-]*"
    r"|\d+[A-Za-z0-9_*-]*[*#]+[-_ ]*"
    # 颜色后面的纯数字型号：`黑色-8001` / `白色_8001` / `灰色 8001`。
    # 供应商用「颜色-款号」区分同色不同批次，款号不是颜色，必须剥掉；
    # 不剥会整条认不出、被当成「非颜色备注」删掉，颜色凭空少一半
    # （2026-11-11 实测：3525595224 八个颜色被砍到四个）。
    r"|[-_ ]+\d{3,}[-_ ]*"
)

# 「型号 + 方括号颜色」：566【白绿色】 / A19(米色) / X-5[黑金]。
# 方括号里就是颜色本身，外面那串是供应商型号。
_MODEL_BRACKET_COLOR = re.compile(
    r"[A-Za-z0-9\-_*#\s]*[（(【\[]\s*(.+?)\s*[）)】\]]")

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
# 括号里是中文颜色的，把颜色留下（566【白绿色】 -> 白绿色）。
# 必须在 _BRACKET_ANY 之前处理：那个正则会把整段括号删掉，只剩 566，
# 于是颜色彻底认不出（2026-11-10 实测：直接抛「颜色全部无法识别」）。
_BRACKET_COLOR = re.compile(
    r"[（(【\[]\s*((?:浅|深|暗|亮|荧光)?(?:" + "|".join(COLOR_WORDS) + r")色?)\s*[）)】\]]")
_BRACKET_ANY = re.compile(r"[（(【\[][^）)】\]]*[）)】\]]")
# 版次词（老版 :205）
# 营销词/俗语：供应商常把卖点塞进颜色值里（「白黑（5.0cm正版）踩屎感」），
# 它们不是颜色，必须删掉，否则 residue 非空直接判「认不出颜色」，
# 整件商品处理失败（2026-11-11 实测：3525319395 三个颜色全挂）。
_STYLE_WORDS = re.compile(
    r"升级版|标准版|新款|款|"
    r"踩屎感|正版|原版|官方|专柜|同款|爆款|网红|热卖|包邮|"
    r"加厚款|特价|限时|断码清仓|工厂直发|一件代发"
)
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

    # 先整体认马来语「型号 + 颜色短语」（HD866 kuning air / HD866 hitam）。
    # 为什么必须在剥型号码之前：COLOR_CODE 会把 HD866 当货号删掉，剩下的
    # `kuningair` 被 MALAY_COLOR 匹配成 kuning，`air`（浅）就丢了 ——
    # 「浅黄」被当成「黄」，与另一个颜色撞名，妙手保存报
    # 「SKU【Kuning-41】不存在」（2026-11-09 实证）。
    # 马来语商品的颜色值本来就是「型号 + 颜色短语」整串（HD866 kuning air），
    # 上游规格表里存的就是这一整串，我们截短成 Kuning air 反而对不上，
    # 妙手会报「SKU【Kuning air-41】不存在」——颜色值必须原样保留型号前缀。
    # 只做去多余空白，不动大小写：HD866 是型号，大写用户自己的写法。
    if _MALAY_FULL.match(original):
        return " ".join(original.split())

    # 「型号 + 方括号颜色」整体形态：566【白绿色】/ 566【黑橘色】/ A19(米色)。
    # 必须在所有剥离之前处理：COLOR_CODE 会先把 566 当货号删掉、末尾 strip
    # 又会把括号剥成半个，两条路都让括号再也配不上
    # （2026-11-10 实测：这两条颜色直接抛「颜色全部无法识别」，整件商品失败）。
    bracketed = _MODEL_BRACKET_COLOR.fullmatch(original.strip())
    if bracketed:
        inner = bracketed.group(1).strip()
        if inner and inner != original.strip():
            try:
                return clean_color_strict(inner)
            except ColorError:
                return inner

    # 括号里的款号/批次标识（白黑色（A19）、黑金【A19】）属于商品身份，
    # 不是备注：妙手草稿的规格表存的就是这一整串，清洗时删掉就对不上，
    # 保存直接报「SKU【白黑色-36】不存在」（2026-11-09 实测）。原样保留。
    if _CODE_SUFFIX.match(original):
        return " ".join(original.split())

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
    text = _BRACKET_COLOR.sub(lambda match: match.group(1), text)
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
    # residue 只由「修饰字」组成时放行：雾霾/烟/藕/抹茶/奶/焦糖 这类是
    # 颜色的程度或材质修饰，不是混进来的工艺/材质异物。
    # 不放过它们的话，「雾霾蓝」「藕粉」这种极常见的供应商写法全部报错
    # （2026-11-10 实测：一件商品两个颜色都认不出，整件入不了仓）。
    leftover = residue.strip(" -_+/、,，配拼撞色")
    if leftover and not all(ch in _COLOR_MODIFIER_CHARS for ch in leftover):
        raise ColorError(f"认不出颜色，需要人工确认：{value!r}")
    return "".join(tokens) + variant


def _derived_value_id(attr_id: Any, label: str) -> str:
    """按「属性 id + 规格值」派生一个稳定 id。

    妙手接受任意非空 attrValueId 作为新规格值（载体路径已实测），
    只要同一个值每次得到同一个 id 就不会来回漂移。
    """
    import hashlib

    seed = "%s|%s" % (str(attr_id or ""), str(label or ""))
    return hashlib.md5(seed.encode("utf-8")).hexdigest()[:10]


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
    dropped_remarks: list[tuple[str, str]] = []
    for index, value in enumerate(values):
        raw = value.get("attrValue")
        try:
            name = clean_color_strict(raw)
        except ColorError as exc:
            # 认不出的颜色值不整件失败：供应商经常在规格里塞厂家备注
            # （「大量拿货可联系」「现货秒发」「厂家直销」），它们根本不是颜色，
            # 是混在规格表里的广告。旧行为是抛错让整件商品处理失败，
            # 一件有 9 个正常色 + 1 条备注的商品会因为那条备注全部作废。
            # 现在删掉这一条规格值（连带它的 SKU 键），其余颜色照常处理。
            dropped_remarks.append((str(raw or "")[:40], str(exc)[:80]))
            continue
        groups.setdefault(name.replace("色", ""), []).append((index, value, name))
    if not groups:
        # 一个颜色都没剩下：这不再是「备注混进来」，而是颜色根本认不出，
        # 必须报错——静默产出无颜色商品，上架时平台会拒，线索更难查。
        detail = "；".join(f"{text}（{why}）" for text, why in dropped_remarks[:3]) or "颜色属性为空"
        raise ColorError(f"颜色全部无法识别，需要人工确认：{detail}")

    # 每组挑一个代表：优先取「原文本来就是这个写法」的那个，改写量最小，
    # 也保证同名 SKU 的展示文案不会因为清洗顺序而漂移（老版 :274）。
    #
    # 顺序上多做一步老版没有的处理：截断按「首次出现顺序」决定留谁（与老版
    # `list(groups.values())[:10]` 一致），但**输出**按原 attrValueList 下标排序。
    # 原因在 pre_save_check 规则 8：它要求保留下来的 id 顺序与原来一致
    # （`after_ids == [v for v in before_ids if v in retained]`），而组代表可能
    # 是组里靠后的那个元素，不重排就会让整件商品被那条规则挡在入库前。
    chosen: list[tuple[int, dict[str, Any]]] = []
    # 值被改写过的原 id -> 新 id。改了值就必须换 id：妙手按
    # 「attrName + attrValue + attrValueId」重建规格表，值写成「米色」却留着
    # 属于「HD866米色」的旧 id，平台按 id 找回报「SKU【米色-41】不存在」，
    # 整件商品存不进去（2026-11-09 实测）。
    id_remap: dict[str, str] = {}
    for candidates in list(groups.values())[:MAX_COLORS]:
        index, value, name = min(
            candidates,
            key=lambda pair: str(pair[1].get("attrValue") or "").strip() != pair[2])
        item = {**dict(value), "attrValue": name}
        original_id = str(value.get("attrValueId") or "")
        original_label = str(value.get("attrValue") or "").strip()
        if original_id and original_label != name:
            # 稳定派生：同一个「属性 + 新值」永远得到同一个 id，重复执行不会漂移
            item["attrValueId"] = _derived_value_id(attr.get("attrId"), name)
            id_remap[original_id] = item["attrValueId"]
        chosen.append((index, item))
    chosen.sort(key=lambda pair: pair[0])
    chosen_values = [item for _, item in chosen]

    colors = [item["attrValue"] for item in chosen_values]
    messages: list[str] = []
    if dropped_remarks:
        messages.append(
            "颜色里混入 %d 条非颜色备注，已剔除：%s" % (
                len(dropped_remarks),
                "、".join(text for text, _ in dropped_remarks[:5])))
    if len(groups) > MAX_COLORS:
        # 老版是直接截断（`list(groups.values())[:10]`），这里保持一致，
        # 但把截断这件事说出来——静默少颜色比报错更难查。
        messages.append(
            f"颜色 {len(groups)} 个超过上限 {MAX_COLORS}，已按顺序保留前 {MAX_COLORS} 个")

    kept_ids = {str(item["attrValueId"]) for item in chosen_values}
    # removed 的口径是「这个颜色被删了」，不是「这个 id 不在输出里」。
    # 值被改写时 id 会换新，旧 id 自然不在 kept_ids 里，但它并没有被删 ——
    # 直接按 id 差集会把它算成删除，全删保护就会误判成「颜色清理会删掉所有
    # 在售 SKU，已停止」，把每一次清洗都拦死（2026-11-09 实测）。
    removed_ids = [value_id for value_id in ids
                   if value_id not in kept_ids and value_id not in id_remap]

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
            # 值改过的颜色换了 id，SKU 键里的旧 id 也要跟着换，
            # 否则键指向一个规格表里已不存在的 id，妙手同样报「SKU 不存在」。
            # SKU 键**保持草稿原来的段顺序**，只把改过值的 id 换掉。
            #
            # 不要重排：妙手保存草稿是按「它自己那份草稿的键」逐字匹配的，
            # 键形如 ;颜色id;尺码id; 还是 ;尺码id;颜色id; 由草稿决定
            # （采集来的数据两种都有）。我们自作主张排序，键就和草稿对不上，
            # 平台报「SKU【白黑色（A19）-36】不存在」（2026-11-09 实测：
            # 产物与草稿的规格、id、颜色值完全一致，唯一差别就是键顺序）。
            # 载体路径能排序是因为那里是**新建**载体、规格表由我们重建；
            # 这里是改已有草稿，必须跟着草稿走。
            parts = [part for part in str(key).strip(";").split(";") if part]
            parts = [id_remap.get(part, part) for part in parts]
            new_key = ";" + ";".join(parts) + ";"
            # 颜色 id 重建后，SKU 记录整条原样搬到新键上 —— 包括
            # shopIdToWarehouseIdAndStockMap。
            # 不能删这个字段：它是「这条 SKU 的库存放在哪个店铺仓库」的归属，
            # 删掉妙手保存时直接报「店铺【xxx】仓库未选择」（2026-11-09 实测）。
            # 它按 warehouseId 记库存，与规格值 id 无关，换键后依然成立。
            sku_map_out[new_key] = sku
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
