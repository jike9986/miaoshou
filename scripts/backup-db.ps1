# 安全备份奥皇上品的 SQLite 数据库。
#
# 为什么不能直接 copy app.db：
#   WAL 模式下最近的写入先在 app.db-wal 里，主库文件还没合并。服务运行中
#   直接 copy 主库会拿到一份**少了最新数据**的库 —— 实测：活库 65 行，
#   裸 copy 只有 60 行，差的 5 行全在 -wal 里（见 _meas/probe_network_fs.py
#   与 docs/shared-deployment.md §6 的实测表）。
#   把 app.db / -wal / -shm 三个文件一起 copy 也不行：三个文件不在同一时刻，
#   拼起来可能对不上。
#
# 正确做法是 `VACUUM INTO`：SQLite 自己开一个读事务，把一致的快照写进新文件。
# 产物是**单文件、无 WAL 依赖、已整理**，可以直接拷走或归档。
#
# 本机没有 sqlite3.exe（实测 Get-Command sqlite3 为空），所以走 Python 的
# sqlite3 模块。Python 是后端自己的运行依赖，装了后端就一定有。
#
# 用法：
#   pwsh -File scripts\backup-db.ps1                          # 备份默认数据目录
#   pwsh -File scripts\backup-db.ps1 -DataDir D:\aohuang-data
#   pwsh -File scripts\backup-db.ps1 -Keep 20                  # 保留最近 20 份
#   pwsh -File scripts\backup-db.ps1 -OutDir E:\backup\aohuang # 备份到别的盘
#   pwsh -File scripts\backup-db.ps1 -Verify                   # 额外跑 integrity_check
#
# 计划任务（每天 02:00）：
#   schtasks /create /tn "AohuangBackup" /sc daily /st 02:00 /ru SYSTEM ^
#     /tr "pwsh -NoProfile -File E:\tk-desktop\scripts\backup-db.ps1"

[CmdletBinding()]
param(
    [string]$DataDir = "",
    [string]$OutDir = "",
    [int]$Keep = 10,
    [switch]$Verify
)

$ErrorActionPreference = "Stop"

function Write-Step([string]$Text) { Write-Host "`n[STAGE] $Text" -ForegroundColor Cyan }
function Write-Ok([string]$Text)   { Write-Host "  PASS  $Text" -ForegroundColor Green }
function Write-Warn2([string]$Text){ Write-Host "  WARN  $Text" -ForegroundColor Yellow }
function Write-Bad([string]$Text)  { Write-Host "  FAIL  $Text" -ForegroundColor Red }

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

# --- 1. 定位数据目录 --------------------------------------------------------
Write-Step "定位数据目录"
if (-not $DataDir) {
    # 优先级与后端一致：环境变量 > 打包版 exe 同级 userdata > LOCALAPPDATA 默认
    if ($env:AOHUANG_DATA_DIR) {
        $DataDir = $env:AOHUANG_DATA_DIR
        Write-Ok "环境变量 AOHUANG_DATA_DIR = $DataDir"
    } else {
        $candidates = @(
            (Join-Path $Root "release\win-unpacked\userdata"),
            (Join-Path $env:LOCALAPPDATA "aohuang-desktop"),
            "D:\aohuang-data"
        )
        foreach ($c in $candidates) {
            if (Test-Path (Join-Path $c "app.db")) { $DataDir = $c; break }
        }
        if ($DataDir) { Write-Ok "自动找到：$DataDir" }
    }
}
if (-not $DataDir) {
    Write-Bad "找不到数据目录，请显式指定：-DataDir D:\aohuang-data"
    exit 2
}

$DbFile = Join-Path $DataDir "app.db"
if (-not (Test-Path $DbFile)) {
    Write-Bad "数据库不存在：$DbFile"
    exit 2
}
Write-Ok $DbFile

# --- 2. 备份目录 ------------------------------------------------------------
if (-not $OutDir) { $OutDir = Join-Path $DataDir "backups" }
if (-not (Test-Path $OutDir)) {
    New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
}
Write-Step "备份目录 $OutDir"

# --- 3. 找 python -----------------------------------------------------------
Write-Step "定位 Python（VACUUM INTO 需要 sqlite3 模块）"
$python = $null
foreach ($candidate in @("python", "py")) {
    $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($cmd) { $python = $cmd.Source; break }
}
if (-not $python) {
    Write-Bad "找不到 python。装了后端就有 python，或手工用 sqlite3 执行："
    Write-Host "       sqlite3 `"$DbFile`" `".backup '$OutDir\app-manual.db'`"" -ForegroundColor Gray
    exit 3
}
Write-Ok $python

# --- 4. VACUUM INTO ---------------------------------------------------------
# 时间戳到秒，文件名里不带冒号（Windows 不允许）
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$target = Join-Path $OutDir "app-$stamp.db"
$verifyFlag = if ($Verify) { "1" } else { "0" }

