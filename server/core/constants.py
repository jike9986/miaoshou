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
    return "0.1.45"


APP_VERSION = _package_version()

UPDATE_MANIFEST_URL = "https://aohuang888.com/miaoshou-downloads/version.json"
# 更新包允许的下载域名（服务端侧，配合 app/src/updateDownload.ts 的客户端白名单）。
#
# 2026-11-20：负责人买了 `aohuang888.xyz` 专门做安装包下载 CDN。
# 主域 `aohuang888.com` 必须保留 —— 同一台 nginx 上还跑着 newapi /
# aohuang-ai / aohuang888.cn / paytest 多个业务，不能动它的 NS；
# 而老版本客户端只认主域，清单里若只给 .xyz 地址，那些客户端会拒绝下载。
UPDATE_HOST_WHITELIST = {
    "aohuang888.com",
    "www.aohuang888.com",
    "aohuang888.xyz",
    "www.aohuang888.xyz",
}
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
IMAGE_CONCURRENCY = 1000
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
IMAGE_BUDGET_MILLIYUAN = 100000
IMAGE_MAX_BYTES = 15 * 1024 * 1024
# 图片翻译用的模型。2026-11-09 实测（每个模型各 3 次，真实商品图）：
#   gpt-image-2    平均 56.7s（波动 46s），文案偏直译
#   gpt-image-2.5  平均 45.8s（波动 39s），文案最好但不稳定
#   gpt-image-2.6  平均 36.0s（波动 17s），文案自然，**最快也最稳**
#   gemini-3.1-flash 平均 28s 但输出乱码（TIIDAL RAMIER），不可用
# 固定用 2.6：批量跑时稳定性比单次文案差异更重要。
IMAGE_MODEL = "gpt-image-2.6"
IMAGE_SIZES = ("1:1", "16:9", "9:16")
IMAGE_RATE_LIMITS = {"minute": 600, "hour": 6000, "day": 60000}

# 分组色板
GROUP_COLORS = ("slate", "rose", "amber", "emerald", "sky", "violet")
GROUP_COLOR_DEFAULT = "slate"

# 定时任务
FREQUENCIES = ("once", "daily", "weekly", "monthly", "range")
SCHEDULE_MAX_RANGE_DAYS = 180
SCHEDULE_MAX_ATTEMPTS_PER_DAY = 3

# 妙手上限
MIAOSHOU_SHOP_PAGE_SIZE = 100
MIAOSHOU_COLLECT_PAGE_SIZE = 20
# 同账号两次上游请求之间的最小间隔。
#
# 2026-11-18 逐档实测（账号 9，每档 5 件，看总耗时）：
#   1.50s → 36.1s 均值
#   1.20s → 30.1s
#   1.15s → 25.9s
#   1.10s → 24.9s
#   1.05s → 27.3s
#   1.00s → 26.7s
#   0.95s → 27.9s
#   0.90s → 26.8s
#   0.80s → 25.7s
#   0.50s → 18.7s
#   0.00s → 16.7s  ← 最快
#
# 结论：**固定间隔是纯等待**。妙手的接口响应本身就要 1.3~5.3 秒，
# 串行调用时两次请求的天然间隔已经远超它的 QPS 窗口，再额外 sleep 只是白等。
# 20 件压测（interval=0）：65s，20/20 成功，平均 3.25s/件，零失败。
#
# 所以默认不再人为加间隔。但保留 Pacer 的自适应保护：万一某次撞到
# accountApiQpsRateLimit（并发场景、或妙手日后收紧），penalize() 会逐步把
# 间隔抬起来，连续成功后再降回去 —— 不需要重新发版调参。
#
# 但「直接设 0」在 20 件规模下不稳定：实测跑完自适应机制把间隔自己抬到了 1.05s
# （Pacer.penalize 在撞限流时上调），总耗时反而涨到 115.8s —— 中途反复撞限流+退避。
# 5 件小批看到的「0 最快」是样本小、没撞上。
#
# 最终取值 1.05s：这是自适应收敛后的实际稳定值，也是固定值测试里最快的档位
# （5 件 22.6s，折合 4.5s/件）。仍然保留 penalize/reward，妙手收紧时能自动抬升。
#
# 2026-11-19 复测（负责人要求再压一档）：
#   上面那张表里 1.00s(26.7s) / 0.95s(27.9s) / 0.90s(26.8s) 与 1.05s(27.3s)
#   在 5 件样本下差异在噪声内（±1.5s，样本只有 5 件）。
#   但真实批量（50 件）的节流代价是线性的：每件 4 个上游请求，
#   1.05 → 0.90 每件省 0.6s，50 件省 30s；100 件省 60s。
#
#   风险边界很清楚：妙手的 accountApiQpsRateLimit 是**每账号**的。
#   撞上会走 rate_limit 分支 —— penalize() 把间隔自动抬回 1.5s，
#   同时退避重试，不会丢数据。所以调低是「有机会更快，最坏退回原速」。
#
#   取 0.90：留 0.15s 余量到 1.05 这个历史稳定值。不再往下压到 0.80 ——
#   那张表里 0.80 的 25.7s 是 5 件样本，样本量不足以下结论，
#   而 50 件批量的限流风险是实打实的。
#
# === 2026-11-20 复测（负责人追问「故意等的 0.9 秒真的需要这么久吗」）===
#
# 直接回答：**需要，但真正的原因不是「妙手限流」，是「妙手服务端会排队」。**
#
# 三组实测（同一个公共箱载体，同一组 6 条链接，每轮间隔 4s）：
#
#   interval=0.90  总 6.16s   客户端睡 4.16s   上游 2.00s   6/6 成功
#   interval=0.00  总 5.46s   客户端睡 0.00s   上游 5.46s   6/6 成功
#   interval=0.35  总 6.07s   客户端睡 0.58s   上游 5.49s   6/6 成功
#
# 关掉节流只快 12%，因为**上游自己变慢了 2.7 倍**（2.00s → 5.46s）。
# 妙手收到密集请求时会在服务端排队，客户端不睡就得在等响应时睡 ——
# 只是把等待挪了个地方。「故意白等」这个说法不成立。
#
# 另外单独确认：`编辑公共箱` 这条路径不需要 0.90 那么久。
# 用 edit 接口单独测（不掺其他请求）：
#   interval=0.90 → 6/6 成功，平均 0.95s
#   interval=0.20 → 6/6 成功，平均 0.84s
#   interval=0.00 → 6/6 成功，平均 0.95s
# 也就是说单看 edit，0.20 也能跑通且略快。0.90 是为**混合请求**
# （采集箱列表 + 详情 + edit + 复制）留的余量，不是 edit 本身需要。
# 压测还抓到过真实的限流响应「请求太频繁，请稍后重试」（见 _is_rate_limit
# 的注释），所以完全不节流在批量场景有风险，不做。
UPSTREAM_PACE_SECONDS = 0.90
# 自适应退避的上下限：撞限流逐步抬到 1.5s，连续成功再慢慢降回 0.90s。
#
# 2026-11-20 修正：MIN 原来也是 0.90，等于 MIN==默认值 ——
# `reward()` 里 `interval > UPSTREAM_PACE_MIN_SECONDS` 永远为假，
# 自适应降速**从未生效过**。真正被 penalize 抬到 1.5s 之后，
# 要连续成功 50 次 × 每次降 0.05s = 1000 次请求才能爬回 0.90。
# 现在 MIN 降到 0.55：penalize 抬上去之后能在合理次数内回落，
# 而且日常跑会慢慢收敛到 0.55（约快 40%）。
# 下限没取 0.20：那只在「纯 edit」场景验证过，混合请求没测，留余量。
UPSTREAM_PACE_MAX_SECONDS = 1.5
UPSTREAM_PACE_MIN_SECONDS = 0.55
UPSTREAM_RATE_LIMIT_RETRIES = 5
UPSTREAM_RATE_LIMIT_BACKOFF = 1.5
# 妙手网关的瞬时失败（「下游服务响应超时」「下游服务繁忙」）：
# HTTP 200 + result=fail，隔一两秒重试就好，不该判成商品数据问题。
#
# 2026-11-19 修：原来 GATEWAY_RETRIES=3 + 线性退避 1.2s，
# 累计只争取到 7.2 秒，而那次妙手网关抖了约 3 分钟 —— 重试窗口远短于故障时长。
# 现在改成 5 次 + 指数退避（1.5/3/6/12/24，累计约 46 秒），
# 更长的抖动交给下方「网关熔断 + 件级重试队列」处理。
GATEWAY_RETRIES = 5
GATEWAY_RETRY_BACKOFF = 1.5
GATEWAY_RETRY_BACKOFF_MAX_SECONDS = 24.0

