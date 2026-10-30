# -*- mode: python ; coding: utf-8 -*-
"""把 FastAPI 后端打成单目录包，随 Electron 安装。

onedir 而不是 onefile：启动快，且杀毒软件对 onefile 自解压更敏感。
"""
from PyInstaller.utils.hooks import collect_all, collect_submodules
from pathlib import Path

datas, binaries, hidden = [], [], []
for pkg in (
    "uvicorn", "fastapi", "pydantic", "sqlalchemy", "alembic", "psycopg",
    "psycopg_binary",
    "httpx", "cryptography", "anyio",
):
    try:
        d, b, h = collect_all(pkg)
    except Exception:
        continue
    datas += d
    binaries += b
    hidden += h

hidden += collect_submodules("services") + collect_submodules("api") + collect_submodules("models")

# 主图 OCR 用的 PowerShell 脚本要一起打进去：image_review_service 用
# Path(__file__).with_name("windows_ocr.ps1") 找它，打包后 = sys._MEIPASS/services/。
datas += [("services/windows_ocr.ps1", "services")]

# Alembic loads revision files from disk when PostgreSQL startup checks heads.
# Ship them as runtime data beside the frozen server modules.
datas += [
    (str(path), str(path.parent))
    for path in Path("migrations").rglob("*")
    if path.is_file() and path.suffix in {".py", ".mako", ".md"}
]

a = Analysis(
    ["run_server.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden + [
        "main", "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on",
    ],
    excludes=["tkinter", "matplotlib", "numpy", "pytest", "tests"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="aohuang-server", console=True)
coll = COLLECT(exe, a.binaries, a.datas, name="server")
