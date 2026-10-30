"""Maya 适配层:读取已求值的场景数据,交给纯 Python 分析内核。

主要职责:
- 节点解析:把用户选择解析为「摄像机 Transform + Shape」与主体列表;
- 逐帧采样:沿播放范围逐帧求值,记录相机世界矩阵与镜头参数;
- 主体中心:优先用网格包围盒中心,避免建模轴心偏移造成误判;
- 环境探测:帧率(时间单位)、世界朝上轴(Z-up / Y-up)。

所有 maya.cmds / maya.api.OpenMaya 调用都延迟在函数内部导入,
因此本模块也能在普通 Python 环境中被安全导入和测试。
"""

from __future__ import annotations

import math
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .analysis import analyze_samples
from .math3d import distance
from .models import AnalysisConfig, CameraSample, MotionReport, Vec3


# 进度回调:(已处理数量, 总数) → None,UI 用它刷新进度条
ProgressCallback = Callable[[int, int], None]


def _maya_modules():
    """延迟导入 Maya 模块(maya.cmds 命令层 + OpenMaya API 2.0)。"""

    import maya.api.OpenMaya as om
    import maya.cmds as cmds

    return cmds, om


def _as_transform(node: str) -> str:
    """把任意节点名规范化为 Transform 的全路径名。

    - nodeType == "transform":本身即 Transform,返回 long 名;
    - 否则(如 shape):取它的父节点(Transform);
    - 都不满足时退回 long 名,由调用方继续校验。
    """

    cmds, _ = _maya_modules()
    # objExists 校验对象存在;ls(long=True) 返回带 | 前缀的完整 DAG 路径
    if not cmds.objExists(node):
        raise ValueError(f"对象不存在：{node}")
    if cmds.nodeType(node) == "transform":
        return cmds.ls(node, long=True)[0]
    # listRelatives(parent=True) 取父节点;shape 的父节点就是它的 Transform
    parents = cmds.listRelatives(node, parent=True, fullPath=True) or []
    if parents:
        return parents[0]
    return cmds.ls(node, long=True)[0]


def resolve_camera(node: str) -> Tuple[str, str]:
    """把节点(Transform 或 Shape)解析为 (摄像机Transform, 摄像机Shape)。"""

    cmds, _ = _maya_modules()
    if not node or not cmds.objExists(node):
        raise ValueError("请选择一个有效的摄像机。")
    node_type = cmds.nodeType(node)
    # 传入的直接是 camera shape:向上找它的 Transform
    if node_type == "camera":
        shape = cmds.ls(node, long=True)[0]
        parent = cmds.listRelatives(shape, parent=True, fullPath=True) or []
        if not parent:
            raise ValueError("摄像机 Shape 没有有效的 Transform。")
        return parent[0], shape
    # 传入的是 Transform(或其它节点):下找 camera 类型的 shape
    transform = _as_transform(node)
    # noIntermediate=True 排除中间(构造历史)形状,避免选错节点
    shapes = cmds.listRelatives(
        transform, shapes=True, noIntermediate=True, fullPath=True
    ) or []
    camera_shapes = [shape for shape in shapes if cmds.nodeType(shape) == "camera"]
    if not camera_shapes:
        raise ValueError(f"所选对象不是摄像机：{node}")
    return transform, camera_shapes[0]


def camera_from_selection() -> Tuple[str, str]:
    """从 Maya 当前选择中找第一个摄像机,返回 (Transform, Shape)。"""

    cmds, _ = _maya_modules()
    # ls(selection=True):当前选择列表;逐个尝试,非摄像机则跳过
    for node in cmds.ls(selection=True, long=True) or []:
        try:
            return resolve_camera(node)
        except ValueError:
            continue
    raise ValueError("当前选择中没有摄像机。")


def targets_from_selection(excluded_camera: Optional[str] = None) -> List[str]:
    """从当前选择中收集被摄主体(排除摄像机与重复项),返回 Transform 列表。

    excluded_camera 传入正在使用的摄像机,避免把相机自己当成主体。
    """

    cmds, _ = _maya_modules()
    excluded = _as_transform(excluded_camera) if excluded_camera else None
    result = []
    for node in cmds.ls(selection=True, long=True) or []:
        transform = _as_transform(node)
        if transform == excluded or transform in result:
            continue  # 跳过相机本体与重复目标
        shapes = cmds.listRelatives(
            transform, shapes=True, noIntermediate=True, fullPath=True
        ) or []
        if any(cmds.nodeType(shape) == "camera" for shape in shapes):
            continue  # 其它摄像机也排除
        result.append(transform)
    return result


