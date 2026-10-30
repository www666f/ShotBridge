"""Maya 侧安装辅助(与纯 Python 分析内核分离)。

仅被 Maya 环境调用:安装货架按钮、打开验证场景;
所有 maya.cmds / maya.mel 的调用都在函数内部延迟导入。
"""

from __future__ import annotations

import os


# 货架相关常量:页签名与按钮显示名
SHELF_NAME = "ShotBridge"
BUTTON_LABEL = "运镜描述"


def repository_root() -> str:
    """仓库根目录(本文件位于其下的 camera_motion_describer 包内)。"""

    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def demo_scene_path() -> str:
    """验证场景的绝对路径(examples/SB_CameraMotion_Validation.ma)。"""

    return os.path.join(repository_root(), "examples", "SB_CameraMotion_Validation.ma")


def install_shelf_button() -> str:
    """在 Maya 货架上幂等地安装一个 ShotBridge 按钮,返回页签名。

    幂等 = 反复执行不会出现重复按钮:
    先确保 SHELF_NAME 页签存在,再按固定文案(annotation)删掉旧按钮,
    最后重建并保存到用户偏好。
    """

    import maya.cmds as cmds
    import maya.mel as mel

    # $gShelfTopLevel 是货架顶层控件;cmds 无直接查询命令,
    # 用 mel.eval 读取该 MEL 全局变量是官方惯用做法
    shelf_parent = mel.eval("$tmp = $gShelfTopLevel")
    # 页签不存在则创建(创建时传 parent 指定挂在货架顶层下)
    if not cmds.shelfLayout(SHELF_NAME, exists=True):
        cmds.shelfLayout(SHELF_NAME, parent=shelf_parent)

    # 遍历页签里的子控件:annotation 与目标文案一致的旧按钮先删除(避免重复)
    for child in cmds.shelfLayout(SHELF_NAME, query=True, childArray=True) or []:
        if cmds.objectTypeUI(child) == "shelfButton":
            annotation = cmds.shelfButton(child, query=True, annotation=True) or ""
            if annotation == "打开 ShotBridge 摄像机运镜描述器":
                cmds.deleteUI(child)

    # 按钮点击时执行的 Python 代码(字符串形式,由 Maya 在点击时才执行)
    command = "import camera_motion_describer; camera_motion_describer.show()"
    # 创建按钮:label 显示名、annotation 悬停提示(兼作去重标识)、
    # image1 为内置图标名、sourceType="python" 表示 command 是 Python 而不是 MEL
    cmds.shelfButton(
        parent=SHELF_NAME,
        label=BUTTON_LABEL,
        annotation="打开 ShotBridge 摄像机运镜描述器",
        image1="camera.png",
        sourceType="python",
        command=command,
    )
    # 持久化货架到用户偏好;个别环境下保存可能失败,此时按钮在本次
    # 会话内仍可用,Maya 正常退出时一般会再次保存,故静默忽略
    try:
        cmds.saveAllShelves(shelf_parent)
    except Exception:
        pass
    return SHELF_NAME


def open_demo_and_show():
    """打开验证场景 → 安装货架按钮 → 选中测试相机 → 弹出描述器窗口。

    注意:会替换当前场景(force=True),仅在用户明确要求打开验证场景时调用。
    """

    import maya.cmds as cmds

    path = demo_scene_path()
    if not os.path.exists(path):
        raise RuntimeError("找不到测试场景：" + path)
    # 打开场景(跳过未保存提示)、安装货架、预选测试相机,最后弹出窗口
    cmds.file(path, open=True, force=True)
    install_shelf_button()
    cmds.select("SB_TestCamera", replace=True)
    from .ui import show  # 延迟导入:非 Maya 环境不会加载界面模块

    return show()
