# -*- coding: utf-8 -*-
# ShotBridge 摄像机运镜描述器 - 安装脚本
# 用法(双击“① 一键安装到Maya.bat”即可,无需手动运行本文件):
#   powershell.exe -NoProfile -ExecutionPolicy Bypass -File installer\install.ps1
# 可选参数(给排查问题/测试用):
#   -MayaInstallDir "E:\Maya2026\Maya2026"   指定 Maya 安装目录(默认查注册表)
#   -MayaVersion 2026                         指定版本号
#   -MayaAppDir  "D:\temp\maya"               指定 Maya 用户目录(默认 文档\maya)
#   -DryRun                                   只显示将做什么,不写文件

param(
    [string]$MayaInstallDir = '',
    [string]$MayaVersion = '',
    [string]$MayaAppDir = '',
    [switch]$DryRun
)

. (Join-Path $PSScriptRoot 'common.ps1')

$exitCode = 0
try {
    Write-Head ($script:ProductName + '  v1.0.0  安装')

    $packageRoot = Get-PackageRoot
    $info = Get-PackageInfo $packageRoot
    $maya = Select-MayaInstallation $MayaInstallDir $MayaVersion
    if (-not $maya.Version) {
        throw '无法确定 Maya 版本号。请用 -MayaVersion 2026 指定。'
    }
    $appDir = Get-MayaAppDir $MayaAppDir
    $layout = Get-InstallLayout $appDir

    Write-Step ('交付包: ' + $packageRoot)
    Write-Step ('工具版本: ' + $info.Version)
    Write-Step ('目标 Maya: ' + $maya.Version + '  (' + $maya.InstallDir + ')')
    Write-Step ('Maya 用户目录: ' + $appDir)
    Write-Step ('程序安装到: ' + $layout.PayloadDir)
    Write-Step ('模块文件写入: ' + $layout.ModFile)
    Write-Plain ''

    if ($maya.Version -ne '2026') {
        Write-Warn ('本工具在 Maya 2026 上完成验收,当前目标是 ' + $maya.Version + ',可以安装但不保证显示效果。')
    }

    if ($DryRun) {
        Write-Head '试运行结束'
        Write-Plain '这是试运行(-DryRun),没有写入任何文件。'
        Write-Plain ('将写入模块文件内容: ' + (Get-ModuleFileContent $maya.Version).Trim())
        exit 0
    }

    $timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'

    # ---------- 1. 备份旧版本 ----------
    $backedUp = $false
    if (Test-Path -LiteralPath $layout.ModFile) {
        New-Item -ItemType Directory -Force -Path $layout.BackupDir | Out-Null
        $modBackup = Join-Path $layout.BackupDir ('ShotBridge.mod.bak-' + $timestamp)
        Copy-Item -LiteralPath $layout.ModFile -Destination $modBackup -Force
        Write-Ok ('已备份原模块文件: ' + $modBackup)
        $backedUp = $true
    }
    if (Test-Path -LiteralPath $layout.PayloadDir) {
        New-Item -ItemType Directory -Force -Path $layout.BackupDir | Out-Null
        $payloadBackup = Join-Path $layout.BackupDir ('ShotBridge-' + $timestamp)
        Move-Item -LiteralPath $layout.PayloadDir -Destination $payloadBackup -Force
        Write-Ok ('已备份旧程序目录: ' + $payloadBackup)
        $backedUp = $true
    }
    if (-not $backedUp) { Write-Plain '没有发现旧版本,无需备份。' }

    # ---------- 2. 复制程序文件 ----------
    New-Item -ItemType Directory -Force -Path $layout.PayloadDir | Out-Null
    Copy-Item -Path (Join-Path $info.PayloadSource '*') -Destination $layout.PayloadDir -Recurse -Force
    # 清掉可能被一起复制的 Python 缓存,保持安装目录干净
    Get-ChildItem -Path $layout.PayloadDir -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    $copiedCount = (Get-ChildItem -Path $layout.PayloadDir -Recurse -File).Count
    Write-Ok ('已复制程序文件: ' + $copiedCount + ' 个')

    # ---------- 3. 写入模块文件 ----------
    $modContent = Get-ModuleFileContent $maya.Version
    Write-AsciiFile $layout.ModFile $modContent
    Write-Ok ('已写入模块文件: ' + $layout.ModFile)

    # ---------- 4. 校验安装结果 ----------
    $problems = New-Object System.Collections.ArrayList
    $expectedFiles = @(
        'camera_motion_describer\__init__.py',
        'camera_motion_describer\ui.py',
        'camera_motion_describer\analysis.py',
        'scripts\userSetup.py',
        'examples\SB_CameraMotion_Validation.ma'
    )
    foreach ($relative in $expectedFiles) {
        if (-not (Test-Path -LiteralPath (Join-Path $layout.PayloadDir $relative))) {
            $null = $problems.Add(('缺少文件: ' + $relative))
        }
    }
    $writtenMod = Get-Content -LiteralPath $layout.ModFile -Raw
    if ($writtenMod -notmatch ('MAYAVERSION:' + [regex]::Escape($maya.Version))) {
        $null = $problems.Add('模块文件里的版本号不正确')
    }
    if ($problems.Count -gt 0) { throw ('安装校验失败: ' + ($problems -join '; ')) }
    Write-Ok '安装结果校验通过'

    # ---------- 完成 ----------
    Write-Head '安装完成'
    Write-Plain ('工具已装到 Maya ' + $maya.Version + ',以后每次启动 Maya 都会自动加载。')
    Write-Plain ''
    Write-Plain '下一步:'
    Write-Plain '  1. 双击“② 启动Maya并打开工具.bat”,Maya 启动后工具会自动弹出;'
    Write-Plain '  2. 以后也可以在 Maya 工具架的 ShotBridge 页签里点“运镜描述”按钮打开。'
    Write-Plain ''
    Write-Plain ('卸载:双击“③ 卸载.bat”(安装文件在 ' + $layout.PayloadDir + ')。')
    $mayaRunning = @(Get-Process -Name 'maya' -ErrorAction SilentlyContinue).Count -gt 0
    if ($mayaRunning) {
        Write-Plain ''
        Write-Warn '检测到 Maya 正在运行:新工具要重启 Maya 之后才会加载。'
    }
} catch {
    Write-Fail $_.Exception.Message
    Write-Plain ''
    Write-Plain '安装没有完成。可以把上面的文字发给工具作者,或检查:'
    Write-Plain '  - Maya 是否安装在这台电脑上;'
    Write-Plain '  - 压缩包是否完整解压(不要在压缩包里直接双击)。'
    $exitCode = 1
}
exit $exitCode
