"""PyInstaller 入口：打包成单个 exe 后由 Electron 启动。"""
from __future__ import annotations

import multiprocessing
import os
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()
    # 冻结后 __file__ 在临时目录，数据目录必须显式指定，否则会写进临时目录
    if getattr(sys, "frozen", False) and not os.environ.get("AOHUANG_DATA_DIR"):
        local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        os.environ["AOHUANG_DATA_DIR"] = os.path.join(local, "aohuang-desktop")
    import uvicorn

    # 监听地址与端口都从 settings 算，不在这里写死：
    # 写死 127.0.0.1 的话，`AOHUANG_SHARED_MODE=1` 只对源码启动生效，
    # 用户装的是 exe —— 也就是所有人都在用打包版，开关等于没做。
    # core/config.py 的 listen_host 是唯一事实来源，main.run() 用同一个属性。
    from core.config import get_settings
    from main import pick_port

    settings = get_settings()
    host = settings.listen_host
    # 端口：Electron 通过 AOHUANG_API_PORT 指定首选端口，settings.api_port 会读到它
    # （Settings 的 env_prefix 是 AOHUANG_）；被占用时 pick_port 按 api_port_max 向上
    # 探测，避免打包后直接绑定失败退出。
    port = pick_port(host)
    uvicorn.run("main:app", host=host, port=port, log_level="info")
