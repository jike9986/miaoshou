# 部署到 C:\AohuangDesktop（桌面快捷方式指向这里）
#
# 为什么要单独一个脚本：以前是手工 Copy-Item，两次踩同样的坑 ——
#   1. 只拷 app.asar，忘了 resources\web（真正的界面文件在 web\assets\*.js）
#      → 界面看起来还是老一版，因为 index.html 指着旧 hash 的 js。
#   2. Copy-Item -Recurse 到已存在的目录是「合并」，新旧 hash 文件并存
#      → index.html 可能仍指向旧文件。
# 所以这里先删空 resources\web 再整体覆盖，userdata 永远不碰。
#
# 用法：
#   pwsh -File scripts\deploy.ps1
# 前置：
#   web 已 pnpm run build，app 已 electron-builder --win --dir

$ErrorActionPreference = "Stop"

$src = Join-Path $PSScriptRoot "..\release\win-unpacked"
$dst = "C:\AohuangDesktop"

if (-not (Test-Path $src)) {
    throw "找不到打包产物：$src（先跑 electron-builder --win --dir）"
}
if (-not (Test-Path $dst)) {
    throw "找不到部署目录：$dst"
}

# electron-builder 在 win-unpacked 已存在时走增量路径，会跳过 extraResources，
# 结果是 resources\ 里只剩 app.asar，web 和 server 都不见了（实测踩过两次）。
# 所以打包前必须先删掉旧产物——这里只删、不检查，删不掉的会立刻在下一步报错。
$stale = Join-Path $src "resources\web"
if ((Test-Path $src) -and -not (Test-Path $stale)) {
    Write-Host "旧产物不完整（缺 resources\web），清掉重打更保险" -ForegroundColor Yellow
}

# electron-builder 返回后文件系统可能还没落盘（实测踩过三次：
# resources\web 或 aohuang-server.exe 还不存在，脚本直接抛错）。
# 等三个关键产物都出现再继续，最多等 60 秒。
$required = @(
    (Join-Path $src "AohuangShangpin.exe"),
    (Join-Path $src "resources\web\index.html"),
    (Join-Path $src "resources\server\aohuang-server.exe")
)
$deadline = (Get-Date).AddSeconds(60)
foreach ($path in $required) {
    while (-not (Test-Path $path)) {
        if ((Get-Date) -gt $deadline) {
            throw "打包产物不完整，缺少：$path。先删掉 release\win-unpacked 再重跑 electron-builder（增量打包会跳过 extraResources）"
        }
        Start-Sleep -Milliseconds 500
    }
}

# 1. 先停进程，否则 exe / server.exe 被占用会 Access denied
Get-Process | Where-Object { $_.ProcessName -match "Aohuang|aohuang-server" } |
    ForEach-Object { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 3

# 2. 主程序
Copy-Item (Join-Path $src "AohuangShangpin.exe") (Join-Path $dst "AohuangShangpin.exe") -Force
Write-Host "[1/4] 主程序已更新"

# 3. app.asar（主进程代码）
$asarSrc = Join-Path $src "resources\app.asar"
if (Test-Path $asarSrc) {
    Copy-Item $asarSrc (Join-Path $dst "resources\app.asar") -Force
    Write-Host "[2/4] app.asar 已更新"
}

# 4. resources\web —— 必须「先删再拷」，不能合并
$webSrc = Join-Path $src "resources\web"
$webDst = Join-Path $dst "resources\web"
if (Test-Path $webSrc) {
    if (Test-Path $webDst) { Remove-Item $webDst -Recurse -Force }
    Copy-Item $webSrc $webDst -Recurse -Force
    $n = (Get-ChildItem $webDst -Recurse -File | Measure-Object).Count
    $asset = (Get-ChildItem (Join-Path $webDst "assets") -File -ErrorAction SilentlyContinue |
              Select-Object -First 1).Name
    Write-Host "[3/4] resources\web 已更新（$n 个文件，入口 $asset）"
} else {
    throw "打包产物里没有 resources\web，electron-builder 配置有问题"
}

# 5. 后端可执行文件
$srvSrc = Join-Path $src "resources\server"
$srvDst = Join-Path $dst "resources\server"
if (Test-Path $srvSrc) {
    Copy-Item (Join-Path $srvSrc "*") $srvDst -Recurse -Force
    Write-Host "[4/4] 后端已更新"
}

Write-Host ""
Write-Host "部署完成：$dst"

# 校验：部署后的后端与界面必须和打包产物一致。
# 之前踩过「以为部署成功、其实还是上一版后端」——exe 时间戳没变，
# 白跑了几十分钟验证。这里直接把不一致打出来。
$srvDeployed = Join-Path $dst "resources\server\aohuang-server.exe"
$srvBuilt = Join-Path $src "resources\server\aohuang-server.exe"
if ((Test-Path $srvDeployed) -and (Test-Path $srvBuilt)) {
    $a = (Get-Item $srvBuilt).LastWriteTime
    $b = (Get-Item $srvDeployed).LastWriteTime
    if ($a -ne $b) {
        Write-Host "警告：后端时间戳不一致（产物 $a / 部署 $b）" -ForegroundColor Yellow
    } else {
        Write-Host "校验：后端 $b"
    }
}
$webIndex = Join-Path $dst "resources\web\index.html"
if (Test-Path $webIndex) {
    Write-Host "校验：界面 $((Get-Item $webIndex).LastWriteTime)"
}

Write-Host "启动：cmd /c start `"`" `"$dst\launch.bat`""
