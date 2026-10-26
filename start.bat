@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" (
    echo [错误] 找不到 Python 3.12：%PY%
    echo 请先安装：winget install --id Python.Python.3.12 -e
    pause
    exit /b 1
)

echo ============================================
echo  wechat-dsh-bot  启动中（Ctrl+C 停止）
echo ============================================
"%PY%" bot.py run %*
echo.
echo 进程已退出。
pause
