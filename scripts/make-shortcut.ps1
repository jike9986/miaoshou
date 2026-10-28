# 创建桌面快捷方式（真正可双击的 .lnk，带 --no-sandbox）
# 本机 Electron 沙箱在 JS 之前就崩，必须在命令行带 --no-sandbox，
# 所以快捷方式的 Arguments 必须是 "--no-sandbox"。
$ErrorActionPreference = "Stop"

$dir = "E:\codex工作台\p006\tk-desktop\release\win-unpacked"
$exe = Join-Path $dir "AohuangShangpin.exe"

if (-not (Test-Path $exe)) {
  Write-Host "找不到 exe: $exe" -ForegroundColor Red
  exit 1
}

$desktop = [Environment]::GetFolderPath("Desktop")
$lnkPath = Join-Path $desktop "奥皇上品.lnk"

$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut($lnkPath)
$s.TargetPath = $exe
$s.Arguments = "--no-sandbox"
$s.WorkingDirectory = $dir
$s.IconLocation = "$exe,0"
$s.Description = "奥皇上品 - 桌面端"
$s.Save()

Write-Host "已创建: $lnkPath"
$check = $ws.CreateShortcut($lnkPath)
Write-Host "  Target    = $($check.TargetPath)"
Write-Host "  Arguments = $($check.Arguments)"
Write-Host "  WorkDir   = $($check.WorkingDirectory)"

# 开始菜单也放一份
$smDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\奥皇上品"
New-Item -ItemType Directory -Path $smDir -Force | Out-Null
$smLnk = Join-Path $smDir "奥皇上品.lnk"
$s2 = $ws.CreateShortcut($smLnk)
$s2.TargetPath = $exe
$s2.Arguments = "--no-sandbox"
$s2.WorkingDirectory = $dir
$s2.IconLocation = "$exe,0"
$s2.Description = "奥皇上品 - 桌面端"
$s2.Save()
Write-Host "已创建开始菜单: $smLnk"
