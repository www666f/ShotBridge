# -*- coding: utf-8 -*-
# ShotBridge 安装脚本公共函数(被 install / uninstall / launch / verify 调用)
# 调用方式:powershell.exe -NoProfile -ExecutionPolicy Bypass -File "installer\install.ps1"
# 本文件必须保存为 UTF-8(带 BOM),Windows PowerShell 5.1 才能正确解码中文。

$ErrorActionPreference = 'Stop'

# 控制台统一用 UTF-8 输出,.bat 里已经 chcp 65001,避免中文乱码
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$script:ProductName = 'ShotBridge 摄像机运镜描述器'
$script:ModuleName  = 'ShotBridge'
$script:AutostartEnv = 'SHOTBRIDGE_AUTOSTART'
$script:CRLF = [string][char]13 + [char]10

function Write-Head([string]$Text) {
    Write-Host ''
    Write-Host ('==== ' + $Text + ' ====') -ForegroundColor Cyan
}

function Write-Step([string]$Text) { Write-Host ('  - ' + $Text) }
function Write-Ok([string]$Text)   { Write-Host ('  [完成] ' + $Text) -ForegroundColor Green }
function Write-Warn([string]$Text) { Write-Host ('  [注意] ' + $Text) -ForegroundColor Yellow }
function Write-Fail([string]$Text) { Write-Host ('  [失败] ' + $Text) -ForegroundColor Red }
function Write-Plain([string]$Text){ Write-Host ('  ' + $Text) }

function Get-PackageRoot {
    # installer\ 的上一级 = 交付包根目录
    return (Split-Path -Parent $PSScriptRoot)
}

function Get-MayaAppDir([string]$Override) {
    # Maya 用户目录(存放 modules、prefs、shelves)
    if ($Override) { return $Override }
    if ($env:MAYA_APP_DIR) { return $env:MAYA_APP_DIR }
    $docs = [Environment]::GetFolderPath('MyDocuments')
    if (-not $docs) { $docs = Join-Path $env:USERPROFILE 'Documents' }
    return (Join-Path $docs 'maya')
}

