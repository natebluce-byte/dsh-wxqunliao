@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在停止机器人...
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='pythonw.exe'\" | Where-Object { $_.CommandLine -like '*bot.py*' -or $_.CommandLine -like '*service.pyw*' -or $_.CommandLine -like '*workbuddy_checkin*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
echo 已停止（启动文件夹里的自启项还在，下次登录仍会自动启动）。
echo 要彻底取消自启：powershell -ExecutionPolicy Bypass -File uninstall-autostart.ps1
pause
