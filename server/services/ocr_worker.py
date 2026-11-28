"""OCR 子进程工作器：读 stdin 的图片路径列表，识别后把 JSON 打到 stdout，然后退出。

由 `ocr_runner.run_ocr_batch()` 拉起，一批一进程。

**这个进程必须短命。** 它存在的唯一理由就是：onnxruntime 的原生内存在
进程退出时由内核回收 —— 留在主进程里怎么 GC 都收不回来（见 ocr_runner
的模块注释，三次 OOM 的 anon-rss 恒为 6.5 GB）。
"""
from __future__ import annotations

import json
import sys


def main() -> int:
    raw = sys.stdin.buffer.read()
    try:
        paths = json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return 2
    if not isinstance(paths, list):
        return 2

    out: dict[str, str | dict] = {}
    try:
        from services.image_review_service import _rapidocr_engine

        engine = _rapidocr_engine()
    except Exception as exc:  # noqa: BLE001 - 引擎起不来就整体报错
        print(f"OCR 引擎加载失败：{exc}", file=sys.stderr)
        return 3

    for item in paths:
        path = str(item)
        try:
            result = engine(path)
            lines = [str(t) for t in (getattr(result, "txts", None) or [])
                     if str(t).strip()]
            out[path] = {"text": "\n".join(lines), "lines": lines}
            del result
        except Exception as exc:  # noqa: BLE001 - 单张失败记空文本
            out[path] = {"text": "", "lines": [], "error": type(exc).__name__}

    sys.stdout.buffer.write(json.dumps(out, ensure_ascii=False).encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
