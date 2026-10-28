# 以「局域网共享模式」一键启动奥皇上品后端。
#
# 做四件事，每件都能单独跳过：
#   1. 定数据目录（AOHUANG_DATA_DIR），默认 D:\aohuang-data，可 -DataDir 覆盖
#   2. 定共享模式（AOHUANG_SHARED_MODE=1）
#   3. 防火墙放行 TCP 端口（默认 8812），**默认只打印命令不执行**
#   4. 启动后端，等探活通过，打印 GET /system/share 里的 lanAddresses
#
# 为什么防火墙默认不执行：改防火墙是管理员级、影响全机的操作，运维脚本
# 不该在「只是想把服务起来」的时候顺手改系统策略。要真加规则显式传
# -AllowFirewall。
#
# 用法：
#   pwsh -File scripts\share-server.ps1                        # 起服务，打印地址
#   pwsh -File scripts\share-server.ps1 -AllowFirewall         # 顺便加防火墙规则
#   pwsh -File scripts\share-server.ps1 -DataDir E:\aohuang    # 换数据目录
#   pwsh -File scripts\share-server.ps1 -Port 8820             # 换端口
#   pwsh -File scripts\share-server.ps1 -CheckOnly             # 只体检，不起服务
#   pwsh -File scripts\share-server.ps1 -RemoveFirewall        # 删掉本脚本加的规则
#
# 前置：server\ 目录下能跑 python（源码部署），或 release\win-unpacked\resources\server\
#       下有 aohuang-server.exe（打包部署）。两者都有时优先用 exe。

[CmdletBinding()]
param(
    [string]$DataDir = "D:\aohuang-data",
    [int]$Port = 8812,
    [string]$RuleName = "AOHUANG-Share-8812",
    [switch]$AllowFirewall,
    [switch]$RemoveFirewall,
    [switch]$CheckOnly,
    [switch]$SkipFirewallCheck
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

# --- 输出小工具 -------------------------------------------------------------
function Write-Step([string]$Text) { Write-Host "`n[STAGE] $Text" -ForegroundColor Cyan }
function Write-Ok([string]$Text)   { Write-Host "  PASS  $Text" -ForegroundColor Green }
function Write-Warn2([string]$Text){ Write-Host "  WARN  $Text" -ForegroundColor Yellow }
function Write-Bad([string]$Text)  { Write-Host "  FAIL  $Text" -ForegroundColor Red }

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ServerDir = Join-Path $Root "server"
$PackedExe = Join-Path $Root "release\win-unpacked\resources\server\aohuang-server.exe"
$ApiBase = "http://127.0.0.1:$Port/api/v1"

# --- 0. 只删规则就走 --------------------------------------------------------
if ($RemoveFirewall) {
    Write-Step "删除防火墙规则 $RuleName"
    $existing = @(Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue)
    if ($existing.Count -eq 0) {
        Write-Warn2 "没有找到规则 $RuleName，无需删除"
        exit 0
    }
    netsh advfirewall firewall delete rule name="$RuleName" | Out-Null
    if ($LASTEXITCODE -eq 0) { Write-Ok "已删除 $RuleName" }
    else { Write-Bad "删除失败（退出码 $LASTEXITCODE），需要管理员权限" }
    exit $LASTEXITCODE
}

# --- 1. 数据目录 ------------------------------------------------------------
Write-Step "数据目录 $DataDir"
try {
    New-Item -ItemType Directory -Path $DataDir -Force | Out-Null
    # 真写一次才算数：mkdir 成功不代表能建文件（只读卷、ACL、杀软拦截都会这样）
    $probe = Join-Path $DataDir ".write-probe"
    "ok" | Set-Content -Path $probe -Encoding utf8
    Remove-Item $probe -Force
    Write-Ok "可写"
} catch {
    Write-Bad "数据目录不可写：$($_.Exception.Message)"
    Write-Host "       换一个位置：-DataDir E:\aohuang-data" -ForegroundColor Yellow
    exit 3
}
# 网络盘红线：SQLite 官方明确不支持网络文件系统（见 docs/shared-deployment.md §3）
if ($DataDir -match '^\\\\' -or $DataDir -match '^[A-Za-z]:\\?$') {
    Write-Bad "数据目录看起来是网络路径（UNC 或盘符根）：$DataDir"
    Write-Host "       SQLite 不支持网络文件系统，WAL 在断线时会损坏数据库。" -ForegroundColor Yellow
    Write-Host "       必须放在服务端本机的本地磁盘上。" -ForegroundColor Yellow
    exit 4
}

# --- 2. 端口占用 ------------------------------------------------------------
Write-Step "端口 $Port"
$listening = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
if ($listening.Count -gt 0) {
    $owner = (Get-Process -Id $listening[0].OwningProcess -ErrorAction SilentlyContinue).ProcessName
    Write-Warn2 "端口已被占用（进程 $owner），后端会自动往 $($Port+1) 起，客户端探测区间是 8812-8822"
} else {
    Write-Ok "空闲"
}

# --- 3. 本机地址 ------------------------------------------------------------
Write-Step "本机可对外地址"
$addrs = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object {
        $_.IPAddress -notlike "127.*" -and
        $_.IPAddress -notlike "169.254.*" -and
        $_.IPAddress -ne "0.0.0.0"
    } | Sort-Object {
        # 私网优先：10/8、172.16/12、192.168/16 才是同事该填的
        $o = $_.IPAddress.Split(".")
        if ($o[0] -eq "10" -or ($o[0] -eq "172" -and [int]$o[1] -ge 16 -and [int]$o[1] -le 31) -or
            ($o[0] -eq "192" -and $o[1] -eq "168")) { 0 } else { 1 }
    })
