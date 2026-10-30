"""ShotBridge 启动钩子(Maya 启动时由模块自动执行)。

Maya 启动时会执行模块目录下 scripts/userSetup.py,因此本文件不需要
修改用户自己的 userSetup.py,也不用去改 Maya 的货架配置文件。

这里只做两件事,任何一步失败都只打印信息,绝不让 Maya 启动失败:
1. 确保货架上存在 ShotBridge 的“运镜描述”按钮;
2. 如果环境变量 SHOTBRIDGE_AUTOSTART=1(由“② 启动Maya并打开工具.bat”设置),
   启动完成后自动打开描述器窗口。

批处理模式(-batch / mayapy)不涉及界面,直接跳过。
"""

from __future__ import annotations

import os

# 与启动器脚本约定:值 == "1" 时自动开窗口
AUTOSTART_ENV = "SHOTBRIDGE_AUTOSTART"


def _shotbridge_open_window_if_needed() -> None:
    """按需打开描述器窗口;窗口已存在时只提到前台,避免重复创建。"""

    try:
        from camera_motion_describer import ui
    except Exception as error:
        print("[ShotBridge] 无法载入窗口模块: %s" % (error,))
        return

    existing = getattr(ui, "_WINDOW", None)
    if existing is not None:
        try:
            existing.show()
            existing.raise_()
            existing.activateWindow()
            return
        except RuntimeError:
            pass  # 旧窗口已被 Qt 释放,走新建流程
        except Exception:
            pass

    try:
        import camera_motion_describer

        camera_motion_describer.show()
        print("[ShotBridge] 已自动打开运镜描述器")
    except Exception as error:
        print("[ShotBridge] 自动打开窗口失败: %s" % (error,))


def _shotbridge_autostart_retry() -> None:
    """兜底:启动完成后 3 秒窗口仍不存在时再试一次。

    个别机器上首次打开可能被 Maya 启动流程的其他任务打断;重试只做一次,
    窗口已存在时什么也不做,不会出现第二个窗口。
    """

    if os.environ.get(AUTOSTART_ENV) != "1":
        return

    try:
        from camera_motion_describer import ui

        if getattr(ui, "_WINDOW", None) is not None:
            return
    except Exception:
        pass

    try:
        from PySide6 import QtCore

        QtCore.QTimer.singleShot(3000, _shotbridge_open_window_if_needed)
    except Exception:
        _shotbridge_open_window_if_needed()


def _shotbridge_post_startup() -> None:
    """Maya 界面就绪后再执行:装货架按钮,按需自动打开窗口。"""

    try:
        from camera_motion_describer import maya_setup
    except Exception as error:  # 包没装好时只是没有按钮,不影响 Maya 正常使用
        print("[ShotBridge] 无法载入 camera_motion_describer: %s" % (error,))
        return

    try:
        maya_setup.install_shelf_button()
        print("[ShotBridge] 货架按钮已就绪")
    except Exception as error:
        print("[ShotBridge] 货架按钮安装失败: %s" % (error,))

    if os.environ.get(AUTOSTART_ENV) == "1":
        _shotbridge_open_window_if_needed()


def _shotbridge_boot() -> None:
    """启动入口:只在图形界面模式下把上面的工作排进 Maya 的延迟队列。"""

    try:
        import maya.cmds as cmds
        import maya.utils

        if cmds.about(batch=True):  # mayapy / 命令行批处理:没有界面
            return
    except Exception:
        return

    # executeDeferred:等 Maya 主界面真正就绪后再动手,避免抢在界面加载前调用 UI
    maya.utils.executeDeferred(_shotbridge_post_startup)
    # 兜底重试:正常情况窗口已在上面打开,这里只处理极端情况下首次打开失败
    maya.utils.executeDeferred(_shotbridge_autostart_retry)


_shotbridge_boot()