# 传输层熔断：连不上 / 超时。
# 上游不可达时 httpx 每次都要等满 request_timeout（30s），用户点一下「采集箱」
# 就得干等十几秒才看到错误。连续 FAIL_THRESHOLD 次传输失败后进入 OPEN，
# COOLDOWN 秒内直接毫秒级快速失败，冷却结束放一个探针试通。
# 只对「连不上/超时」计数；业务错误（凭据错、参数错）不计。
BREAKER_FAIL_THRESHOLD = 3
BREAKER_COOLDOWN_SECONDS = 60.0

# 网关级抖动熔断（HTTP 200 + result=fail）。
#
# 为什么必须单独一套：传输层熔断只认 httpx 异常，而「下游服务响应超时」
# 是规规矩矩的 HTTP 200，旧代码拿到响应就 record_success() 把计数清零，
# 熔断器全程没动作。2026-11-19 那次抖 3 分钟，每件商品各自白等 7.2 秒，
# 50 件一次性全废。
#
# 现在：连续 3 次网关失败 → 整个账号的请求停 30 秒（指数退避到 300 秒上限）。
# 抖动期的请求毫秒级快速失败，由 carrier_service 的件级重试队列
# 排到本批队尾重跑，不再让每件都完整走一遍退避。
GATEWAY_FAIL_THRESHOLD = 3
GATEWAY_COOLDOWN_SECONDS = 30.0
GATEWAY_COOLDOWN_MAX_SECONDS = 300.0

# 件级重试队列：单件载体创建/写入失败时排到本批队尾重试，不拖垮整批。
#
# 为什么要它（负责人 2026-11-19 明确）：「哪个失败了就把这个成品放到这一批
# 从最后重试，因为成品仓库里面肯定都是没问题的」。
# 旧行为是 collect_carriers 里任何一件抛错就 raise，50 件全废。
CARRIER_ITEM_RETRY_ROUNDS = 3
# 每轮之间整批停一下再跑：给妙手网关恢复的时间，也让熔断冷却先走完。
CARRIER_ITEM_RETRY_PAUSE_SECONDS = 20.0

# 系统
HEALTH_INTERVAL_MS = 200          # 前端/Electron 探活轮询间隔
SLOW_REQUEST_MS = 800             # 慢请求日志阈值（main.py request_context 使用）
CATEGORY_STALE_SECONDS = 7 * 86400
