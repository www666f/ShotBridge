"""创建确定性的 Maya 验证场景(供手动与集成测试使用)。

场景内容全部写死,保证每次构建结果一致:
- 时间单位 film(24fps),播放范围 1–120 帧;
- 一台 SB_TestCamera:先推近,再横移转向,最后拉远并变焦;
- 两个可见目标方块:SB_Target_A(轴心被刻意挪远)、SB_Target_B;
- 第 47 帧刻意插入一帧 translateY 跳变,用于验证毛刺清洗;
- 关键帧切线按预期运动方式(线性 / 自动)设置,便于速度曲线分析。
"""

from __future__ import annotations

import math
import os


def build_demo_scene(save_path: str) -> str:
    """重建并保存一个确定性验证场景,返回保存后的绝对路径。"""

    import maya.cmds as cmds

    # 清空当前场景、时间单位设为 film(24fps)、播放范围 1–120
    cmds.file(new=True, force=True)
    cmds.currentUnit(time="film")
    cmds.playbackOptions(minTime=1, maxTime=120, animationStartTime=1, animationEndTime=120)

    # 创建测试相机并固定节点名(便于集成测试断言)
    camera_transform, camera_shape = cmds.camera(name="SB_TestCamera")
    camera_transform = cmds.rename(camera_transform, "SB_TestCamera")
    camera_shape = cmds.listRelatives(camera_transform, shapes=True)[0]
    camera_shape = cmds.rename(camera_shape, "SB_TestCameraShape")
    # 固定镜头参数:35mm、f/2.8、开启景深、对焦 20、快门角 180°
    cmds.setAttr(camera_shape + ".focalLength", 35.0)
    cmds.setAttr(camera_shape + ".fStop", 2.8)
    cmds.setAttr(camera_shape + ".depthOfField", True)
    cmds.setAttr(camera_shape + ".focusDistance", 20.0)
    cmds.setAttr(camera_shape + ".shutterAngle", 180.0)

    # 两个可见目标方块;Target A 的轴心被刻意挪到 (30, 1, 0),
    # 用于验证工具使用“网格包围盒中心”而不是轴心作为主体位置
    target_a = cmds.polyCube(name="SB_Target_A", width=2.0, height=2.0, depth=2.0)[0]
    target_b = cmds.polyCube(name="SB_Target_B", width=2.0, height=2.0, depth=2.0)[0]
    cmds.setAttr(target_a + ".translateX", -1.5)
    cmds.setAttr(target_a + ".translateY", 1.0)
    cmds.setAttr(target_b + ".translateX", 1.5)
    cmds.setAttr(target_b + ".translateY", 1.0)
    cmds.xform(target_a, worldSpace=True, pivots=(30.0, 1.0, 0.0))  # 有意弄偏轴心

    # 关键帧表:帧号 → (translateX, translateY, translateZ, rotateY, focalLength)
    # 相机朝局部 -Z 看: 世界 Z 减小 = 推近;后段位移跟随旋转后的局部 +Z
    keys = {
        1: (0.0, 1.0, 20.0, 0.0, 35.0),
        24: (0.0, 1.0, 20.0, 0.0, 35.0),
        60: (0.0, 1.0, 10.0, 0.0, 35.0),
        90: (5.0, 1.0, 10.0, math.degrees(math.atan2(5.0, 10.0)), 35.0),
        120: (9.0, 1.0, 18.0, math.degrees(math.atan2(5.0, 10.0)), 50.0),
    }
    for frame, (x, y, z, rotate_y, focal) in keys.items():
        # 逐关键帧写入位置 / 旋转 / 焦距(setKeyframe 会自动创建动画曲线)
        cmds.setKeyframe(camera_transform, attribute="translateX", time=frame, value=x)
        cmds.setKeyframe(camera_transform, attribute="translateY", time=frame, value=y)
        cmds.setKeyframe(camera_transform, attribute="translateZ", time=frame, value=z)
        cmds.setKeyframe(camera_transform, attribute="rotateY", time=frame, value=rotate_y)
        cmds.setKeyframe(camera_shape, attribute="focalLength", time=frame, value=focal)

    # 第 47 帧刻意插入一帧 Y 跳变(1 → 8 → 1):
    # 应触发“单帧跳变”警告,但最终提示词中不得出现升降运动
    cmds.setKeyframe(camera_transform, attribute="translateY", time=46, value=1.0)
    cmds.setKeyframe(camera_transform, attribute="translateY", time=47, value=8.0)
    cmds.setKeyframe(camera_transform, attribute="translateY", time=48, value=1.0)

    # 1–90 帧设为线性切线(匀速段,便于断言);
    # 90–120 帧的拉远 / 变焦保留自动切线,形成非线性速度供速度曲线分析
    cmds.keyTangent(
        camera_transform,
        time=(1, 90),
        inTangentType="linear",
        outTangentType="linear",
    )
    # 末段 X / Z 位移用 auto 切线平滑缓动;
    # 旋转与焦距边界保持精确,使预期分段无歧义
    for attribute in ("translateX", "translateZ"):
        cmds.keyTangent(
            camera_transform,
            attribute=attribute,
            time=(90, 120),
            inTangentType="auto",
            outTangentType="auto",
        )
    # 焦距曲线整体恢复为线性,只靠关键帧值体现变化
    cmds.keyTangent(
        camera_shape,
        attribute="focalLength",
        inTangentType="linear",
        outTangentType="linear",
    )

    # 回到首帧、预选三个对象(相机 + 两个目标),保存为 Maya ASCII
    cmds.currentTime(1)
    cmds.select([camera_transform, target_a, target_b], replace=True)
    save_path = os.path.abspath(save_path)
    os.makedirs(os.path.dirname(save_path), exist_ok=True)
    cmds.file(rename=save_path)
    cmds.file(save=True, type="mayaAscii", force=True)
    return save_path


# 直接执行本文件不构建场景:必须经由 scripts/build_demo_scene.py 的流程
if __name__ == "__main__":
    raise SystemExit("请通过 scripts/build_demo_scene.py 创建测试场景。")
