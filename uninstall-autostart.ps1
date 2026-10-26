# 取消开机自启并停掉机器人。
$ErrorActionPreference = 'Continue'
$startup = [Environment]::GetFolderPath('Startup')
foreach ($name in @('WechatDshBot.lnk', 'WechatDshBot.vbs')) {
    $p = Join-Path $startup $name
    if (Test-Path $p) { Remove-Item $p -Force; Write-Host "已移除启动项：$p" }
}

Get-CimInstance Win32_Process -Filter "Name='python.exe' or Name='pythonw.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*bot.py*' -or $_.CommandLine -like '*service.pyw*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*service.ps1*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Write-Host "已取消自启并停止机器人。"
