"""pytest 配置：确保 server/ 在 sys.path 中，并把数据库隔离到临时目录。

## 为什么必须隔离（2026-11-25）

原来这个文件只做 sys.path 注入，测试就用 `Settings.db_path` 的默认值 ——
也就是**桌面端的真实数据库**：

    C:\\Users\\Administrator\\AppData\\Local\\aohuang-desktop\\app.db

后果有三个，都是实测到的：

1. 测试往真库里灌脏数据。各测试用固定 `app_key`
   （`claim-job-test` / `claim-partial-test` / `price-foreign-key`…），
   第一次跑成功，第二次就撞 `UNIQUE constraint failed: accounts.app_key`
   —— 整套 85 个失败里绝大多数都是这个，跟业务代码无关。
2. 测试会读到你真实的商品、账号、认领记录，结果不可信。
3. 万一哪个测试写了删除逻辑，真实数据就没了。

现在在 import 任何 server 模块**之前**把 `AOHUANG_DATA_DIR` 指向临时目录。
必须在模块顶层设置（不能放进 fixture）：`Settings.data_dir` 是类定义阶段
求值的默认值，等 fixture 跑起来时模块早就 import 完了，改了也不生效。
"""
import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

# --- 数据库隔离 ---------------------------------------------------------
# 只在用户没显式指定时接管，方便临时对某个真实库跑测试（排查问题时这么干）。
if not os.environ.get("AOHUANG_DATA_DIR"):
    _test_data_dir = Path(tempfile.mkdtemp(prefix="aohuang-test-"))
    os.environ["AOHUANG_DATA_DIR"] = str(_test_data_dir)
    # 同时清掉可能残留的显式连接串，否则它优先于 data_dir
    os.environ.pop("AOHUANG_DATABASE_URL", None)

    @atexit.register
    def _cleanup_test_data_dir() -> None:
        shutil.rmtree(_test_data_dir, ignore_errors=True)