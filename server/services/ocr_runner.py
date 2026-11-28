"""OCR 子进程执行器：每批识别跑在独立进程里，结束即退出，内存全部归还。

## 为什么必须用子进程（2026-11-28）

负责人的判断是对的：「还有什么分批+子进程回收」。

onnxruntime 的推理会分配**原生内存**（输入张量、中间特征图、
各线程的 arena），这部分内存：
  · Python 的 `gc.collect()` 管不了；
  · 进程不退出就不会归还给操作系统（glibc 只把空闲块留在 arena 里）。

实测证据：
  · 单件商品处理完，进程 RSS 从 223 MB 涨到 1075 MB，**结束后不回落**；
  · 连续处理 8 件就顶到 6.5 GB 被内核 OOM 杀掉；
  · 三次 OOM（12:27 / 12:53 / 14:23）的 anon-rss 恒为 6.5 GB。

在同一个进程里怎么调并发、怎么 `del`、怎么 `gc` 都拦不住这个增长。
唯一可靠的办法是**把 OCR 放进短命子进程**：进程退出时内核回收它的
全部内存，主进程 RSS 不受影响。

## 设计

  · 主进程把「要识别的图片路径」交给子进程，子进程跑完 OCR 输出 JSON 后退出；
  · 一批（默认 9 张）一个子进程，跑完即退 —— 内存峰值封顶在「一个引擎 + 9 张图」；
  · 子进程崩溃/超时不影响主服务，只让这一批退回「待复核」；
  · 子进程里仍然保留单例，避免同一批 9 张图重复加载模型。

## 代价

每个子进程要重新加载一次 ONNX 模型（约 1.5 秒）。一批 9 张图多花 1.5 秒，
相对图片翻译 35-50 秒/张完全可接受，而且换来了内存绝对可控。
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 子进程里跑 OCR 的入口脚本
_WORKER = Path(__file__).with_name("ocr_worker.py")


def run_ocr_batch(paths: list[Path], *, timeout: float = 180.0) -> dict[str, Any]:
    """在一个短命子进程里识别一批图片。

    返回 `{路径字符串: {"text": str, "lines": [...]}}`。
    子进程失败时返回 `{}`，调用方按「OCR 不可用」处理（标记待复核，不阻断）。
    """
    if not paths:
        return {}
    payload = json.dumps([str(p) for p in paths], ensure_ascii=False)
    try:
        proc = subprocess.run(  # noqa: S603 - 固定参数，input 走 stdin
            [sys.executable, str(_WORKER)],
            input=payload.encode("utf-8"),
            capture_output=True,
            timeout=timeout,
            text=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning("OCR 子进程超时（%ss），本批退回待复核", timeout)
        return {}
    except Exception as exc:  # noqa: BLE001 - 子进程起不来也不能让主服务挂
        logger.warning("OCR 子进程启动失败：%s", exc)
        return {}

    if proc.returncode != 0:
        stderr = (proc.stderr or b"").decode("utf-8", "replace")[-400:]
        logger.warning("OCR 子进程退出码 %s：%s", proc.returncode, stderr)
        return {}
    try:
        data = json.loads((proc.stdout or b"").decode("utf-8", "replace"))
    except json.JSONDecodeError:
        logger.warning("OCR 子进程输出无法解析")
        return {}
    return data if isinstance(data, dict) else {}
