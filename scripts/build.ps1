# 生产客户端：校验 → 前端/Electron → NSIS 安装包 → 产物校验。
#
# 2026-11-23 改：安装包从 Inno Setup 换成 electron-builder 自带的 NSIS。
# 原因见 docs/project/auto-update-nsis-2026-11-23.md —— Inno 不认客户端传的
# `/S`，会弹图形向导，导致「关软件后不安装、不重启、快捷方式指向旧版本」。
param([switch]$SkipChecks)

$root = Split-Path -Parent $PSScriptRoot
$ErrorActionPreference = "Stop"
$originalTemp = $env:TEMP
$originalTmp = $env:TMP
Push-Location $root
try {
  $buildTmp = Join-Path $root "_build_tmp"
  New-Item -ItemType Directory -Path $buildTmp -Force | Out-Null
  $env:TEMP = $buildTmp
  $env:TMP = $buildTmp

  if (-not $SkipChecks) {
    Write-Host "1/4 版本与静态检查..." -ForegroundColor Cyan
    python -X utf8 scripts\check_version.py
    if ($LASTEXITCODE -ne 0) { throw "版本号不一致" }
    pnpm check
    if ($LASTEXITCODE -ne 0) { throw "静态检查失败" }
  }

  Write-Host "2/4 构建前端和 Electron..." -ForegroundColor Cyan
  pnpm build
  if ($LASTEXITCODE -ne 0) { throw "客户端构建失败" }

  Write-Host "3/4 生成 NSIS 安装包..." -ForegroundColor Cyan
  Push-Location app
  try {
    pnpm exec electron-builder --win nsis
    if ($LASTEXITCODE -ne 0) { throw "NSIS 安装包构建失败" }
  } finally {
    Pop-Location
  }

  Write-Host "4/4 校验安装包..." -ForegroundColor Cyan
  node scripts\verify-desktop-package.mjs
  if ($LASTEXITCODE -ne 0) { throw "目录包校验失败" }

  $version = (Get-Content -LiteralPath app\package.json -Raw | ConvertFrom-Json).version
  $installer = Join-Path $root "release\奥皇上品-$version-Setup.exe"
  if (-not (Test-Path -LiteralPath $installer)) { throw "安装包产物不存在：$installer" }
  Get-Item -LiteralPath $installer | Select-Object Name, @{n='MB';e={[math]::Round($_.Length/1MB,1)}}
  python -X utf8 -c "import hashlib,sys; from pathlib import Path; p=Path(sys.argv[1]); print('SHA256: '+hashlib.file_digest(p.open('rb'), 'sha256').hexdigest())" $installer
  if ($LASTEXITCODE -ne 0) { throw "安装包校验和计算失败" }
} finally {
  $env:TEMP = $originalTemp
  $env:TMP = $originalTmp
  Pop-Location
}