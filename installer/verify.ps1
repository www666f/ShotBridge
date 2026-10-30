# -*- coding: utf-8 -*-
# ShotBridge 摄像机运镜描述器 - 自检脚本(可选)
# 用法(双击“④ 自检.bat”):
#   powershell.exe -NoProfile -ExecutionPolicy Bypass -File installer\verify.ps1
# 做三件事:
#   1. 检查安装文件是否齐全、模块文件内容是否正确;
#   2. 用 Maya 自带的 mayapy 跑纯 Python 单元测试(不打开 Maya 界面);
#   3. 用 mayapy 跑 Maya 集成测试(真的读取摄像机、真的生成运镜描述)。

param(
    [string]$MayaAppDir = '',
    [string]$MayaVersion = '',
    [string]$MayaInstallDir = ''
)

. (Join-Path $PSScriptRoot 'common.ps1')

$exitCode = 0
$failures = New-Object System.Collections.ArrayList
function Add-Failure([string]$Text) { $null = $failures.Add($Text); Write-Fail $Text }
function Add-Pass([string]$Text) { Write-Ok $Text }

try {
    Write-Head ($script:ProductName + '  自检')

    $maya = Select-MayaInstallation $MayaInstallDir $MayaVersion
    $appDir = Get-MayaAppDir $MayaAppDir
    $layout = Get-InstallLayout $appDir
    Write-Step ('Maya: ' + $maya.Version + '  (' + $maya.InstallDir + ')')
    Write-Step ('用户目录: ' + $appDir)

    # 测试代码在交付包的 tests\ 里(不随安装复制到用户目录),必须回交付包根目录执行
    $packageRoot = Get-PackageRoot
    $testsDir = Join-Path $packageRoot 'tests'

    # ---------- 1. 安装文件检查 ----------
    Write-Plain ''
    Write-Plain '[1/3] 检查安装文件'
    if (-not (Test-Path -LiteralPath $layout.ModFile)) {
        Add-Failure ('缺少模块文件: ' + $layout.ModFile)
    } else {
        $modText = Get-Content -LiteralPath $layout.ModFile -Raw
        if ($modText -match 'MAYAVERSION:' -and $modText -match 'ShotBridge') {
            Add-Pass '模块文件存在且内容正确'
        } else {
            Add-Failure '模块文件内容不正确'
        }
    }
    foreach ($relative in @(
        'camera_motion_describer\__init__.py',
        'camera_motion_describer\ui.py',
        'camera_motion_describer\analysis.py',
        'scripts\userSetup.py',
        'examples\SB_CameraMotion_Validation.ma'
    )) {
        $path = Join-Path $layout.PayloadDir $relative
        if (-not (Test-Path -LiteralPath $path)) { Add-Failure ('缺少文件: ' + $relative) }
    }
    if ($failures.Count -eq 0) { Add-Pass '程序文件齐全' }

    if (-not (Test-Path -LiteralPath $testsDir)) {
        Add-Failure ('交付包里缺少 tests 目录,无法做程序自检: ' + $testsDir)
        throw '缺少 tests 目录'
    }

    if (-not $maya.MayapyExe) {
        Add-Failure '找不到 mayapy.exe,无法做程序自检'
        throw '找不到 mayapy.exe'
    }

    # mayapy 跑的测试要用 UTF-8 与无缓冲输出
    $env:PYTHONIOENCODING = 'utf-8'
    $env:PYTHONPATH = $layout.PayloadDir
    $env:MAYA_MODULE_PATH = $layout.ModulesDir

    $tempRoot = [System.IO.Path]::GetTempPath()
    $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
    $tempLog = Join-Path $tempRoot ('shotbridge_verify_' + $stamp + '.log')
    $savedEap = $ErrorActionPreference

    Push-Location $packageRoot
    try {
        # unittest 的进度点和 Maya 的日志都会写到 stderr。PowerShell 5.1 在
        # ErrorActionPreference=Stop 时会把原生命令的 stderr 当成异常,所以这里
        # 临时放宽错误偏好,并把全部输出重定向到文件,避免日志噪音导致误判失败。
        $ErrorActionPreference = 'Continue'

        # ---------- 2. 纯 Python 单元测试 ----------
        Write-Plain ''
        Write-Plain '[2/3] 单元测试(纯 Python,不打开 Maya 界面)'
        $unitLog = Join-Path $tempRoot ('shotbridge_unit_' + $stamp + '.log')
        & $maya.MayapyExe -m unittest discover -s tests -p 'test_*.py' *> $unitLog
        $unitCode = $LASTEXITCODE
        $unitText = ''
        if (Test-Path -LiteralPath $unitLog) { $unitText = (Get-Content -LiteralPath $unitLog -Raw) }
        Set-Content -LiteralPath $tempLog -Value $unitText -Encoding UTF8
        if ($unitCode -eq 0) {
            if ($unitText -match 'Ran\s+(\d+)\s+test') {
                Add-Pass ('单元测试通过: ' + $Matches[1] + ' 个')
            } else {
                Add-Pass '单元测试通过'
            }
        } else {
            Add-Failure '单元测试失败'
            Write-Plain $unitText
        }

        # ---------- 3. Maya 集成测试 ----------
        Write-Plain ''
        Write-Plain '[3/3] Maya 集成测试(mayapy 独立模式,真的读取摄像机)'
        $integrationScript = Join-Path $testsDir 'run_maya_integration.py'
        $integrationLog = Join-Path $tempRoot ('shotbridge_integration_' + $stamp + '.log')
        & $maya.MayapyExe $integrationScript *> $integrationLog
        $integrationCode = $LASTEXITCODE
        $integrationText = ''
        if (Test-Path -LiteralPath $integrationLog) { $integrationText = (Get-Content -LiteralPath $integrationLog -Raw) }
        if ($integrationCode -eq 0) {
            Add-Pass 'Maya 集成测试通过(摄像机采样与运镜描述正常)'
        } else {
            Add-Failure 'Maya 集成测试失败'
            Write-Plain $integrationText
        }
    } finally {
        $ErrorActionPreference = $savedEap
        Pop-Location
    }

    # ---------- 结果 ----------
    Write-Head '自检结果'
    if ($failures.Count -eq 0) {
        Write-Plain '全部通过:安装是好的,可以双击“② 启动Maya并打开工具.bat”开始用。'
    } else {
        Write-Plain ('有 ' + $failures.Count + ' 项没通过:')
        foreach ($item in $failures) { Write-Plain ('  - ' + $item) }
        $exitCode = 1
    }
    if (Test-Path -LiteralPath $tempLog) {
        Write-Plain ''
        Write-Plain ('单元测试完整输出: ' + $tempLog)
    }
} catch {
    Write-Fail $_.Exception.Message
    $exitCode = 1
}
exit $exitCode