Write-Step "VACUUM INTO $target"

# Python 脚本写到临时文件再执行：PowerShell 里内联多行 Python 的引号转义
# 在 pwsh 5.1 / 7 之间不一致，落文件最稳。
$pyScript = @'
import json
import os
import sqlite3
import sys
import time

src, dest, do_verify = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
result = {"ok": False, "src": src, "dest": dest}

def count_rows(path):
    con = sqlite3.connect("file:" + path + "?mode=ro", uri=True, timeout=30.0)
    try:
        return con.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    finally:
        con.close()

started = time.perf_counter()
try:
    # 只读打开源库不现实（VACUUM 要写目标），但 busy_timeout 必须给够：
    # 服务正在跑的时候 VACUUM 要和写入排队，5 秒不够一次大批量导入。
    con = sqlite3.connect(src, timeout=60.0)
    try:
        con.execute("PRAGMA busy_timeout=60000")
        con.execute("VACUUM INTO ?", (dest,))
    finally:
        con.close()
    result["seconds"] = round(time.perf_counter() - started, 3)
    result["sizeBytes"] = os.path.getsize(dest)
    result["sourceRows"] = count_rows(src)
    result["backupRows"] = count_rows(dest)
    if do_verify:
        con = sqlite3.connect("file:" + dest + "?mode=ro", uri=True)
        try:
            result["integrity"] = con.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            con.close()
    result["ok"] = result["backupRows"] == result["sourceRows"]
    if not result["ok"]:
        result["error"] = "商品数不一致：源 %s / 备份 %s" % (
            result["sourceRows"], result["backupRows"])
except Exception as exc:
    result["error"] = "%s: %s" % (type(exc).__name__, exc)

print(json.dumps(result, ensure_ascii=False))
sys.exit(0 if result["ok"] else 1)
'@

$pyFile = Join-Path ([System.IO.Path]::GetTempPath()) "aohuang_backup_$stamp.py"
Set-Content -Path $pyFile -Value $pyScript -Encoding utf8
try {
    $raw = & $python $pyFile $DbFile $target $verifyFlag 2>&1
    $exitCode = $LASTEXITCODE
} finally {
    Remove-Item $pyFile -Force -ErrorAction SilentlyContinue
}

# python 的 stdout 可能是数组（多行），拼起来再解析
$json = ($raw | Out-String).Trim()
try {
    $res = $json | ConvertFrom-Json
} catch {
    Write-Bad "备份脚本输出无法解析："
    Write-Host $json -ForegroundColor Gray
    exit 4
}

if (-not $res.ok) {
    Write-Bad "备份失败：$($res.error)"
    Remove-Item $target -Force -ErrorAction SilentlyContinue
    exit 5
}

Write-Ok ("备份完成 {0} 秒 / {1:N0} 字节 / 商品 {2} 行（源 {3} 行）" -f `
    $res.seconds, $res.sizeBytes, $res.backupRows, $res.sourceRows)
if ($res.integrity) {
    if ($res.integrity -eq "ok") { Write-Ok "integrity_check = ok" }
    else { Write-Bad "integrity_check = $($res.integrity)" }
}

# --- 5. 保留最近 N 份 -------------------------------------------------------
Write-Step "清理旧备份（保留最近 $Keep 份）"
$all = @(Get-ChildItem -Path $OutDir -Filter "app-*.db" -File |
    Sort-Object LastWriteTime -Descending)
Write-Host "  现有 $($all.Count) 份"
if ($all.Count -gt $Keep) {
    $drop = $all | Select-Object -Skip $Keep
    foreach ($f in $drop) {
        Remove-Item $f.FullName -Force -ErrorAction SilentlyContinue
        Write-Host "  删除 $($f.Name)" -ForegroundColor Gray
    }
    Write-Ok "删除 $($drop.Count) 份，保留 $Keep 份"
} else {
    Write-Ok "无需清理"
}

Write-Host ""
Write-Host "  最新备份：" -ForegroundColor White
$newest = Get-ChildItem -Path $OutDir -Filter "app-*.db" -File |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($newest) {
    Write-Host ("    {0}  {1:N0} 字节  {2}" -f $newest.Name, $newest.Length, $newest.LastWriteTime) -ForegroundColor White
}
Write-Host ""
Write-Host "  恢复方法（务必先停后端）：" -ForegroundColor Gray
Write-Host "    1) Stop-Process -Name aohuang-server -Force" -ForegroundColor Gray
Write-Host "    2) 把 app.db 改名为 app.db.broken（不要删，留着取证）" -ForegroundColor Gray
Write-Host "    3) 把备份文件复制成 $DbFile" -ForegroundColor Gray
Write-Host "    4) 同时删掉 app.db-wal 与 app.db-shm（残留的 WAL 会和恢复的库对不上）" -ForegroundColor Gray
Write-Host "    5) 重启后端，GET /system/share 确认 dbWritable=true" -ForegroundColor Gray
exit 0
