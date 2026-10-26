# 安装「登录时自启」：在启动文件夹放一个快捷方式，指向无窗口守夜进程 service.pyw。
# 不需要管理员权限，也不用计划任务（后者要提权，会被拒绝）。
$ErrorActionPreference = 'Stop'
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$startup = [Environment]::GetFolderPath('Startup')
$lnkPath = Join-Path $startup 'WechatDshBot.lnk'
$service = Join-Path $dir 'service.pyw'
$pyw = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\pythonw.exe'

if (-not (Test-Path $service)) { throw "找不到 $service" }
if (-not (Test-Path $pyw)) { throw "找不到 pythonw：$pyw" }

# 清理旧格式的自启项和残留进程
foreach ($old in @('WechatDshBot.vbs')) {
    $p = Join-Path $startup $old
    if (Test-Path $p) { Remove-Item $p -Force }
}
Get-CimInstance Win32_Process -Filter "Name='python.exe' or Name='pythonw.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*bot.py*' -or $_.CommandLine -like '*service.pyw*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*service.ps1*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut($lnkPath)
$lnk.TargetPath = $pyw
$lnk.Arguments = "`"$service`""
$lnk.WorkingDirectory = $dir
$lnk.WindowStyle = 7
$lnk.Description = 'WeChat + DeepSeek Harness bot watchdog (no window)'
$lnk.Save()
Write-Host "已创建启动项：$lnkPath"

Start-Process -FilePath $pyw -ArgumentList "`"$service`"" -WorkingDirectory $dir
Start-Sleep -Seconds 15

$p = Get-CimInstance Win32_Process -Filter "Name='python.exe' or Name='pythonw.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*bot.py*' -or $_.CommandLine -like '*service.pyw*' }
if ($p) {
    foreach ($item in $p) { Write-Host "已启动：$($item.Name) PID=$($item.ProcessId)" }
    Write-Host "日志：$dir\logs\bot-$(Get-Date -Format yyyyMMdd).log"
} else {
    Write-Host "没看到进程；看 $dir\logs\service.log"
}
