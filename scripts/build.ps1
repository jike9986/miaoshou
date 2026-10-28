# 完整打包：前端 → 后端 exe → Electron → NSIS 安装程序
$root = Split-Path -Parent $PSScriptRoot
$ErrorActionPreference = "Stop"
Push-Location $root
try {
  # NSIS 要往 %TEMP% 写临时文件。若当前环境的进程被限制写用户目录，
  # 会弹「Error writing temporary file」并让打包失败，这里统一指到工作区。
  $buildTmp = Join-Path $root "_build_tmp"
  New-Item -ItemType Directory -Path $buildTmp -Force | Out-Null
  $env:TEMP = $buildTmp
  $env:TMP = $buildTmp

  Write-Host "0/6 静态检查（pyflakes + tsc + 回归）..." -ForegroundColor Cyan
  Push-Location server
  python -m pyflakes api services core schemas main.py run_server.py 2>&1 | Tee-Object -Variable pyflakesOut
  $pyflakesBad = $pyflakesOut | Where-Object { $_ -match "undefined name|syntax error|redefinition" }
  Pop-Location
  if ($pyflakesBad) { throw "pyflakes 检查未通过：`n$($pyflakesBad -join "`n")" }

  Push-Location web
  npx tsc --noEmit
  if ($LASTEXITCODE -ne 0) { throw "前端 TypeScript 类型检查失败" }
  Pop-Location

  Push-Location app
  npx tsc --noEmit
  if ($LASTEXITCODE -ne 0) { throw "Electron TypeScript 类型检查失败" }
  Pop-Location

  # P0/P1 回归：mock 上游、跑完自动清理，基线应为 355 商品 / 0 定时任务
  python scripts\regression.py
  if ($LASTEXITCODE -ne 0) { throw "P0/P1 回归未通过（详见上方 FAIL 行）" }

  Write-Host "1/6 构建前端..." -ForegroundColor Cyan
  pnpm build:web
  if ($LASTEXITCODE -ne 0) { throw "前端构建失败" }

  Write-Host "2/6 打包后端 (PyInstaller)..." -ForegroundColor Cyan
  Push-Location server
  python -m PyInstaller --noconfirm server.spec
  if ($LASTEXITCODE -ne 0) { throw "后端打包失败" }
  Pop-Location

  Write-Host "3/6 编译 Electron 主进程..." -ForegroundColor Cyan
  pnpm build:app
  if ($LASTEXITCODE -ne 0) { throw "Electron 编译失败" }

  Write-Host "4/6 生成安装程序..." -ForegroundColor Cyan
  Push-Location app
  pnpm exec electron-builder --win
  if ($LASTEXITCODE -ne 0) { throw "安装程序打包失败" }
  Pop-Location

  Write-Host "5/6 校验产物..." -ForegroundColor Cyan
  if (-not (Test-Path "release")) { throw "release/ 不存在，打包未产出" }

  Write-Host "6/6 完成，产物在 release/" -ForegroundColor Green
  Get-ChildItem release -Filter "*.exe" | Select-Object Name, @{n='MB';e={[math]::Round($_.Length/1MB,1)}}
} finally {
  Pop-Location
}
