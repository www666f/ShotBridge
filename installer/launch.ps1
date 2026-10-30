# -*- coding: utf-8 -*-
# ShotBridge 摄像机运镜描述器 - 启动器
# 用法(双击“② 启动Maya并打开工具.bat”即可):
#   powershell.exe -NoProfile -ExecutionPolicy Bypass -File installer\launch.ps1
# 原理:安装后的 userSetup.py 会在 Maya 启动时读环境变量 SHOTBRIDGE_AUTOSTART,
#       等于 1 时自动打开描述器窗口。所以这里只需要设置变量再启动 Maya,
#       不用给 maya.exe 拼任何 -command 参数。

param(
    [string]$MayaInstallDir = '',
    [string]$MayaVersion = '',
    [string]$MayaAppDir = ''
)

. (Join-Path $PSScriptRoot 'common.ps1')

$exitCode = 0
try {
    Write-Head ($script:ProductName + '  启动')

    $maya = Select-MayaInstallation $MayaInstallDir $MayaVersion
    $appDir = Get-MayaAppDir $MayaAppDir
    $layout = Get-InstallLayout $appDir

    # 先确认工具确实装好了,避免启动完 Maya 才发现没装
    if (-not (Test-Path -LiteralPath $layout.ModFile)) {
        Write-Fail '还没有安装 ShotBridge。请先双击“① 一键安装到Maya.bat”。'
        exit 1
    }
    if (-not (Test-Path -LiteralPath $layout.PayloadDir)) {
        Write-Fail '安装目录不完整,请重新双击“① 一键安装到Maya.bat”。'
        exit 1
    }

    Write-Step ('Maya: ' + $maya.Version + '  (' + $maya.MayaExe + ')')

    # Maya 已经在运行时,新进程会和现有会话抢文件锁,只能让用户去点工具架按钮
    if (@(Get-Process -Name 'maya' -ErrorAction SilentlyContinue).Count -gt 0) {
        Write-Warn 'Maya 已经在运行了。'
        Write-Plain '请在已打开的 Maya 里操作:'
        Write-Plain '  工具架 → ShotBridge 页签 → 点“运镜描述”按钮。'
        Write-Plain '(如果看不到 ShotBridge 页签,重启一次 Maya。)'
        exit 0
    }

    # 让本次启动的 Maya 自动弹窗(安装目录里的 userSetup.py 会读这个变量)
    Set-Item -Path ('env:' + $script:AutostartEnv) -Value '1'
    $binDir = Split-Path -Parent $maya.MayaExe
    Start-Process -FilePath $maya.MayaExe -WorkingDirectory $binDir

    Write-Ok '已启动 Maya。'
    Write-Plain 'Maya 界面加载完成后,“摄像机运镜描述器”会自动弹出(首次启动可能需要一两分钟)。'
    Write-Plain '如果没有自动弹出,去工具架 ShotBridge 页签点“运镜描述”即可。'
    Write-Plain '这个窗口可以关掉,Maya 会继续运行。'
} catch {
    Write-Fail $_.Exception.Message
    $exitCode = 1
}
exit $exitCode
