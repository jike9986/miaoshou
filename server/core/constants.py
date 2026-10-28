"""站点、语言、上限常量。替代旧 web_domain.py 的字典部分。"""
from __future__ import annotations

APP_NAME = "奥皇上品"
APP_NAME_EN = "AOHUANG Listing"
SERVER_NAME = "aohuang-listing-server"


def _package_version() -> str:
    """优先读仓库根 package.json，保证与安装包版本一致（审查 P2-9）。

    打包后（PyInstaller）通常读不到该文件，回退到下面的默认值。
    """
    import json
    from pathlib import Path

    for parent in Path(__file__).resolve().parents:
        candidate = parent / "package.json"
        if not candidate.is_file():
            continue
        try:
            value = str(json.loads(candidate.read_text(encoding="utf-8")).get("version") or "")
        except (OSError, ValueError):
            continue
        if value:
            return value
    # 打包后读不到仓库里的 package.json，用这里的值兜底。
    # 必须和 app/package.json、web/package.json 的 version 手动保持一致：
    # 兜底值偏小会让已装的新版永远判定「有更新」，侧栏挂一个永不消失的角标。
    return "0.1.5"


APP_VERSION = _package_version()

UPDATE_MANIFEST_URL = "https://aohuang888.com/tk-product-automation/update/version.json"
UPDATE_HOST_WHITELIST = {"aohuang888.com", "www.aohuang888.com"}
UPDATE_CHANNEL = "stable"

# 站点 → 该站点可接受的文字语言（第一个为默认）
SITE_LANGUAGES: dict[str, list[str]] = {
    "MY": ["ms", "en"],
    "SG": ["en", "zh"],
    "PH": ["en"],
    "TH": ["th"],
    "VN": ["vi"],
    "ID": ["id"],
}
SITE_NAMES = {
    "MY": "马来西亚",
    "SG": "新加坡",
    "PH": "菲律宾",
    "TH": "泰国",
    "VN": "越南",
    "ID": "印尼",
}
# 发布允许的文字语言：api/auth.py 的 languages 与 publish_service.check_language
# 的全局兜底都读它，顺序与下面的 LANGUAGE_LABELS 对齐，方便人眼逐行比对。
# 硬约束：必须覆盖 SITE_LANGUAGES 里出现的每一个值。漏一个，那个站点的商品在
# 发布时就会被「语言只能是 …」直接 400 拒掉——印尼站（ID → id）正是这么被误杀的：
# 标签表里一直写着「印尼语」，白名单里却没有 id。
SUPPORTED_LANGUAGES = ["ms", "en", "th", "vi", "id", "zh"]
LANGUAGE_LABELS = {
    "ms": "马来语",
    "en": "英语",
    "th": "泰语",
    "vi": "越南语",
    "id": "印尼语",
    "zh": "简体中文",
    "zh-tw": "繁体中文",
}

# 图片与商品上限
MAIN_IMAGE_LIMIT = 9
DETAIL_IMAGE_LIMIT = 9
DETAIL_IMAGE_HARD_LIMIT = 30
IMAGE_CONCURRENCY = 3
COLLECT_BOX_MAX_ITEMS = 100
# 妙手上游单次最多给 20 条，但本服务会按页向上游翻，直到凑够一页。
# 因此对外每页上限放开到 100（= COLLECT_BOX_MAX_ITEMS），与 API 层 le=100 对齐。
# 以前这里锁 20，导致界面只敢给 10 / 20 两个选项，一页看不了几件。
COLLECT_BOX_PAGE_MAX = 100
# 打上游时每页仍按妙手上限 20 去要，避免被上游拒绝
COLLECT_BOX_UPSTREAM_PAGE = 20
# 仓库容量上限。**这是提示线，不是自动清理线**：
# `warehouse_service.stats()` 用它算 remaining / nearLimit 让界面提前提示，
# 但没有任路径会自动删商品——`trim_overflow()` 默认拒绝执行，理由见其 docstring。
# 之所以仍然有上限：SQLite 单库到几千件后列表查询与备份都会明显变慢，
# 而用户删东西的成本只是「再挑一次」，删错东西的成本是整个月的活白干。
WAREHOUSE_MAX_ITEMS = 2000
# 到这条线（80%）界面开始提示「快满了」，留 400 件的缓冲给用户安排清理
NEAR_LIMIT_RATIO = 0.8
PUBLISH_MAX_ITEMS = 200
TITLE_MIN_LENGTH = 25
TITLE_MAX_LENGTH = 255
WORD_LIMIT = 500

# 图片翻译计费（毫央）
UNIT_MILLIYUAN = 24
IMAGE_BUDGET_MILLIYUAN = 10000
IMAGE_MAX_BYTES = 15 * 1024 * 1024
IMAGE_MODEL = "gpt-image-2"
IMAGE_SIZES = ("1:1", "16:9", "9:16")
IMAGE_RATE_LIMITS = {"minute": 600, "hour": 6000, "day": 60000}

# 分组色板
GROUP_COLORS = ("slate", "rose", "amber", "emerald", "sky", "violet")
GROUP_COLOR_DEFAULT = "slate"

# 定时任务
FREQUENCIES = ("daily", "weekly", "monthly", "range")
SCHEDULE_MAX_RANGE_DAYS = 180
SCHEDULE_MAX_ATTEMPTS_PER_DAY = 3

# 妙手上限
MIAOSHOU_SHOP_PAGE_SIZE = 100
MIAOSHOU_COLLECT_PAGE_SIZE = 20
UPSTREAM_PACE_SECONDS = 1.5
UPSTREAM_RATE_LIMIT_RETRIES = 5
UPSTREAM_RATE_LIMIT_BACKOFF = 1.5

# 系统
HEALTH_INTERVAL_MS = 200          # 前端/Electron 探活轮询间隔
SLOW_REQUEST_MS = 800             # 慢请求日志阈值（main.py request_context 使用）
CATEGORY_STALE_SECONDS = 7 * 86400