if ($addrs.Count -eq 0) {
    Write-Bad "本机没有可对外的 IPv4（只有回环 / 169.254 链路本地）"
    Write-Host "       同事无法连接。检查网线 / 无线是否连上局域网。" -ForegroundColor Yellow
} else {
    foreach ($a in $addrs) {
        Write-Ok ("{0,-16} {1}" -f $a.IPAddress, $a.InterfaceAlias)
    }
    $first = $addrs[0].IPAddress
    Write-Host "`n  发给同事的地址：http://${first}:$Port" -ForegroundColor White
}

# --- 4. 防火墙 --------------------------------------------------------------
Write-Step "防火墙（TCP $Port）"
if ($SkipFirewallCheck) {
    Write-Warn2 "已跳过（-SkipFirewallCheck）"
} else {
    $rule = @(Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue)
    if ($rule.Count -gt 0) {
        Write-Ok "规则已存在：$RuleName"
    } elseif ($AllowFirewall) {
        # 只在 Private / Domain 放行：Public 配置文件下放行等于把服务暴露到
        # 咖啡厅 / 机场 WiFi，这不是「局域网共享」该做的事。
        netsh advfirewall firewall add rule `
            name="$RuleName" `
            dir=in action=allow protocol=TCP localport=$Port `
            profile=private,domain `
            description="奥皇上品 局域网共享后端（scripts/share-server.ps1 添加）" | Out-Null
        if ($LASTEXITCODE -eq 0) { Write-Ok "已添加规则 $RuleName（profile=private,domain）" }
        else { Write-Bad "添加失败（退出码 $LASTEXITCODE），需要管理员权限运行" }
    } else {
        Write-Warn2 "未添加规则。手工执行下面这条（管理员 PowerShell）："
        Write-Host ""
        Write-Host "  netsh advfirewall firewall add rule name=`"$RuleName`" ``" -ForegroundColor Gray
        Write-Host "    dir=in action=allow protocol=TCP localport=$Port ``" -ForegroundColor Gray
        Write-Host "    profile=private,domain" -ForegroundColor Gray
        Write-Host ""
        Write-Host "  删除：netsh advfirewall firewall delete rule name=`"$RuleName`"" -ForegroundColor Gray
        Write-Host "  核对：netsh advfirewall firewall show rule name=`"$RuleName`"" -ForegroundColor Gray
        Write-Host "  或直接重跑本脚本并加 -AllowFirewall" -ForegroundColor Gray
    }
    Write-Host "`n  当前网络位置（放行 private 才生效）：" -ForegroundColor Gray
    Get-NetConnectionProfile -ErrorAction SilentlyContinue |
        ForEach-Object { Write-Host ("    {0,-14} {1,-10} {2}" -f $_.Name, $_.InterfaceAlias, $_.NetworkCategory) -ForegroundColor Gray }
}

if ($CheckOnly) {
    Write-Step "只体检（-CheckOnly），不启动服务"
    exit 0
}

# --- 5. 启动 ----------------------------------------------------------------
Write-Step "启动后端（共享模式）"
$env:AOHUANG_DATA_DIR = $DataDir
$env:AOHUANG_SHARED_MODE = "1"
$env:AOHUANG_API_PORT = "$Port"
# 另一处改动引入的键；设了就用，没设后端自己按 shared_mode 推 0.0.0.0
$env:AOHUANG_BIND_HOST = "0.0.0.0"

if (Test-Path $PackedExe) {
    Write-Host "  用打包后端：$PackedExe"
    $proc = Start-Process -FilePath $PackedExe -PassThru -WindowStyle Hidden
} elseif (Test-Path (Join-Path $ServerDir "main.py")) {
    Write-Host "  用源码后端：$ServerDir\main.py"
    $proc = Start-Process -FilePath "python" -ArgumentList "main.py" `
        -WorkingDirectory $ServerDir -PassThru -WindowStyle Hidden
} else {
    Write-Bad "找不到后端：既没有 $PackedExe，也没有 $ServerDir\main.py"
    exit 5
}
Write-Ok "进程 PID $($proc.Id)"

# --- 6. 等探活 --------------------------------------------------------------
Write-Step "等后端就绪（最多 30 秒）"
$token = ""
$tokenFile = Join-Path $DataDir "secret.key"
$ready = $false
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Milliseconds 500
    try {
        # 探活接口免鉴权：客户端在拿到 token 之前就要能问「服务器活着吗」
        $h = Invoke-RestMethod -Uri "$ApiBase/system/share/health" -TimeoutSec 2
        if ($h.ok -ne $null) { $ready = $true; break }
    } catch {
        # 端口可能被占用后上移，扫一下区间
        foreach ($p in ($Port..($Port + 10))) {
            try {
                $h = Invoke-RestMethod -Uri "http://127.0.0.1:$p/api/v1/system/share/health" -TimeoutSec 1
                if ($h.ok -ne $null) { $Port = $p; $ApiBase = "http://127.0.0.1:$p/api/v1"; $ready = $true; break }
            } catch { }
        }
        if ($ready) { break }
    }
}
if (-not $ready) {
    Write-Bad "后端 30 秒内没起来。看日志：$DataDir\logs\server.log"
    exit 6
}
if (Test-Path $tokenFile) { $token = (Get-Content $tokenFile -Raw).Trim() }
Write-Ok "就绪：$ApiBase（端口 $Port）"

# --- 7. 打印共享状态 --------------------------------------------------------
Write-Step "GET /system/share"
try {
    $share = Invoke-RestMethod -Uri "$ApiBase/system/share" -TimeoutSec 5 `
        -Headers @{ Authorization = "Bearer $token" }
} catch {
    Write-Bad "查询失败：$($_.Exception.Message)"
    exit 7
}

Write-Host ""
Write-Host "  sharedMode      : $($share.sharedMode)"
Write-Host "  bindHost        : $($share.bindHost)"
Write-Host "  port            : $($share.port)"
Write-Host "  walEnabled      : $($share.walEnabled)"
Write-Host "  dbWritable      : $($share.dbWritable)  (busy=$($share.dbBusy))"
Write-Host "  busyTimeoutMs   : $($share.busyTimeoutMs)"
Write-Host "  dbPath          : $($share.dbPath)"
Write-Host "  dbSizeBytes     : $($share.dbSizeBytes)"
Write-Host "  clientCount     : $($share.clientCount)"
Write-Host "  serverVersion   : $($share.serverVersion)"
Write-Host ""
Write-Host "  lanAddresses:" -ForegroundColor White
foreach ($ip in $share.lanAddresses) {
    Write-Host "    $ip" -ForegroundColor White
}
Write-Host ""
Write-Host "  同事在客户端填这个地址：" -ForegroundColor White
foreach ($u in $share.shareUrls) { Write-Host "    $u" -ForegroundColor White }

if ($share.warnings -and $share.warnings.Count -gt 0) {
    Write-Host ""
    Write-Host "  警告：" -ForegroundColor Yellow
    foreach ($w in $share.warnings) { Write-Host "    - $w" -ForegroundColor Yellow }
}

if (-not $share.sharedMode) {
    Write-Host ""
    Write-Warn2 "sharedMode 为 false：库里存的开关优先于环境变量，去 POST /system/share 打开"
}
if ($share.bindHost -like "127.*") {
    Write-Host ""
    Write-Warn2 "bindHost 还是回环地址：别的机器连不上，重启后端让 bind_host 生效"
}

Write-Host ""
Write-Host "  操作员 token（每人一个，只显示一次）：" -ForegroundColor White
Write-Host "    POST $ApiBase/operators  {`"name`":`"张三`"}" -ForegroundColor Gray
Write-Host "  备份：" -ForegroundColor White
Write-Host "    pwsh -File scripts\backup-db.ps1 -DataDir `"$DataDir`"" -ForegroundColor Gray
Write-Host "  停止：" -ForegroundColor White
Write-Host "    Stop-Process -Id $($proc.Id)" -ForegroundColor Gray
Write-Host ""
Write-Host "  部署细节：docs\shared-deployment.md" -ForegroundColor Gray
