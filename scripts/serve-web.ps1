# 浏览器验证模式：后端 + 前端（不需要 Electron）
$root = Split-Path -Parent $PSScriptRoot
Write-Host "启动后端 8812..." -ForegroundColor Cyan
$server = Start-Process -FilePath "python" -ArgumentList "-m","uvicorn","main:app","--port","8812" `
  -WorkingDirectory (Join-Path $root "server") -PassThru -WindowStyle Hidden
Start-Sleep -Seconds 3
Write-Host "启动前端 5173..." -ForegroundColor Cyan
Write-Host "浏览器打开 http://127.0.0.1:5173" -ForegroundColor Green
Push-Location (Join-Path $root "web")
try { pnpm dev } finally { Pop-Location; Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue }
