@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist output mkdir output

echo.
echo   seedream-layers  http://127.0.0.1:8787
echo   关闭此窗口即停止服务
echo.

start "" http://127.0.0.1:8787
python -m uvicorn app:app --host 127.0.0.1 --port 8787
pause
