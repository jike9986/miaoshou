# 生产客户端：校验 → 前端/Electron → 无共享令牌的目录包 → Inno 安装程序。
param([string]$InnoCompiler = "")

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

  if (-not $InnoCompiler) {
    $compilerCandidates = @(
      "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
      "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    )
    $InnoCompiler = $compilerCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
  }
  if (-not $InnoCompiler -or -not (Test-Path -LiteralPath $InnoCompiler)) {
    throw "未找到 Inno Setup 6，请安装或通过 -InnoCompiler 指定 ISCC.exe。"
  }

  Write-Host "1/5 版本与静态检查..." -ForegroundColor Cyan
  python -X utf8 scripts\check_version.py
  if ($LASTEXITCODE -ne 0) { throw "版本号不一致" }
  pnpm check
  if ($LASTEXITCODE -ne 0) { throw "静态检查失败" }

  Write-Host "2/5 构建前端和 Electron..." -ForegroundColor Cyan
  pnpm build
  if ($LASTEXITCODE -ne 0) { throw "客户端构建失败" }

  Write-Host "3/5 生成目录包（仅连接 HTTPS 服务端）..." -ForegroundColor Cyan
  Push-Location app
  try {
    pnpm exec electron-builder --win --dir
    if ($LASTEXITCODE -ne 0) { throw "Electron 目录包构建失败" }
  } finally {
    Pop-Location
  }

  Write-Host "4/5 校验目录包和登录启动..." -ForegroundColor Cyan
  node scripts\verify-desktop-package.mjs
  if ($LASTEXITCODE -ne 0) { throw "目录包校验失败" }

  Write-Host "5/5 生成可选择目录和新建文件夹的安装程序..." -ForegroundColor Cyan
  & $InnoCompiler installer\setup.iss
  if ($LASTEXITCODE -ne 0) { throw "Inno 安装程序构建失败" }

  $version = (Get-Content -LiteralPath app\package.json -Raw | ConvertFrom-Json).version
  $installer = Join-Path $root "release\奥皇上品-$version-安装程序.exe"
  if (-not (Test-Path -LiteralPath $installer)) { throw "安装程序产物不存在" }
  Get-Item -LiteralPath $installer | Select-Object Name, @{n='MB';e={[math]::Round($_.Length/1MB,1)}}
  python -X utf8 -c "import hashlib,sys; from pathlib import Path; p=Path(sys.argv[1]); print('SHA256: '+hashlib.file_digest(p.open('rb'), 'sha256').hexdigest())" $installer
  if ($LASTEXITCODE -ne 0) { throw "安装程序校验和计算失败" }
} finally {
  $env:TEMP = $originalTemp
  $env:TMP = $originalTmp
  Pop-Location
}
