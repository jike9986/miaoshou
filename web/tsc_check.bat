@echo off
cd /d "D:\缓存文件夹\codex工作台\p006\miaoshou\web"
npx tsc --noEmit 2>&1
echo Exit code: %ERRORLEVEL%
