@echo off
taskkill /F /IM AohuangShangpin.exe /T 2>nul
taskkill /F /IM aohuang-server.exe /T 2>nul
timeout /t 3 /nobreak >nul

set SRC=D:\缓存文件夹\codex工作台\p006\miaoshou\release\win-unpacked
set DST=D:\软件\奥皇上品

copy /Y "%SRC%\AohuangShangpin.exe" "%DST%\AohuangShangpin.exe"
echo [1/3] AohuangShangpin.exe updated

copy /Y "%SRC%\resources\app.asar" "%DST%\resources\app.asar"
echo [2/3] app.asar updated

rmdir /S /Q "%DST%\resources\web"
xcopy /E /I /Y "%SRC%\resources\web" "%DST%\resources\web"
echo [3/3] resources\web updated

echo Done. Restart AohuangShangpin.exe manually.
