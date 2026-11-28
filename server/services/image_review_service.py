"""主图文字识别（本地 OCR）：选中图片前先看图上有没有中文，避免把带中文的图发到海外店铺。

老版是 `image_review.analyze_images()`（Windows 内置 OCR + 中文检测）。新版把它做成
`POST /images/analyze`：只认阿里 CDN 的原图，不下载用户输入的任意 URL。

约束（与老版一致，不可放宽）：
  · 只接受 https + `cbu01.alicdn.com`（同 `_fetch_source` 的来源白名单思路）；
  · 单图上限 15 MB，超时 30 秒，结果按 URL 哈希缓存到 images/ocr/；
  · OCR 走 `powershell.exe`（Windows PowerShell 5.1）——PowerShell 7 不支持 WinRT，
    调用会报 `Operation is not supported on this platform (0x80131539)`；
  · OCR 不可用时返回 error=True + action=review，不抛错阻断整批。
"""
from __future__ import annotations  # noqa: I001

import contextlib
import gc
import logging
import hashlib
import json
import re
import subprocess
import threading
import sys
from collections.abc import Callable, Iterable

from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler

from core.config import get_settings

logger = logging.getLogger("aohuang.image_review")

OCR_TIMEOUT = 30
OCR_MAX_BYTES = 15 * 1024 * 1024
# OCR 并发。RapidOCR 是纯 CPU（onnxruntime），4 核机器上并发超过核数
# 只会互相抢 CPU，反而更慢（实测 4 并发 0.44s/张 比串行 0.36s/张 还慢）。
# 所以这个值跟着 CPU 核数走，不跟上游并发走。
import os as _os  # noqa: E402, I001
OCR_CONCURRENCY = max(2, min(8, (_os.cpu_count() or 4)))
# 缓存版本：OCR 脚本从 StorageFile 改为 ReadAllBytes 后识别结果才可靠，
# 旧的失败缓存（version 2 及以前，全是「本地 OCR 未完成」）必须失效，
# 否则会一直复用「识别不出字」的错误结论，把含中文的图误判成 keep/review。
OCR_CACHE_VERSION = 3
ANALYZE_LIMIT = 20

# 允许识别的图片来源：常见图床 + 阿里 CDN。
# 老版只放 cbu01.alicdn.com，但新版商品的真实主图来自多个图床，
# 因此改成「公网 https + 非内网地址」的白名单式校验（防 SSRF，见 allowed_source）。
ALLOWED_HOSTS = {
    "cbu01.alicdn.com", "img.alicdn.com", "gd1.alicdn.com", "gd2.alicdn.com",
    "gd3.alicdn.com", "gd4.alicdn.com", "img.chengji-inc.com",
    "earth-rt.chengji-inc.com", "static.chengji-inc.com",
}
ALLOWED_SUFFIXES = (".alicdn.com", ".chengji-inc.com", ".taobao.com", ".tmall.com")
# 自有图床：处理阶段已经把上游图转存到这里，OCR 必须认它。
# 不认的后果是「转存过的图全部无法识别」——而处理流程里图片一定会先转存，
# 等于服务器端 OCR 永远不生效（2026-11-09 实测）。
OWN_IMAGE_HOSTS = {"aohuang888.com", "www.aohuang888.com"}
# 明确拒绝的主机名/网段（内网、回环、云元数据）
BLOCKED_HOSTS = {"localhost", "metadata.google.internal", "169.254.169.254"}
CHINESE = re.compile(r"[\u4e00-\u9fff]")
_SCRIPT = Path(__file__).with_name("windows_ocr.ps1")


def _is_private_host(host: str) -> bool:
    """拒绝私有/回环/链路本地地址，避免 OCR 变成 SSRF 探测工具。

    例外：`198.18.0.0/15`（RFC 2544 benchmark 网段）。本机代理把公网域名解析到
    这个网段，若一并拒绝会让所有图床都不可用；该网段由代理转发，不指向内网服务。
    """
    import ipaddress
    import socket

    if host in BLOCKED_HOSTS:
        return True
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except OSError:
        return False
    benchmark = ipaddress.ip_network("198.18.0.0/15")
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if ip.version == 4 and ip in benchmark:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return True
    return False


