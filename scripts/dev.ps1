# 一键开发：后端 + 前端 + Electron 窗口
$root = Split-Path -Parent $PSScriptRoot
Write-Host "启动 FastAPI 后端 (8812)..." -ForegroundColor Cyan
$server = Start-Process -FilePath "python" -ArgumentList "-m","uvicorn","main:app","--reload","--port","8812" `
  -WorkingDirectory (Join-Path $root "server") -PassThru -WindowStyle Hidden
Start-Sleep -Seconds 3
Write-Host "启动 Vite 前端 (5173)..." -ForegroundColor Cyan
$web = Start-Process -FilePath "pnpm" -ArgumentList "--filter","web","dev" -WorkingDirectory $root -PassThru -WindowStyle Hidden
Start-Sleep -Seconds 4
Write-Host "启动 Electron..." -ForegroundColor Cyan
Push-Location (Join-Path $root "app")
try {
  pnpm dev
} finally {
  Pop-Location
  Stop-Process -Id $web.Id -Force -ErrorAction SilentlyContinue
  Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
  Write-Host "已停止后端与前端" -ForegroundColor Yellow
}