def playback_frame_range() -> Tuple[int, int]:
    """读取 Maya 播放范围,向内取整为整数帧区间 [start, end]。"""

    cmds, _ = _maya_modules()
    # playbackOptions 的 minTime / maxTime 可能是小数帧:向内取整
    start = int(math.ceil(cmds.playbackOptions(query=True, minTime=True)))
    end = int(math.floor(cmds.playbackOptions(query=True, maxTime=True)))
    if end <= start:
        raise ValueError("播放范围至少需要包含两个整数帧。")
    return start, end


def frames_in_range(start_frame: float, end_frame: float) -> List[int]:
    """把小数帧区间转换为逐帧整数列表(含两端)。"""

    start = int(math.ceil(start_frame))
    end = int(math.floor(end_frame))
    if end <= start:
        raise ValueError("分析范围至少需要包含两个整数帧。")
    return list(range(start, end + 1))


def maya_fps() -> float:
    """读取 Maya 当前时间单位并换算为 fps(提示词秒数换算用)。"""

    cmds, _ = _maya_modules()
    unit = cmds.currentUnit(query=True, time=True)
    # 时间单位 → 帧率对照(Maya 内置命名单位)
    named = {
        "game": 15.0,
        "film": 24.0,
        "pal": 25.0,
        "ntsc": 30.0,
        "show": 48.0,
        "palf": 50.0,
        "ntscf": 60.0,
    }
    if unit in named:
        return named[unit]
    if unit.endswith("fps"):
        try:
            return float(unit[:-3])
        except ValueError:
            pass
    return 24.0


def _world_position_and_orientation(transform: str):
    """读取 Transform 的世界矩阵,提取位置与四元数旋转。"""

    cmds, om = _maya_modules()
    # xform(matrix=True) 返回 16 个浮点数的行主序 4x4 世界矩阵
    values = cmds.xform(transform, query=True, worldSpace=True, matrix=True)
    matrix = om.MMatrix(values)
    # 矩阵第 13–15 个元素(下标 12–14)即世界平移
    position = (float(values[12]), float(values[13]), float(values[14]))
    # 用 OpenMaya 的 MTransformationMatrix 正确分解出旋转四元数 (x, y, z, w)
    quaternion = om.MTransformationMatrix(matrix).rotation(asQuaternion=True)
    orientation = (
        float(quaternion.x),
        float(quaternion.y),
        float(quaternion.z),
        float(quaternion.w),
    )
    return position, orientation


def _mesh_shapes(transform: str) -> List[str]:
    """收集 Transform 自身及所有后代的网格(mesh)形状节点。"""

    cmds, _ = _maya_modules()
    # allDescendents=True:递归取所有后代,支持成组 / 层级的模型
    shapes = cmds.listRelatives(
        transform,
        allDescendents=True,
        shapes=True,
        noIntermediate=True,
        fullPath=True,
    ) or []
    # 再补上直接子形状(与后代结果去重)
    direct_shapes = cmds.listRelatives(
        transform, shapes=True, noIntermediate=True, fullPath=True
    ) or []
    shapes.extend(shape for shape in direct_shapes if shape not in shapes)
    return [shape for shape in shapes if cmds.nodeType(shape) == "mesh"]


def _bbox_bounds(transform: str) -> Optional[Tuple[Vec3, Vec3]]:
    """读取单个目标全部网格的世界包围盒；无网格时返回 None。"""

    cmds, _ = _maya_modules()
    mesh_shapes = _mesh_shapes(transform)
    if not mesh_shapes:
        return None
    # exactWorldBoundingBox 返回 [xmin, ymin, zmin, xmax, ymax, zmax],
    # ignoreInvisible=True 跳过隐藏网格
    bounds = cmds.exactWorldBoundingBox(mesh_shapes, ignoreInvisible=True)
    if len(bounds) != 6 or not all(math.isfinite(value) for value in bounds):
        return None
    return (
        (float(bounds[0]), float(bounds[1]), float(bounds[2])),
        (float(bounds[3]), float(bounds[4]), float(bounds[5])),
    )