function Add-MayaInstallation($List, [string]$Location, [string]$Version) {
    # 校验目录里确实有 bin\maya.exe,再收进列表(按路径去重)
    if (-not $Location) { return }
    $Location = $Location.Trim().TrimEnd('\')
    $mayaExe = Join-Path $Location 'bin\maya.exe'
    if (-not (Test-Path -LiteralPath $mayaExe)) { return }
    foreach ($item in $List) {
        if ($item.InstallDir -ieq $Location) { return }
    }
    $mayapyExe = Join-Path $Location 'bin\mayapy.exe'
    if (-not (Test-Path -LiteralPath $mayapyExe)) { $mayapyExe = '' }
    $null = $List.Add([pscustomobject]@{
        Version    = $Version
        InstallDir = $Location
        MayaExe    = $mayaExe
        MayapyExe  = $mayapyExe
    })
}

function Get-MayaInstallations {
    # 主路径:注册表 HKLM\SOFTWARE\Autodesk\Maya\<版本>\Setup\InstallPath 的 MAYA_INSTALL_LOCATION
    # (安装目录可能带空格或装在非 C 盘,不能靠扫目录猜)
    $found = New-Object System.Collections.ArrayList
    $registryRoots = @(
        'HKLM:\SOFTWARE\Autodesk\Maya',
        'HKLM:\SOFTWARE\WOW6432Node\Autodesk\Maya'
    )
    foreach ($root in $registryRoots) {
        if (-not (Test-Path $root)) { continue }
        foreach ($key in (Get-ChildItem -Path $root -ErrorAction SilentlyContinue)) {
            $version = $key.PSChildName
            if ($version -notmatch '^20\d\d$') { continue }
            $installKey = Join-Path $key.PSPath 'Setup\InstallPath'
            if (-not (Test-Path $installKey)) { continue }
            $props = Get-ItemProperty -Path $installKey -ErrorAction SilentlyContinue
            if (-not $props) { continue }
            $location = $props.MAYA_INSTALL_LOCATION
            if (-not $location) { $location = $props.'(default)' }
            Add-MayaInstallation $found $location $version
        }
    }

    # 兜底:注册表读不到时扫描常见安装位置
    if ($found.Count -eq 0) {
        $candidates = New-Object System.Collections.ArrayList
        $programDirs = @($env:ProgramFiles, [Environment]::GetEnvironmentVariable('ProgramFiles(x86)'))
        foreach ($base in $programDirs) {
            if (-not $base) { continue }
            $autodesk = Join-Path $base 'Autodesk'
            if (-not (Test-Path $autodesk)) { continue }
            foreach ($dir in (Get-ChildItem -Path $autodesk -Directory -Filter 'Maya*' -ErrorAction SilentlyContinue)) {
                $ver = ''
                if ($dir.Name -match 'Maya\s*(20\d\d)') { $ver = $Matches[1] }
                $null = $candidates.Add([pscustomobject]@{ Path = $dir.FullName; Version = $ver })
            }
        }
        foreach ($drive in (Get-PSDrive -PSProvider FileSystem -ErrorAction SilentlyContinue)) {
            if (-not $drive.Root) { continue }
            foreach ($dir in (Get-ChildItem -Path $drive.Root -Directory -Filter 'Maya*' -ErrorAction SilentlyContinue)) {
                $ver = ''
                if ($dir.Name -match '(20\d\d)') { $ver = $Matches[1] }
                $null = $candidates.Add([pscustomobject]@{ Path = $dir.FullName; Version = $ver })
            }
        }
        foreach ($candidate in $candidates) {
            Add-MayaInstallation $found $candidate.Path $candidate.Version
        }
    }

    # 版本号从高到低
    return @($found | Sort-Object -Property @{ Expression = { if ($_.Version -match '^\d+$') { [int]$_.Version } else { 0 } } } -Descending)
}

function Select-MayaInstallation([string]$MayaInstallDir, [string]$MayaVersion) {
    # 允许显式指定安装目录(用于测试或非标准安装)
    if ($MayaInstallDir) {
        $list = New-Object System.Collections.ArrayList
        $ver = $MayaVersion
        if (-not $ver -and ($MayaInstallDir -match '(20\d\d)')) { $ver = $Matches[1] }
        Add-MayaInstallation $list $MayaInstallDir $ver
        if ($list.Count -eq 0) {
            throw ('指定的 Maya 目录里没有 bin\maya.exe: ' + $MayaInstallDir)
        }
        return $list[0]
    }

    $all = Get-MayaInstallations
    if ($all.Count -eq 0) {
        throw '没有在注册表或常见安装目录里找到 Maya。请确认已安装 Maya 2026,或用 -MayaInstallDir 指定安装目录。'
    }
    if ($MayaVersion) {
        foreach ($item in $all) {
            if ($item.Version -eq $MayaVersion) { return $item }
        }
        throw ('没有找到 Maya ' + $MayaVersion + '。已找到: ' + (($all | ForEach-Object { $_.Version }) -join ', '))
    }
    return $all[0]
}

function Get-PackageInfo([string]$PackageRoot) {
    # 交付包自检:必须的目录与文件都在,并读出 app 里的版本号
    $payloadSource = Join-Path $PackageRoot 'app'
    $initFile = Join-Path $payloadSource 'camera_motion_describer\__init__.py'
    $hookFile = Join-Path $payloadSource 'scripts\userSetup.py'
    $demoFile = Join-Path $payloadSource 'examples\SB_CameraMotion_Validation.ma'

    $missing = New-Object System.Collections.ArrayList
    $required = @(
        @{ Path = $initFile; Name = 'app\camera_motion_describer\__init__.py' },
        @{ Path = $hookFile; Name = 'app\scripts\userSetup.py' },
        @{ Path = $demoFile; Name = 'app\examples\SB_CameraMotion_Validation.ma' }
    )
    foreach ($pair in $required) {
        if (-not (Test-Path -LiteralPath $pair.Path)) { $null = $missing.Add($pair.Name) }
    }
    if ($missing.Count -gt 0) {
        throw ('交付包不完整,缺少: ' + ($missing -join '、') + '。请重新解压完整的压缩包。')
    }

    $version = '1.0.0'
    $text = Get-Content -LiteralPath $initFile -Raw -Encoding UTF8
    if ($text -match '__version__\s*=\s*"([^"]+)"') { $version = $Matches[1] }

    $fileCount = (Get-ChildItem -Path $payloadSource -Recurse -File).Count
    return [pscustomobject]@{
        PayloadSource = $payloadSource
        Version       = $version
        FileCount     = $fileCount
    }
}

function Get-InstallLayout([string]$MayaAppDir) {
    $modulesDir = Join-Path $MayaAppDir 'modules'
    $payloadDir = Join-Path $MayaAppDir $script:ModuleName
    return [pscustomobject]@{
        MayaAppDir = $MayaAppDir
        ModulesDir = $modulesDir
        PayloadDir = $payloadDir
        ModFile    = Join-Path $modulesDir ($script:ModuleName + '.mod')
        BackupDir  = Join-Path $MayaAppDir ($script:ModuleName + '_backup')
    }
}

function Get-ModuleFileContent([string]$Version) {
    # 相对路径 ../ShotBridge 相对 .mod 所在目录解析,安装路径含空格或中文都不受影响
    $lines = @(
        ('+ MAYAVERSION:' + $Version + ' ' + $script:ModuleName + ' 1.0.0 ../' + $script:ModuleName),
        'PYTHONPATH +:= .'
    )
    return (($lines -join $script:CRLF) + $script:CRLF)
}

function Write-AsciiFile([string]$Path, [string]$Content) {
    # .mod 由 Maya 读取,只用 ASCII,避免编码歧义
    $parent = Split-Path -Parent $Path
    if ($parent -and -not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }
    [System.IO.File]::WriteAllText($Path, $Content, (New-Object System.Text.ASCIIEncoding))
}
