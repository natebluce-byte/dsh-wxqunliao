@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" (
    echo [错误] 找不到 Python 3.12：%PY%
    pause
    exit /b 1
)
"%PY%" bot.py check
echo.
pause