def _cache_dir() -> Path:
    path = get_settings().data_dir / "images" / "ocr"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def allowed_source(url: str) -> bool:
    parts = urlsplit(str(url or ""))
    if parts.scheme != "https" or parts.username:
        return False
    host = (parts.hostname or "").lower()
    if not host or parts.port not in (None, 443):
        return False
    known = (host in ALLOWED_HOSTS or host in OWN_IMAGE_HOSTS
             or host.endswith(ALLOWED_SUFFIXES))
    if not known:
        return False
    return not _is_private_host(host)


# 厂家/工厂宣传语：这类图出现在跨境商品主图上基本是废图——不是要翻译，
# 是要整张删掉。仅判断「有没有中文」分辨不出来（老版 image_review.FACTORY_PHRASES）。
FACTORY_PHRASES = (
    "厂家直销", "工厂直销", "源头工厂", "实力工厂", "工厂实拍", "工厂展示",
    "生产车间", "生产基地", "厂房展示", "厂家介绍", "工厂介绍", "公司简介",
    "招商加盟", "招收代理", "一件代发", "来图定制", "来样定制",
    # 实测常见但原词表没有的写法（2026-11-09 补）：
    # 「厂家」「工厂」单独出现也算——跨境店铺主图放厂房照没有任何转化价值；
    # 「拿货/批发/代理/定制」是 B2B 用语；联系方式同理，那类图上永远是广告。
    "厂家", "工厂", "厂价", "出厂价", "货源", "拿货", "大量拿货", "批发",
    "诚招代理", "招代理", "代理加盟", "支持定制", "来样加工", "OEM", "ODM",
    "欢迎咨询", "联系电话", "客服电话", "扫码", "二维码", "微信号",
    "加微信", "微信", "WhatsApp", "Quality Assurance", "Best Price",
)

# 尺码表 / 尺码图：这类图混进主图或详情图是废图——尺码对照表要作为
# 「尺码图」单独挂字段，塞在展示图里既占位又误导买家（2026-11-09 负责人提出）。
SIZE_CHART_PHRASES = (
    "尺码对照", "尺码表", "尺寸对照", "尺寸表", "尺码说明", "尺码参考",
    "如何测量", "测量方法", "脚长", "内长", "鞋码对照",
    "size chart", "size guide", "size table", "how to measure",
)
# 尺码表的结构特征：OCR 出来是「35 36 37 …」这种纯数字序列，
# 或者密集出现「码」字。两者占其一就按尺码图处理。
_SIZE_RUN = re.compile(r"(?:\b\d{2}\b[\s,，/\n]*){4,}")


def classify_text(text: str) -> dict[str, Any]:
    """按 OCR 文本给处理建议。

    keep（无中文）/ translate（有中文）/ remove（厂家宣传，整张删）/
    review（空文本，或只识别到一个「一」疑似尺码横线）。
    """
    clean = re.sub(r"\s+", "", text or "")
    chinese = " ".join(CHINESE.findall(text or ""))
    if not clean:
        return {"action": "review", "reason": "图上没识别到文字，可保留或人工看图",
                "hasChinese": None, "chineseText": ""}
    lower = clean.lower()
    hits = [phrase for phrase in FACTORY_PHRASES if phrase.lower() in lower]
    if hits:
        return {"action": "remove", "reason": "疑似厂家宣传：" + "、".join(hits),
                "hasChinese": True, "chineseText": chinese, "factoryPhrases": hits}
    size_hits = [phrase for phrase in SIZE_CHART_PHRASES if phrase.lower() in lower]
    if size_hits or _SIZE_RUN.search(text or ""):
        # 尺码图不进主图/详情图：它是「尺码对照表」专用位，混在展示图里
        # 既占 9 张主图额度，又让买家以为那是商品实拍。
        return {"action": "remove",
                "reason": "疑似尺码对照图：" + ("、".join(size_hits) or "密集尺码数字"),
                "hasChinese": bool(CHINESE.search(text or "")), "chineseText": chinese,
                "sizeChartPhrases": size_hits}
    if set(CHINESE.findall(text or "")) == {"一"}:
        # 尺码表里表示「不可选」的横线常被 OCR 认成「一」，别当中文标图
        return {"action": "review",
                "reason": "仅识别到「一」，可能是尺码横线，请看图复核",
                "hasChinese": True, "chineseText": chinese}
    if CHINESE.search(text or ""):
        return {"action": "translate", "reason": "识别到中文，发海外前建议翻译",
                "hasChinese": True, "chineseText": chinese}
    return {"action": "keep", "reason": "未识别到中文，可直接使用",
            "hasChinese": False, "chineseText": ""}