def _bbox_center(transform: str) -> Tuple[Optional[Vec3], Optional[float]]:
    """计算网格的世界包围盒中心与对角线长度;无网格时返回 (None, None)。"""

    bounds = _bbox_bounds(transform)
    if bounds is None:
        return None, None
    minimum, maximum = bounds
    # 中心 = 各轴最小 / 最大值的平均值
    center = (
        (minimum[0] + maximum[0]) * 0.5,
        (minimum[1] + maximum[1]) * 0.5,
        (minimum[2] + maximum[2]) * 0.5,
    )
    # 对角线长度(用于“相对阈值”尺度与轴心偏移判断)
    diagonal = math.sqrt(
        (maximum[0] - minimum[0]) ** 2
        + (maximum[1] - minimum[1]) ** 2
        + (maximum[2] - minimum[2]) ** 2
    )
    return center, diagonal


def target_visual_center(transform: str) -> Tuple[Vec3, Optional[float], bool]:
    """返回 (可见中心, 包围盒对角线, 是否使用了网格包围盒)。

    优先用网格包围盒中心(贴合视觉观感);
    没有任何网格时退回轴心(rotatePivot),此时第三个返回值为 False。
    """

    cmds, _ = _maya_modules()
    transform = _as_transform(transform)
    center, diagonal = _bbox_center(transform)
    if center is not None:
        return center, diagonal, True
    # 无网格兜底:用旋转轴心的世界坐标作为目标点
    pivot = cmds.xform(transform, query=True, worldSpace=True, rotatePivot=True)
    return (float(pivot[0]), float(pivot[1]), float(pivot[2])), None, False


def weighted_target_center(
    targets: Sequence[str], weights: Optional[Dict[str, float]] = None
) -> Optional[Vec3]:
    """把多个主体的可见中心按权重合成为一个整体中心。

    weights 为 None 时各目标等权(1.0);权重 ≤ 0 的目标被忽略;
    总权重为 0 时报错。
    """

    if not targets:
        return None
    weighted = [0.0, 0.0, 0.0]
    total_weight = 0.0
    for target in targets:
        center, _, _ = target_visual_center(target)
        weight = float((weights or {}).get(target, 1.0))
        if weight <= 0.0:
            continue
        weighted[0] += center[0] * weight
        weighted[1] += center[1] * weight
        weighted[2] += center[2] * weight
        total_weight += weight
    if total_weight <= 0.0:
        raise ValueError("目标对象的总权重必须大于零。")
    return (
        weighted[0] / total_weight,
        weighted[1] / total_weight,
        weighted[2] / total_weight,
    )


def combined_target_bounds(
    targets: Sequence[str], weights: Optional[Dict[str, float]] = None
) -> Optional[Tuple[Vec3, Vec3]]:
    """合并所有有效目标的网格包围盒；权重仅决定目标是否参与，不缩放尺寸。"""

    boxes = []
    for target in targets:
        if float((weights or {}).get(target, 1.0)) <= 0.0:
            continue
        bounds = _bbox_bounds(_as_transform(target))
        if bounds is not None:
            boxes.append(bounds)
    if not boxes:
        return None
    return (
        tuple(min(box[0][axis] for box in boxes) for axis in range(3)),
        tuple(max(box[1][axis] for box in boxes) for axis in range(3)),
    )


def inspect_target_pivots(targets: Sequence[str]) -> List[str]:
    """检查每个主体的轴心与网格中心是否明显偏离,返回中文警告列表。

    仅提示(不修改任何数据):轴心偏移通常意味着建模 / 绑定遗留问题,
    提醒用户确认“以网格中心作为目标点”是否符合预期。
    """

    cmds, _ = _maya_modules()
    warnings = []
    for target in targets:
        transform = _as_transform(target)
        center, diagonal, used_mesh = target_visual_center(transform)
        short_name = transform.rsplit("|", 1)[-1]
        if not used_mesh:
            warnings.append(
                f"目标 {short_name} 没有可见网格，已使用其轴心位置作为目标点。"
            )
            continue
        # 轴心与包围盒中心的距离 相对 对角线长度,超过 0.5 倍视为明显偏离
        pivot_values = cmds.xform(
            transform, query=True, worldSpace=True, rotatePivot=True
        )
        pivot = (float(pivot_values[0]), float(pivot_values[1]), float(pivot_values[2]))
        if diagonal and diagonal > 1.0e-8:
            ratio = distance(pivot, center) / diagonal
            if ratio > 0.50:
                warnings.append(
                    f"目标 {short_name} 的轴心明显偏离网格中心"
                    f"（偏移约为包围盒对角线的 {ratio:.2f} 倍）；"
                    "本次已使用网格包围盒中心。"
                )
    return warnings


