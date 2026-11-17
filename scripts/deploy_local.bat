@echo off
set "SRC=D:\huan_cache\miaoshou\release\win-unpacked"
set "DST=D:\software\aohuang"
echo SRC=%SRC%
echo DST=%DST%

taskkill /F /IM AohuangShangpin.exe /T 2>nul
taskkill /F /IM aohuang-server.exe /T 2>nul
timeout /t 3 /nobreak >nul

copy /Y "%SRC%\AohuangShangpin.exe" "%DST%\AohuangShangpin.exe"
echo [1/3] exe updated

copy /Y "%SRC%\resources\app.asar" "%DST%\resources\app.asar"
echo [2/3] asar updated

rmdir /S /Q "%DST%\resources\web"
xcopy /E /I /Y "%SRC%\resources\web" "%DST%\resources\web"
echo [3/3] web updated

echo All done.
