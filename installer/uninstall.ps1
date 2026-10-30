# -*- coding: utf-8 -*-
# ShotBridge 摄像机运镜描述器 - 卸载脚本
# 用法(双击“③ 卸载.bat”即可):
#   powershell.exe -NoProfile -ExecutionPolicy Bypass -File installer\uninstall.ps1
# 参数:
#   -MayaAppDir  指定 Maya 用户目录(默认 文档\maya)
#   -Yes         不再询问,直接卸载
#   -NoRestore   不恢复安装前备份的旧版本
#   -RemoveShelf 同时删除 Maya 工具架上的 ShotBridge 页签(默认保留)

param(
    [string]$MayaAppDir = '',
    [switch]$Yes,
    [switch]$NoRestore,
    [switch]$RemoveShelf
)

. (Join-Path $PSScriptRoot 'common.ps1')

$exitCode = 0
try {
    Write-Head ($script:ProductName + '  卸载')

    $appDir = Get-MayaAppDir $MayaAppDir
    $layout = Get-InstallLayout $appDir

    $hasPayload = Test-Path -LiteralPath $layout.PayloadDir
    $hasMod = Test-Path -LiteralPath $layout.ModFile
    if (-not $hasPayload -and -not $hasMod) {
        Write-Plain ('没有找到已安装的 ShotBridge(检查位置: ' + $appDir + ')。')
        Write-Plain '可能已经卸载过,或当初装到了别的 Maya 用户目录。'
        exit 0
    }

    Write-Plain '将删除:'
    if ($hasMod) { Write-Plain ('  - ' + $layout.ModFile) }
    if ($hasPayload) { Write-Plain ('  - ' + $layout.PayloadDir) }
    Write-Plain '将保留:'
    Write-Plain ('  - 备份目录(如果安装前有旧版本): ' + $layout.BackupDir)
    Write-Plain '  - 你自己的 Maya 场景、偏好设置和工具架页签'

    if (-not $Yes) {
        Write-Plain ''
        $answer = Read-Host '确认卸载吗?输入 y 回车继续,直接回车取消'
        if ($answer.Trim().ToLower() -ne 'y') {
            Write-Plain '已取消,没有删除任何文件。'
            exit 0
        }
    }

    if ($hasMod) {
        Remove-Item -LiteralPath $layout.ModFile -Force
        Write-Ok '已删除模块文件'
    }
    if ($hasPayload) {
        Remove-Item -LiteralPath $layout.PayloadDir -Recurse -Force
        Write-Ok '已删除程序目录'
    }

    # 恢复安装前备份的旧模块文件(例如开发者原来指向源码目录的 .mod)
    if (-not $NoRestore -and (Test-Path -LiteralPath $layout.BackupDir)) {
        $oldMods = @(Get-ChildItem -Path $layout.BackupDir -Filter 'ShotBridge.mod.bak-*' -File -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending)
        if ($oldMods.Count -gt 0) {
            Copy-Item -LiteralPath $oldMods[0].FullName -Destination $layout.ModFile -Force
            Write-Ok ('已恢复安装前的旧模块文件: ' + $oldMods[0].FullName)
        }
        $oldPayloads = @(Get-ChildItem -Path $layout.BackupDir -Directory -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending)
        if ($oldPayloads.Count -gt 0) {
            Write-Warn ('旧程序目录备份仍在: ' + $oldPayloads[0].FullName + ' ,需要时可手动改名回 ' + $script:ModuleName)
        }
    }

    # 工具架页签是可选项:它是 Maya 偏好的一部分,默认不碰
    if ($RemoveShelf) {
        $shelfFiles = @(Get-ChildItem -Path $appDir -Recurse -File -Filter 'shelf_ShotBridge.mel' -ErrorAction SilentlyContinue)
        foreach ($file in $shelfFiles) {
            Remove-Item -LiteralPath $file.FullName -Force
            Write-Ok ('已删除工具架页签文件: ' + $file.FullName)
        }
    } else {
        Write-Plain ''
        Write-Plain '提示: Maya 工具架上的 ShotBridge 页签没有删除(它属于 Maya 偏好)。'
        Write-Plain '      在 Maya 里右键该页签选 Delete Shelf 即可;'
        Write-Plain '      或者重新运行卸载并加 -RemoveShelf 参数。'
    }

    Write-Head '卸载完成'
    Write-Plain '下次启动 Maya 就不会再加载 ShotBridge 了。'
} catch {
    Write-Fail $_.Exception.Message
    $exitCode = 1
}
exit $exitCode