def sample_camera(
    camera: str,
    targets: Sequence[str] = (),
    start_frame: Optional[float] = None,
    end_frame: Optional[float] = None,
    target_weights: Optional[Dict[str, float]] = None,
    progress_callback: Optional[ProgressCallback] = None,
) -> Tuple[List[CameraSample], List[str], Tuple[str, str]]:
    """逐帧求值场景,采样相机的世界位姿与镜头参数。

    返回 (采样列表, 主体相关警告, (相机Transform, 相机Shape))。
    实现方式:逐帧把场景时间拨到目标帧(currentTime edit+update),
    让 Maya 求值整条动画链,再读取相机矩阵与参数;
    采样结束后恢复原始当前帧。
    """

    cmds, _ = _maya_modules()
    camera_transform, camera_shape = resolve_camera(camera)
    targets = [_as_transform(target) for target in targets]
    if start_frame is None or end_frame is None:
        start_frame, end_frame = playback_frame_range()
    frames = frames_in_range(start_frame, end_frame)
    warnings = inspect_target_pivots(targets)
    samples: List[CameraSample] = []
    original_time = cmds.currentTime(query=True)  # 记下原当前帧,采样后还原
    try:
        for index, frame in enumerate(frames, start=1):
            # 拨到目标帧并强制求值(update=True 保证立即刷新)
            cmds.currentTime(frame, edit=True, update=True)
            position, orientation = _world_position_and_orientation(camera_transform)
            # 整体主体中心(每个目标按权重合成,本帧即时求值)
            target_center = weighted_target_center(targets, target_weights)
            target_bounds = combined_target_bounds(targets, target_weights)
            sample = CameraSample(
                frame=float(frame),
                position=position,
                orientation=orientation,
                focal_length=float(cmds.getAttr(camera_shape + ".focalLength")),
                # cmds.camera 的 query 模式读取视场角(度)
                horizontal_fov=float(
                    cmds.camera(camera_shape, query=True, horizontalFieldOfView=True)
                ),
                vertical_fov=float(
                    cmds.camera(camera_shape, query=True, verticalFieldOfView=True)
                ),
                f_stop=float(cmds.getAttr(camera_shape + ".fStop")),
                focus_distance=float(cmds.getAttr(camera_shape + ".focusDistance")),
                shutter_angle=float(cmds.getAttr(camera_shape + ".shutterAngle")),
                depth_of_field=bool(cmds.getAttr(camera_shape + ".depthOfField")),
                target_center=target_center,
                target_bounds_min=target_bounds[0] if target_bounds else None,
                target_bounds_max=target_bounds[1] if target_bounds else None,
            )
            samples.append(sample)
            if progress_callback:
                progress_callback(index, len(frames))
    finally:
        # 无论成功与否,都恢复用户原先的当前帧
        cmds.currentTime(original_time, edit=True, update=True)
    return samples, warnings, (camera_transform, camera_shape)


def analyze_maya_camera(
    camera: str,
    targets: Sequence[str] = (),
    start_frame: Optional[float] = None,
    end_frame: Optional[float] = None,
    target_weights: Optional[Dict[str, float]] = None,
    config: Optional[AnalysisConfig] = None,
    progress_callback: Optional[ProgressCallback] = None,
) -> MotionReport:
    """Maya 侧分析入口:采样 → 调用纯 Python 内核 → 返回报告(UI 的主调用)。"""

    cmds, _ = _maya_modules()
    samples, warnings, resolved = sample_camera(
        camera=camera,
        targets=targets,
        start_frame=start_frame,
        end_frame=end_frame,
        target_weights=target_weights,
        progress_callback=progress_callback,
    )
    camera_transform, _ = resolved
    target_transforms = tuple(_as_transform(target) for target in targets)
    # 世界朝上轴:Z-up 场景用 (0,0,1),否则默认 Y-up(影响环绕左右判定)
    world_up = (
        (0.0, 0.0, 1.0)
        if cmds.upAxis(query=True, axis=True) == "z"
        else (0.0, 1.0, 0.0)
    )
    # 默认配置:帧率取自 Maya 时间单位,朝上轴取自场景设置
    # UI 只提供算法选项；时间与上方向始终来自当前 Maya 场景。
    from dataclasses import replace
    active_config = replace(config or AnalysisConfig(), fps=maya_fps(), world_up=world_up)
    return analyze_samples(
        samples,
        # 名称只取短名(去掉 | 路径前缀),报告更易读
        camera_name=camera_transform.rsplit("|", 1)[-1],
        target_names=tuple(item.rsplit("|", 1)[-1] for item in target_transforms),
        config=active_config,
        extra_warnings=warnings,
    )
