"""pytest 基础设施：隔离数据目录 + 把 _meas 探针当测试跑。

两个原则：

1. **绝不碰真实 app.db。** 任何 import 了 ``db.session`` 的测试都会按
   ``AOHUANG_DATA_DIR`` 去建库；忘了隔离就会在开发机的真实数据目录里建表、
   写种子数据，甚至把探针造的假商品留在运营的仓库里。这里在 session 级
   就把数据目录钉到临时目录，并在结束时断言真实 app.db 没被写过。

2. **探针逻辑一行都不重写。** ``_meas/`` 里那 20 个探针已经是踩过坑的回归
   资产（合计 800+ 条断言），重写等于把「已验证」降级成「刚写的」。所以这里
   只做搬运：子进程跑探针、看退出码、失败时把 FAIL 明细捞出来。

为什么用子进程而不是同进程 import：
  · 探针在模块顶层就 ``init_db()`` / ``Base.metadata.create_all()``，并且大量
    monkeypatch 全局函数（``schedule_service.pick_products``、``image_upload
    .upload_image``），同进程跑会互相串状态，一个探针的 patch 泄漏到下一条；
  · ``core.config.get_settings`` 是 ``lru_cache`` 的，进程内第一次算出的
    db_path 之后改环境变量也变不回来——同进程跑等于所有探针共用一个库；
  · 探针自己会 ``sys.path.insert``、设 ``AOHUANG_DATA_DIR``，这些在父进程里
    做会污染 pytest 自己的解释器状态。
代价是每条用例多 ~1s 启动时间，20 条约 20s，值得。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

SERVER_ROOT = Path(__file__).resolve().parents[1]
MEAS_DIR = SERVER_ROOT / "_meas"

# 真实数据目录的候选位置：测试结束要确认这些地方没被写过。
# 只做「事后检查」而不是「事前拦截」——事前拦截要靠 core.config 配合，
# 那是业务代码，不该为测试让路。
_REAL_APP_DB_CANDIDATES = (
    SERVER_ROOT / "app.db",
    Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "aohuang-desktop" / "app.db",
    Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "tk-product-automation" / "app.db",
)

# --- 数据目录隔离：必须在 conftest 被导入时就生效 --------------------------
# 时机很关键：pytest 收集阶段会 import 测试模块，而 ``import main`` 会连带
# import ``db.session``，后者在**模块顶层**就把 engine 的 sqlite 路径算死了
# （见 db/session.py 的 ``engine = create_engine(f"sqlite:///{_settings.db_path}")``）。
# 等到 fixture 里再设环境变量已经晚了——engine 指向的还是真实数据目录。
# 所以放在这里，collect 之前就执行。
_ISOLATED_ROOT = Path(tempfile.mkdtemp(prefix="aohuang-pytest-"))
os.environ["AOHUANG_DATA_DIR"] = str(_ISOLATED_ROOT)
# 老变量名也一起设：几个探针会 pop 掉它，但不设的话
# core.config._default_data_dir 会去读 LOCALAPPDATA，等于没隔离。
os.environ["TK_AUTOMATION_DATA_DIR"] = str(_ISOLATED_ROOT)
# 图床一律不许真连：没配地址时 image_upload 会走 SFTP 兜底，
# 而 SFTP 凭据是「桌面雨云.txt」——绝不能在测试里碰。
os.environ.pop("IMAGE_UPLOAD_URL", None)


def _snapshot_real_dbs() -> dict[str, tuple[int, float]]:
    """记录真实 app.db 的 (大小, mtime)；不存在就不记。"""
    state: dict[str, tuple[int, float]] = {}
    for path in _REAL_APP_DB_CANDIDATES:
        try:
            stat = path.stat()
        except OSError:
            continue
        state[str(path)] = (stat.st_size, stat.st_mtime)
    return state


@pytest.fixture(scope="session")
def isolated_data_dir() -> Path:
    """整个测试会话共用的临时数据目录，**结束不清理**。

    失败时要能进去看现场（探针写的 sqlite、生成的图片、probe 自己的 json），
    自动 rmtree 会把唯一能解释失败的东西删掉。路径会打在用例头部，
    机器重启后系统自己清临时目录，不需要我们操心。

    目录与环境变量在**模块导入时**就设好了（见文件末尾的 _ISOLATED_ROOT），
    这个 fixture 只负责把路径交给用例。
    """
    print(f"\n[conftest] 测试数据目录（不自动清理）：{_ISOLATED_ROOT}")
    return _ISOLATED_ROOT


@pytest.fixture(scope="session", autouse=True)
def _guard_real_app_db(isolated_data_dir: Path) -> None:
    """会话级守门：测试前后真实 app.db 必须一字未变。"""
    before = _snapshot_real_dbs()
    yield
    after = _snapshot_real_dbs()
    touched = [path for path, state in after.items() if before.get(path) != state]
    assert not touched, (
        "测试写到了真实 app.db：" + ", ".join(touched) +
        f"\n本会话的隔离数据目录是 {isolated_data_dir}；"
        "有测试绕过了 isolated_data_dir，请检查它是不是自己设了 AOHUANG_DATA_DIR。"
    )


def run_probe(module_name: str, *, timeout: int = 300) -> None:
    """跑 ``_meas/<module_name>.py``，退出码非 0 就 fail 并贴出 FAIL 明细。

    module_name 不带 ``.py``，例如 ``probe_bootstrap``。

    失败信息里必须能直接看到「哪几条挂了」：探针把明细写进同目录的
    ``<module>.json``（各探针格式略有不同，见下），但真正的证据在 stdout——
    探针会逐条打印 ``PASS/FAIL <name>  [detail]``。所以两处都捞：
      · stdout 里的 FAIL 行（最直接，带探针自己写的 detail）；
      · 同目录 json 里的失败条目（有些探针的 FAIL 只落盘不打印）。
    """
    path = MEAS_DIR / f"{module_name}.py"
    if not path.is_file():
        pytest.fail(f"探针不存在：{path}")

    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # 子进程必须有自己能写的临时数据目录：探针里多数会自己再建一个，
    # 少数（probe_gate / test_pre_save）依赖父进程传下来的环境。
    env.setdefault("AOHUANG_DATA_DIR", tempfile.mkdtemp(prefix="aohuang-probe-"))
    env.setdefault("TK_AUTOMATION_DATA_DIR", env["AOHUANG_DATA_DIR"])
    env.pop("IMAGE_UPLOAD_URL", None)

    completed = subprocess.run(
        [sys.executable, str(path)],
        cwd=str(SERVER_ROOT),      # 探针里有按相对路径找 _meas/*.json 的
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if completed.returncode == 0:
        return

    pytest.fail(_failure_report(module_name, completed), pytrace=False)


def _failure_report(module_name: str, completed: subprocess.CompletedProcess) -> str:
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    lines = [f"探针 {module_name} 退出码 {completed.returncode}"]

    fails = [line.strip() for line in stdout.splitlines() if _is_fail_line(line)]
    if fails:
        lines.append("--- 探针 stdout 里的 FAIL ---")
        lines.extend(fails[:40])
        if len(fails) > 40:
            lines.append(f"... 还有 {len(fails) - 40} 条")

    json_fails = _json_failures(module_name)
    if json_fails:
        lines.append(f"--- {MEAS_DIR.name}/{module_name}.json 里的失败条目 ---")
        lines.extend(f"  · {entry}" for entry in json_fails[:40])

    if not fails and not json_fails:
        # 探针在跑到 check 之前就崩了（import 错、语法错、数据目录不可写）
        lines.append("--- stdout 尾部 ---")
        lines.extend(stdout.splitlines()[-30:])
        lines.append("--- stderr 尾部 ---")
        lines.extend(stderr.splitlines()[-30:])
    return "\n".join(lines)


def _is_fail_line(line: str) -> bool:
    """判断这一行是不是探针自己打的失败行。

    探针的打印形态有两种：``FAIL <name>  [detail]`` 和 ``  FAIL <name>: ...``。
    只认「行首（去缩进后）就是 FAIL」的那种，否则会把汇总行
    ``TOTAL 2 PASS 2 FAIL`` 也算进来——那行没有名字，贴进报告只会干扰视线。
    """
    return line.strip().startswith("FAIL")


def _json_failures(module_name: str) -> list[str]:
    """从探针的 json 结果里捞出失败条目；格式不认就返回空。

    三种已知形态：
      · {"cases": [{"name", "ok"/"result", "detail"}]}  —— 新版探针
      · [{"case", "result", "detail"}]                  —— probe_color
      · {"summary": {"failed": [...]}}                  —— probe_building
    """
    path = MEAS_DIR / f"{module_name}.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []

    rows: list[dict] = []
    if isinstance(data, list):
        rows = [row for row in data if isinstance(row, dict)]
    elif isinstance(data, dict):
        cases = data.get("cases") or data.get("checks")
        if isinstance(cases, list):
            rows = [row for row in cases if isinstance(row, dict)]

    out: list[str] = []
    for row in rows:
        ok = row.get("ok")
        if ok is None:
            ok = row.get("result") == "PASS" or row.get("pass") is True
        if ok:
            continue
        name = row.get("name") or row.get("case") or "?"
        detail = row.get("detail") or row.get("trace") or ""
        out.append(f"{name}  [{str(detail)[:400]}]")

    if isinstance(data, dict):
        failed = (data.get("summary") or {}).get("failed")
        if isinstance(failed, list):
            out.extend(str(item) for item in failed)
    return out


@pytest.fixture(scope="session")
def probe_runner(isolated_data_dir: Path):
    """把 ``run_probe`` 包成 fixture，测试里拿到就能跑。"""
    def _runner(module_name: str) -> None:
        run_probe(module_name)

    return _runner