class _NoRedirect(HTTPRedirectHandler):
    """禁止自动跟随重定向（对齐老版 `image_review.NoRedirect`）。

    urllib 默认 opener 会静默跟随 302/307；`redirect_request` 返回 None 时
    OpenerDirector 走到 http_error_default 抛 HTTPError，调用方就能把
    3xx 当成拒绝而不是当成「下载失败重试」。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _download_cache_path(url: str) -> Path:
    """`_download()` 落盘的位置（不下载，只算路径）。"""
    return _cache_dir() / f"{_key(url)}.image"


def _download(url: str) -> Path:
    """下载原图到缓存目录；已存在则复用。

    不跟随重定向（老版 `NoRedirect(HTTPRedirectHandler)`）：
    `allowed_source` 只校验了**首个** URL 的 host，一旦上游回 302 就换了目标，
    「白名单域名 → 内网地址」的重定向就能绕过白名单（典型 SSRF 跳板）。
    所以这里禁止自动跳转，遇到 3xx 直接拒绝，由用户换直链。
    """
    import urllib.error
    import urllib.request

    directory = _cache_dir()
    image = directory / f"{_key(url)}.image"
    if image.exists() and image.stat().st_size > 0:
        return image
    request = urllib.request.Request(url, headers={"User-Agent": "AOHUANG-Image-Review/1.0"})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=15) as response:  # noqa: S310 - host 已校验且禁跳转
            status = int(getattr(response, "status", 0) or response.getcode() or 0)
            if 300 <= status < 400:
                raise ValueError("图片地址发生重定向，已拒绝")
            content_type = str(response.headers.get("Content-Type") or "")
            if not content_type.startswith("image/"):
                raise ValueError("返回内容不是图片")
            raw = response.read(OCR_MAX_BYTES + 1)
    # urlopen 对 3xx 未必抛错：默认 opener 会静默跟随，自定义 opener 下才可能把
    # HTTPError 抛出来。两条路径都按「重定向即拒绝」处理，别让 302 混成下载失败。
    except urllib.error.HTTPError as exc:
        if 300 <= int(getattr(exc, "code", 0) or 0) < 400:
            raise ValueError("图片地址发生重定向，已拒绝") from exc
        raise ValueError(
            f"图片下载失败（HTTP {getattr(exc, 'code', '?')}），可重试或看图复核") from exc
    if len(raw) > OCR_MAX_BYTES:
        raise ValueError("图片超过 15 MB，无法识别")
    image.write_bytes(raw)
    return image


# OCR 前先把图缩到这个长边（2026-11-28）。
#
# 负责人：「识图需要原图吗，不是压缩再识图吗」—— 对，不需要原图。
# OCR 只判断「图上有没有中文」，1280 长边足够认字；原图动辄 3000×3000
# 解成 numpy 是 27 MB/张，缩到 1280 后不到 5 MB，差 5 倍以上。
# 而且 `analyze()` 要连看 9 张主图，这个差别直接决定会不会把内存打爆。
OCR_MAX_EDGE = 1280
OCR_JPEG_QUALITY = 82


def _prepare_for_ocr(image: Path) -> Path:
    """把原图缩到 `OCR_MAX_EDGE` 再交给引擎；返回压缩后的临时文件。

    为什么要缩（2026-11-28）：
      · 内存 —— onnxruntime 的输入张量按像素算，3000×3000 的 RGB
        就是 27 MB，缩到 1280 只要 4.9 MB；
      · 速度 —— 检测阶段的计算量随像素线性增长，缩图后单张快 2-3 倍；
      · 精度 —— 分类任务只要「有没有字」，1280 长边对中文小字仍可识别。

    缩放失败（图坏了 / 格式怪）就退回原图，不让预处理把整件商品卡住。
    """
    try:
        from PIL import Image

        with Image.open(image) as img:
            img = img.convert("RGB")
            width, height = img.size
            longest = max(width, height)
            if longest > OCR_MAX_EDGE:
                scale = OCR_MAX_EDGE / float(longest)
                img = img.resize(
                    (max(1, int(width * scale)), max(1, int(height * scale))),
                    Image.LANCZOS)
            compact = image.with_name(f"{image.stem}.ocr.jpg")
            img.save(compact, "JPEG", quality=OCR_JPEG_QUALITY, optimize=True)
            return compact
    except Exception as exc:  # noqa: BLE001 - 缩图失败退回原图
        logger.warning("OCR 预处理缩图失败，改用原图 %s：%s", image.name, exc)
        return image


def _script_path() -> Path:
    """定位 windows_ocr.ps1。

    源码模式在 services/ 下；PyInstaller 冻结后 datas 会解到 sys._MEIPASS/services/
    （见 server.spec），`Path(__file__).with_name()` 在两种模式下都指向同一位置。
    """
    candidates = [_SCRIPT]
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        candidates.append(Path(bundle) / "services" / "windows_ocr.ps1")
    for path in candidates:
        if path.is_file():
            return path
    raise ValueError("缺少 windows_ocr.ps1（OCR 脚本未随程序发布），请看图复核")


# RapidOCR 单例。模型加载约 1 秒，之后单张 0.3-0.5 秒（4 核 CPU 实测）。
# 常驻进程里复用同一个引擎，不能每张图都新建。
_RAPIDOCR_ENGINE: Any = None
_RAPIDOCR_LOCK = threading.Lock()

# 引擎调用锁（2026-11-28 修 OOM）。
#
# **必须串行调用同一个引擎。** 原来 `analyze()` 用 4 个线程并发调
# `engine(path)`，看着并行很快，实际 onnxruntime 的 InferenceSession
# 在多线程同时推理时会**每个线程各分配一份动态 workspace**
# （det/cls/rec 三个模型都要），内存随并发线程数成倍增长且用完不归还。
#
# 实测：单件商品走完整流程后进程 RSS 从 223 MB 涨到 1075 MB 并不回落；
# 连续处理 8 件就顶到 6.5 GB 被内核 OOM 杀掉（12:27 / 12:53 / 14:23 三次，
# anon-rss 恒为 6.5 GB）。负责人那几批全报「处理过程中断（服务重启）」。
#
# 单个引擎本来就该串行批量跑 —— 负责人原话：「ocr 为什么会加载这么多，
# 不是只加载一个然后批量看图片吗」。串行后单张 0.35-0.56s，
# 一件商品 9 张图也就 5 秒，相对 35-50 秒的图片翻译完全不是瓶颈。
_RAPIDOCR_RUN_LOCK = threading.Lock()


def _rapidocr_engine() -> Any:
    """惰性加载 RapidOCR（Linux/服务器端的 OCR 后端）。"""
    global _RAPIDOCR_ENGINE
    if _RAPIDOCR_ENGINE is not None:
        return _RAPIDOCR_ENGINE
    with _RAPIDOCR_LOCK:
        if _RAPIDOCR_ENGINE is not None:
            return _RAPIDOCR_ENGINE
        from rapidocr import RapidOCR

        _RAPIDOCR_ENGINE = RapidOCR()
        logger.info("RapidOCR 引擎已加载（服务器端 OCR 后端）")
        return _RAPIDOCR_ENGINE


def _run_ocr_rapidocr(image: Path) -> dict[str, Any]:
    """用 RapidOCR（ONNX Runtime）识别，返回与 Windows OCR 相同的结构。

    为什么服务器端要另做一套：原来只有 Windows 内置 OCR（WinRT），
    Linux 服务器上既没有 pwsh 也没有 WinRT，OCR 必然失败 —— 后果是
    「挑不出含中文的图 → 全部提交翻译」（浪费额度），而且工厂图、
    尺码对照图也识别不了（2026-11-09 实测）。
    RapidOCR 是 PaddleOCR 模型的 ONNX 版本，纯 CPU 跑，实测单张 0.35-0.56s，
    对 35-50s 的图片翻译来说完全可以忽略。

    **识别完立刻释放**（2026-11-28）：RapidOCR 返回的 result 里带着
    检测框坐标数组、中间特征图等，一张 3000px 的图能占几十 MB。
    不显式 `del` + `gc.collect()`，这些会留在堆里等下一次 GC ——
    而 `analyze()` 是连跑 9 张的循环，等不到 GC 就叠满了。
    """
    engine = _rapidocr_engine()
    compact = _prepare_for_ocr(image)
    try:
        # 串行调用同一个引擎（见 _RAPIDOCR_RUN_LOCK 的说明）：
        # 并发推理会让 onnxruntime 每线程各开一份 workspace，内存成倍涨且不归还。
        with _RAPIDOCR_RUN_LOCK:
            result = engine(str(compact.resolve()))
            lines = [str(t) for t in (getattr(result, "txts", None) or [])
                     if str(t).strip()]
            # 用完就地清掉结果对象，别让检测框数组留到下一张
            del result
    finally:
        # 压缩出来的临时图用完即删（原图留着给后面翻译用）
        if compact != image:
            with contextlib.suppress(OSError):
                compact.unlink()
        gc.collect()
    return {"text": "\n".join(lines), "lines": lines, "version": OCR_CACHE_VERSION}


def _run_ocr(image: Path) -> dict[str, Any]:
    """识别一张图：Windows 走系统 OCR，其余平台走 RapidOCR。"""
    if sys.platform != "win32":
        return _run_ocr_rapidocr(image)
    script = _script_path()
    executable = "powershell.exe" if sys.platform == "win32" else "pwsh"
    process = subprocess.run(  # noqa: S603 - 固定参数，无 shell
        [executable, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(script), "-ImagePath", str(image.resolve())],
        capture_output=True, timeout=OCR_TIMEOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    raw = process.stdout or b""
    # powershell.exe 输出编码随系统 locale 变化（中文系统常见 GBK），逐种尝试解码
    text = ""
    for encoding in ("utf-8-sig", "utf-8", "gbk", "cp936", "mbcs"):
        try:
            text = raw.decode(encoding)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if not text:
        text = raw.decode("utf-8", "replace")
    try:
        payload = json.loads(text or "{}")
    except json.JSONDecodeError:
        raise ValueError("本地 OCR 输出无法解析，请看图复核或重试") from None
    if process.returncode or not isinstance(payload.get("text"), str):
        raise ValueError("本地 OCR 未完成，请看图复核或重试")
    payload["version"] = OCR_CACHE_VERSION
    return payload


def recognize(url: str, *, retry: bool = False) -> dict[str, Any]:
    """识别一张图，结果按 URL 缓存。retry=True 时忽略缓存重跑。"""
    if not allowed_source(url):
        raise ValueError("该图片来源暂未接入本地识别（只支持 cbu01.alicdn.com 原图）")
    cached = _cache_dir() / f"{_key(url)}.json"
    if cached.exists() and not retry:
        try:
            data = json.loads(cached.read_text(encoding="utf-8"))
            if data.get("version") == OCR_CACHE_VERSION and isinstance(data.get("text"), str):
                return data
        except (OSError, ValueError):
            pass
    image = _download(url)
    result = _run_ocr(image)
    with contextlib.suppress(OSError):
        # 缓存写不进去只是下次多跑一次 OCR，不能因此让本次识别失败
        cached.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return result


def analyze_one(url: str, *, retry: bool = False) -> dict[str, Any]:
    try:
        result = recognize(url, retry=retry)
    except ValueError as exc:
        return {"action": "review", "reason": str(exc), "hasChinese": None,
                "chineseText": "", "text": "", "error": True}
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        reason = f"本地 OCR 不可用（{type(exc).__name__}），请看图复核"
        return {"action": "review", "reason": reason,
                "hasChinese": None, "chineseText": "", "text": "", "error": True}
    return {**classify_text(str(result.get("text") or "")),
            "text": str(result.get("text") or ""), "error": False}


def analyze(images: Iterable[str], *, limit: int = ANALYZE_LIMIT, retry: bool = False,
            max_workers: int = OCR_CONCURRENCY,
            on_complete: Callable[[int, int], None] | None = None) -> list[dict[str, Any]]:
    """批量识别主图，返回每张的 index/url/action/reason/text。

    ## 走短命子进程（2026-11-28 修 OOM）

    负责人的判断：「OCR 为什么会加载这么多，不是只加载一个然后批量看图片吗」
    「识图需要原图吗不是压缩再识图吗」「识图完成一张不是立马释放一张吗」
    「还有什么分批+子进程回收」—— 全部照做：

      · 压缩：`_prepare_for_ocr` 把长边缩到 1280 再识别（原图 3000px 是 27 MB/张）；
      · 单引擎：子进程内仍是单例，一批 9 张只加载一次模型；
      · 立刻释放：每张识别完 `del result` + `gc.collect()`；
      · 子进程回收：**这一批跑在独立进程里，结束就退出**，onnxruntime 的
        原生内存由内核回收，主进程 RSS 完全不涨。

    只用前三条不够 —— 实测单件处理完主进程 RSS 从 223 MB 涨到 1075 MB
    且不回落，8 件就 OOM（anon-rss 恒 6.5 GB）。原生内存不受 Python GC 管，
    进程不退出就不会归还。所以这里必须走子进程。

    子进程不可用（起不来/超时/崩溃）时退回「待复核」，不阻断商品处理。
    """
    selected = [str(url or "").strip() for url in images if str(url or "").strip()][:limit]
    unique = list(dict.fromkeys(selected))
    if not unique:
        return []

    # 先下载 + 压缩，再把压缩后的路径一次性交给子进程
    prepared: dict[str, Path] = {}
    for url in unique:
        try:
            if not allowed_source(url):
                raise ValueError("该图片来源暂未接入本地识别")
            image = _download(url)
            prepared[url] = _prepare_for_ocr(image)
        except Exception as exc:  # noqa: BLE001 - 单张下载失败不影响其他
            logger.warning("OCR 取图失败 %s：%s", url[:80], exc)

    results: dict[str, dict[str, Any]] = {}
    if prepared:
        from services import ocr_runner

        paths = list(dict.fromkeys(str(p) for p in prepared.values()))
        raw = ocr_runner.run_ocr_batch([Path(p) for p in paths])

        # 把「压缩图路径 → 原文 URL」映射回去
        by_path = {str(p): url for url, p in prepared.items()}
        for path, payload in raw.items():
            url = by_path.get(str(path))
            if not url or not isinstance(payload, dict):
                continue
            text = str(payload.get("text") or "")
            results[url] = {**classify_text(text), "text": text,
                            "error": bool(payload.get("error"))}

    # 没拿到的（下载失败 / 子进程没返）一律按待复核
    for index, url in enumerate(unique, 1):
        results.setdefault(url, {"action": "review",
                                 "reason": "本地 OCR 未完成，请看图复核",
                                 "hasChinese": None, "chineseText": "",
                                 "text": "", "error": True})
        if on_complete:
            on_complete(index, len(unique))

    # 清理压缩临时文件
    for url, path in prepared.items():
        try:
            original = _download_cache_path(url)
            if path != original:
                path.unlink(missing_ok=True)
        except OSError:
            pass

    return [{"index": index, "url": url, **results.get(url, {})}
            for index, url in enumerate(selected, 1)]


def check_local_file(path: Path, *, retry: bool = False) -> dict[str, Any]:
    """复查本地结果图里还有没有中文（老版 image_review 的「译文中文残留」检查）。

    翻译完的图如果仍带中文，直接发到海外店铺就是脏图。这里只做判定，
    不改图、不阻断流程：调用方自己决定是标记待复核还是拒绝。
    """
    try:
        if not Path(path).is_file():
            raise ValueError("结果图文件不存在")
        cached = _cache_dir() / f"local-{_key(str(Path(path).resolve()))}.json"
        if cached.exists() and not retry:
            try:
                data = json.loads(cached.read_text(encoding="utf-8"))
                if data.get("version") == OCR_CACHE_VERSION and isinstance(data.get("text"), str):
                    return {**classify_text(str(data.get("text") or "")),
                            "text": str(data.get("text") or ""), "error": False}
            except (OSError, ValueError):
                pass
        result = _run_ocr(Path(path))
        with contextlib.suppress(OSError):
            cached.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    except ValueError as exc:
        return {"action": "review", "reason": str(exc), "hasChinese": None,
                "chineseText": "", "text": "", "error": True}
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        return {"action": "review",
                "reason": f"本地 OCR 不可用（{type(exc).__name__}），请看图复核",
                "hasChinese": None, "chineseText": "", "text": "", "error": True}
    text = str(result.get("text") or "")
    return {**classify_text(text), "text": text, "error": False}


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {"keep": 0, "translate": 0, "review": 0}
    for row in rows:
        action = str(row.get("action") or "review")
        counts[action] = counts.get(action, 0) + 1
    return {
        "total": len(rows),
        "keep": counts.get("keep", 0),
        "translate": counts.get("translate", 0),
        "review": counts.get("review", 0),
        "withChinese": sum(1 for row in rows if row.get("hasChinese") is True),
        "errors": sum(1 for row in rows if row.get("error")),
    }
