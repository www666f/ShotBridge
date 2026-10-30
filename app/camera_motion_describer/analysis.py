"""摄像机轨迹分析内核(完全不依赖 Maya,输入输出都是纯 Python 数据)。

整体流水线(analyze_samples 内部按此顺序执行):
1. 清洗:识别并忽略“下一帧恢复”的单帧跳变(位置 / 旋转 / 焦距);
2. 分解:把相邻帧之间的世界位移与旋转转换到相机局部轴
   (dolly 推拉 / truck 横移 / pedestal 升降 / tilt 俯仰 / pan 横摇 / roll 滚转 / zoom 变焦);
3. 打标:按阈值判定每一步的运动标签,每步最多保留两个主要空间运动;
4. 稳定:桥接抖动、合并过短的标签段;
5. 分段:把连续相同的标签合成时间段,计算速度曲线、与主体的关系等度量;
6. 生成:汇总为 MotionReport(含中文提示词)。
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, replace
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .math3d import (
    angle_between_degrees,
    bounding_diagonal,
    cross,
    distance,
    dot,
    normalize,
    quaternion_rotate,
    relative_rotation_vector_degrees,
    subtract,
    world_vector_to_camera_local,
)
from .models import (
    AnalysisConfig,
    CameraSample,
    MotionAnomaly,
    MotionReport,
    MotionSegment,
)
from .prompt import (
    PROMPT_MERGE_THRESHOLD_DEFAULT,
    PROMPT_MIN_PHASE_SECONDS,
    build_prompt,
    group_phases,
)
from .trajectory import measure_trajectory
from .filtering import clean_camera_spikes, clean_target_spikes


# 运动标签的中文展示名(报告/详情用的短词;提示词里用的是 prompt.py 中更口语化的短语)
MOTION_TEXT = {
    "static": "固定镜头",
    "dolly_in": "向前推进",
    "dolly_out": "向后拉远",
    "truck_left": "向左横移",
    "truck_right": "向右横移",
    "pedestal_up": "向上升起",
    "pedestal_down": "向下降低",
    "pan_left": "向左横摇",
    "pan_right": "向右横摇",
    "tilt_up": "向上仰摇",
    "tilt_down": "向下俯摇",
    "roll_left": "向左滚转",
    "roll_right": "向右滚转",
    "zoom_in": "增加焦距（变焦拉近）",
    "zoom_out": "减小焦距（变焦拉远）",
}

# 运动标签的固定排序权重(数值即优先级),不同运动同时出现时按此顺序展示:
# 推拉(10) → 横移(20) → 升降(30) → 横摇(40) → 俯仰(50) → 滚转(60) → 变焦(70)
MOTION_ORDER = {
    "static": 0,
    "dolly_in": 10,
    "dolly_out": 11,
    "truck_left": 20,
    "truck_right": 21,
    "pedestal_up": 30,
    "pedestal_down": 31,
    "pan_left": 40,
    "pan_right": 41,
    "tilt_up": 50,
    "tilt_down": 51,
    "roll_left": 60,
    "roll_right": 61,
    "zoom_in": 70,
    "zoom_out": 71,
}


@dataclass
class _Step:
    """分析内部使用:相邻两帧之间的“一步”运动及其派生量。"""

    start_frame: float                    # 起始帧
    end_frame: float                      # 结束帧
    values: Dict[str, float]              # 七轴原始数值:dolly/truck/pedestal/pan/tilt/roll/zoom
    start_sample: CameraSample            # 起始采样(供插值等使用)
    end_sample: CameraSample              # 结束采样
    motions: Tuple[str, ...] = ()         # 运动标签(由 _label_steps 填充)
    active_spatial_count: int = 0         # 活跃轴数量(超过上限即视为复杂复合运动)
    target_aim_angle: Optional[float] = None  # 相机前向与目标方向的夹角(度)


def _extract_steps(samples: Sequence[CameraSample]) -> List[_Step]:
    """把相邻帧之间的世界位移 / 旋转分解到相机局部轴,生成逐步长数据。"""

    steps: List[_Step] = []
    for start, end in zip(samples, samples[1:]):
        # 世界位移转到相机局部坐标:局部 X = 右(truck)、Y = 上(pedestal)、-Z = 前(dolly)
        world_delta = subtract(end.position, start.position)
        local_delta = world_vector_to_camera_local(start.orientation, world_delta)
        rotation_delta = relative_rotation_vector_degrees(
            start.orientation, end.orientation
        )
        values = {
            "truck": local_delta[0],    # 局部 X:横移量
            "pedestal": local_delta[1],  # 局部 Y:升降量
            "dolly": -local_delta[2],   # 相机朝 -Z,取负使“前进”为正
            "tilt": rotation_delta[0],  # 局部 X 轴旋转:俯仰
            # Maya 局部 Y 正方向旋转,会让“朝 -Z 看”的相机转向其左侧
            "pan": rotation_delta[1],
            "roll": rotation_delta[2],
            "zoom": end.focal_length - start.focal_length,  # 焦距差(正 = 拉近)
        }
        # 若选择了被摄主体:计算相机前向与目标方向的夹角(用于判断“是否持续对准”)
        aim_angle = None
        if start.target_center is not None:
            camera_forward = quaternion_rotate(start.orientation, (0.0, 0.0, -1.0))
            target_direction = subtract(start.target_center, start.position)
            aim_angle = angle_between_degrees(camera_forward, target_direction)
            # 选填:本步内相机到主体的距离变化(用于主体关系分析)
            if end.target_center is not None:
                values["target_distance_change"] = distance(
                    end.position, end.target_center
                ) - distance(start.position, start.target_center)
        steps.append(
            _Step(
                start_frame=start.frame,
                end_frame=end.frame,
                values=values,
                start_sample=start,
                end_sample=end,
                target_aim_angle=aim_angle,
            )
        )
    return steps


def _signed_motion(axis: str, value: float) -> str:
    """把某轴的带符号数值映射成具体运动标签(正 / 负对应相反方向)。"""

    # 每个轴的 (正方向标签, 负方向标签)
    mapping = {
        "dolly": ("dolly_in", "dolly_out"),
        "truck": ("truck_right", "truck_left"),
        "pedestal": ("pedestal_up", "pedestal_down"),
        "pan": ("pan_left", "pan_right"),
        "tilt": ("tilt_up", "tilt_down"),
        "roll": ("roll_left", "roll_right"),
        "zoom": ("zoom_in", "zoom_out"),
    }
    positive, negative = mapping[axis]
    return positive if value >= 0.0 else negative


def _label_steps(
    steps: Sequence[_Step],
    position_threshold: float,
    config: AnalysisConfig,
) -> None:
    """按阈值给每一步标注运动标签(就地写入 step.motions)。

    规则:
    - 空间运动(dolly/truck/pedestal、pan/tilt/roll)中选出主要的一到两个:
      平移取最大者;有旋转时平移与旋转各取最大者;一侧不存在时,
      若另一侧的次大值达到最大值的 35% 也保留;
    - 变焦(zoom)只要超过焦距阈值就单独附加,不占空间运动名额;
    - 无任何运动则标为 ("static",),最终标签按 MOTION_ORDER 固定排序。
    """

    for step in steps:
        # 超过阈值的平移轴与旋转轴(后续按绝对值排序,取主要运动)
        translations = [
            (axis, step.values[axis])
            for axis in ("dolly", "truck", "pedestal")
            if abs(step.values[axis]) >= position_threshold
        ]
        rotations = [
            (axis, step.values[axis])
            for axis in ("pan", "tilt", "roll")
            if abs(step.values[axis]) >= config.rotation_epsilon_degrees
        ]
        zoom = step.values["zoom"]
        translations.sort(key=lambda item: abs(item[1]), reverse=True)
        rotations.sort(key=lambda item: abs(item[1]), reverse=True)

        # 挑选主要空间运动:平移与旋转各取最大;若一侧不存在,
        # 则另一侧的次大值达到最大值 35% 以上时也保留(体现复合运动)
        spatial: List[Tuple[str, float]] = []
        if translations:
            spatial.append(translations[0])
        if rotations:
            spatial.append(rotations[0])
        if not rotations and len(translations) > 1:
            if abs(translations[1][1]) >= abs(translations[0][1]) * 0.35:
                spatial.append(translations[1])
        if not translations and len(rotations) > 1:
            if abs(rotations[1][1]) >= abs(rotations[0][1]) * 0.35:
                spatial.append(rotations[1])

        # 记录本步活跃轴总数(用于后续判断“复杂复合运动”)
        step.active_spatial_count = len(translations) + len(rotations)
        # 空间运动最多保留 maximum_spatial_motions 个
        spatial = spatial[: config.maximum_spatial_motions]
        motions = [_signed_motion(axis, value) for axis, value in spatial]
        if abs(zoom) >= config.focal_epsilon_mm:
            motions.append(_signed_motion("zoom", zoom))
        step.motions = (
            tuple(sorted(motions, key=MOTION_ORDER.__getitem__))
            if motions
            else ("static",)
        )


def _runs(labels: Sequence[Tuple[str, ...]]) -> List[Tuple[int, int, Tuple[str, ...]]]:
    """把标签序列切成连续相同标签的区间,返回 (起始下标, 结束下标, 标签)。"""

    if not labels:
        return []
    result: List[Tuple[int, int, Tuple[str, ...]]] = []
    start = 0
    for index in range(1, len(labels)):
        if labels[index] != labels[start]:
            result.append((start, index - 1, labels[start]))
            start = index
    result.append((start, len(labels) - 1, labels[start]))
    return result


def _stabilize_labels(
    labels: Sequence[Tuple[str, ...]], config: AnalysisConfig
) -> List[Tuple[str, ...]]:
    """稳定标签序列,减少抖动造成的碎段(最多循环 4 轮,每轮只改一处)。

    两条规则:
    - 桥接:短段(≤ bridge_gap_frames)前后标签相同 → 直接用该标签覆盖;
    - 并入:段长小于 minimum_segment_frames → 并入标签重合度最高的邻居
      (重合度按 Jaccard 计算,相同时取更长的邻居)。
    """

    labels = list(labels)
    for _ in range(4):
        changed = False
        runs = _runs(labels)
        for run_index, (start, end, label) in enumerate(runs):
            run_length = end - start + 1
            previous = runs[run_index - 1] if run_index > 0 else None
            following = runs[run_index + 1] if run_index + 1 < len(runs) else None
            # 规则一:前后标签相同的短抖动段,直接桥接
            if (
                previous
                and following
                and previous[2] == following[2]
                and run_length <= config.bridge_gap_frames
            ):
                labels[start : end + 1] = [previous[2]] * run_length
                changed = True
                break
            # 规则二:过短的段并入与它最相似的邻居(先比重合度,再比段长)
            if run_length < config.minimum_segment_frames:
                candidates = [item for item in (previous, following) if item]
                if candidates:
                    source = set(label)

                    def candidate_score(candidate):
                        target = set(candidate[2])
                        overlap = len(source & target) / max(1, len(source | target))
                        candidate_length = candidate[1] - candidate[0] + 1
                        return overlap, candidate_length

                    replacement = max(candidates, key=candidate_score)[2]
                    labels[start : end + 1] = [replacement] * run_length
                    changed = True
                    break
        if not changed:
            break
    return labels


def _speed_profile(values: Sequence[float]) -> str:
    """由逐步速度值判定速度曲线类型。

    先看离散程度:变异系数(标准差 / 均值)< 0.15 视为匀速;
    否则把序列分三段,比较首段 / 中段 / 尾段的平均速度得出曲线形状。
    """

    values = [abs(value) for value in values]
    # 帧数太少或几乎无位移:直接视为匀速
    if len(values) < 4 or max(values, default=0.0) <= 1.0e-12:
        return "steady"
    mean = statistics.fmean(values)
    if mean <= 1.0e-12:
        return "steady"
    # 变异系数:整体波动小于 15% 视为匀速
    deviation = statistics.pstdev(values) / mean
    if deviation < 0.15:
        return "steady"

    # 分三段统计平均速度:首段 / 中段 / 尾段
    third = max(1, len(values) // 3)
    first = statistics.fmean(values[:third])
    middle_values = values[third:-third] or values
    middle = statistics.fmean(middle_values)
    last = statistics.fmean(values[-third:])
    tolerance = mean * 0.12  # 12% 容差,避免把稳态误判成变化
    # 两端慢、中间快:缓入缓出
    if first + tolerance < middle and last + tolerance < middle:
        return "ease_in_out"
    # 尾段明显快于首段:加速
    if first + tolerance < last:
        return "accelerating"
    # 首段明显快于尾段:减速
    if first > last + tolerance:
        return "decelerating"
    # 其余情况:不规则
    return "variable"


def _primary_value(step: _Step, motion: str) -> float:
    """由运动标签反查该步的对应轴原始数值(计算主运动速度曲线用)。"""

    reverse_lookup = {
        "dolly_in": "dolly",
        "dolly_out": "dolly",
        "truck_left": "truck",
        "truck_right": "truck",
        "pedestal_up": "pedestal",
        "pedestal_down": "pedestal",
        "pan_left": "pan",
        "pan_right": "pan",
        "tilt_up": "tilt",
        "tilt_down": "tilt",
        "roll_left": "roll",
        "roll_right": "roll",
        "zoom_in": "zoom",
        "zoom_out": "zoom",
    }
    return step.values.get(reverse_lookup.get(motion, ""), 0.0)


def _screen_position(sample: CameraSample, threshold: float) -> Dict[str, object]:
    """把主体中心投影到归一化画面坐标,并给出中文构图方位。

    归一化坐标:横向 ±1 对应画面左右边缘,纵向 ±1 对应上下边缘;
    绝对值不超过 threshold 视为“中央”;
    投影不做镜头畸变修正,只按视场角做透视除法。
    """

    if sample.target_center is None:
        return {}
    local = world_vector_to_camera_local(
        sample.orientation, subtract(sample.target_center, sample.position)
    )
    depth = -local[2]        # 局部 -Z 为前方,取负得到正景深
    if depth <= 1.0e-8:
        return {"label": "摄像机后方", "visible": False}

    # 缺省视场角(仅在读不到相机 FOV 时兜底)
    horizontal_fov = sample.horizontal_fov or 60.0
    vertical_fov = sample.vertical_fov or 40.0
    half_width = depth * math.tan(math.radians(horizontal_fov) * 0.5)
    half_height = depth * math.tan(math.radians(vertical_fov) * 0.5)
    screen_x = local[0] / max(half_width, 1.0e-8)
    screen_y = local[1] / max(half_height, 1.0e-8)

    # 由归一化坐标翻译成中文方位词
    horizontal = "中央"
    if screen_x < -threshold:
        horizontal = "左侧"
    elif screen_x > threshold:
        horizontal = "右侧"
    vertical = ""
    if screen_y > threshold:
        vertical = "偏上"
    elif screen_y < -threshold:
        vertical = "偏下"
    label = f"画面{horizontal}{vertical}"
    if abs(screen_x) > 1.0 or abs(screen_y) > 1.0:
        label += "之外"
    return {
        "label": label,
        "visible": abs(screen_x) <= 1.0 and abs(screen_y) <= 1.0,
        "normalized_x": screen_x,
        "normalized_y": screen_y,
    }


def _subject_framing(sample: CameraSample) -> Dict[str, object]:
    """把整体包围盒八个角投影到画面，近似判断完整入画、部分入画或出画。"""

    if sample.target_bounds_min is None or sample.target_bounds_max is None:
        return {}
    minimum, maximum = sample.target_bounds_min, sample.target_bounds_max
    projected = []
    behind_count = 0
    for x in (minimum[0], maximum[0]):
        for y in (minimum[1], maximum[1]):
            for z in (minimum[2], maximum[2]):
                point_sample = replace(sample, target_center=(x, y, z))
                point = _screen_position(point_sample, 0.0)
                if "normalized_x" not in point:
                    behind_count += 1
                else:
                    projected.append((point["normalized_x"], point["normalized_y"]))
    if not projected:
        return {"state": "behind_camera", "label": "主体整体位于摄像机后方"}
    xs = [point[0] for point in projected]
    ys = [point[1] for point in projected]
    screen_bounds = {"min": [min(xs), min(ys)], "max": [max(xs), max(ys)]}
    overlaps = (
        max(xs) >= -1.0 and min(xs) <= 1.0
        and max(ys) >= -1.0 and min(ys) <= 1.0
    )
    fully_inside = (
        behind_count == 0 and min(xs) >= -1.0 and max(xs) <= 1.0
        and min(ys) >= -1.0 and max(ys) <= 1.0
    )
    if fully_inside:
        state, label = "fully_in_frame", "主体整体完整处于画面内"
    elif overlaps:
        state, label = "partially_in_frame", "主体整体部分超出画面"
    else:
        state, label = "out_of_frame", "主体整体处于画面外"
    return {"state": state, "label": label, "screen_bounds": screen_bounds}


def _framing_events(samples: Sequence[CameraSample]) -> List[Dict[str, object]]:
    """把连续相同的主体整体入画状态合并为时间事件。"""

    events: List[Dict[str, object]] = []
    for sample in samples:
        framing = _subject_framing(sample)
        if not framing:
            continue
        if events and events[-1]["state"] == framing["state"]:
            events[-1]["end_frame"] = sample.frame
        else:
            events.append({
                "start_frame": sample.frame,
                "end_frame": sample.frame,
                "state": framing["state"],
                "label": framing["label"],
            })
    return events


def _target_relation(
    segment_steps: Sequence[_Step],
    target_aimed: bool,
    config: AnalysisConfig,
) -> Dict[str, object]:
    """统计本段内相机与主体的关系:距离、观察角、环绕、相对高度与画面构图。

    关键量:
    - 观察角:相机→主体方向向量在段首与段尾的夹角(净变化);
    - 环绕判定:持续对准主体、净观察角达阈值,且切向路径明显大于径向路径
      (说明在“绕着”看而不是“靠近 / 远离”);方向由首尾向量叉积与世界上方向
      点积的符号决定(正 = 向右环绕);
    - 画面构图:主体中心投影到段首 / 段尾的归一化屏幕坐标。
    """

    # 本段完整采样序列:首帧 + 各步的结束帧
    samples = [segment_steps[0].start_sample]
    samples.extend(step.end_sample for step in segment_steps)
    if any(sample.target_center is None for sample in samples):
        return {}

    # 相机相对主体的位置向量(从主体指向相机)与距离(半径)
    relative_vectors = [
        subtract(sample.position, sample.target_center)  # type: ignore[arg-type]
        for sample in samples
    ]
    radii = [distance(sample.position, sample.target_center) for sample in samples]  # type: ignore[arg-type]
    # 距离变化率 = (段尾半径 - 段首半径) / 段首半径(分母设下限防除零)
    start_radius = radii[0]
    end_radius = radii[-1]
    radius_change = end_radius - start_radius
    radius_change_ratio = radius_change / max(start_radius, 1.0e-8)
    # 把每一步位移分解为:径向(朝向 / 远离主体)与切向(绕主体转)两部分的路径长度
    tangential_path = 0.0
    radial_path = 0.0
    for left, right in zip(relative_vectors, relative_vectors[1:]):
        movement = subtract(right, left)
        radial_step = dot(movement, normalize(left))
        radial_path += abs(radial_step)
        tangential_squared = max(0.0, dot(movement, movement) - radial_step**2)
        tangential_path += math.sqrt(tangential_squared)
    # 累计观察角:所有步长夹角之和(反映绕行总量,即使绕回原位也算)
    travelled_orbit_degrees = sum(
        angle_between_degrees(left, right)
        for left, right in zip(relative_vectors, relative_vectors[1:])
    )
    # 净观察角:只看段首与段尾的方向差;方向符号 = 叉积 · 世界上方向(>0 为右环绕)
    viewpoint_degrees = angle_between_degrees(
        relative_vectors[0], relative_vectors[-1]
    )
    signed_viewpoint = dot(
        cross(relative_vectors[0], relative_vectors[-1]), config.world_up
    )
    # 环绕 = 持续对准主体 + 净观察角达阈值 + 切向为主(不是单纯靠近 / 远离)
    orbit_direction = None
    if (
        target_aimed
        and viewpoint_degrees >= config.orbit_minimum_degrees
        and tangential_path > radial_path * 0.35
    ):
        orbit_direction = "right" if signed_viewpoint >= 0.0 else "left"

    # 段首 / 段尾主体在画面中的位置(构图)
    start_screen = _screen_position(samples[0], config.screen_center_threshold)
    end_screen = _screen_position(samples[-1], config.screen_center_threshold)
    relation: Dict[str, object] = {
        "screen_start": start_screen,
        "screen_end": end_screen,
        "distance_start": start_radius,
        "distance_end": end_radius,
        "distance_change_ratio": radius_change_ratio,
        "camera_height_start": relative_vectors[0][1],
        "camera_height_end": relative_vectors[-1][1],
        "camera_height_change_ratio": (
            relative_vectors[-1][1] - relative_vectors[0][1]
        )
        / max(start_radius, 1.0e-8),
        "viewpoint_direction": (
            "right" if signed_viewpoint >= 0.0 else "left"
        )
        if viewpoint_degrees >= config.orbit_minimum_degrees
        else None,
        "orbit_direction": orbit_direction,
        "viewpoint_angle_degrees": viewpoint_degrees,
        "travelled_orbit_angle_degrees": travelled_orbit_degrees,
        "tangential_path": tangential_path,
        "radial_path": radial_path,
    }
    screens = [_screen_position(sample, config.screen_center_threshold) for sample in samples]
    relation.update(measure_trajectory(samples, screens, config))
    framing_events = _framing_events(samples)
    if framing_events:
        relation["framing_start"] = _subject_framing(samples[0])
        relation["framing_end"] = _subject_framing(samples[-1])
        relation["framing_events"] = framing_events
    # 结构化摘要与逐帧事件使用相同的水平角和世界上方向，避免两套结论矛盾。
    signed_angle = relation["signed_orbit_degrees"]
    viewpoint_direction = None
    if abs(signed_angle) >= config.orbit_minimum_degrees:
        viewpoint_direction = "right" if signed_angle > 0 else "left"
    relation["viewpoint_direction"] = viewpoint_direction
    relation["orbit_direction"] = viewpoint_direction if target_aimed else None
    relation["viewpoint_angle_degrees"] = abs(signed_angle)
    relation["camera_height_start"] = dot(relative_vectors[0], normalize(config.world_up))
    relation["camera_height_end"] = dot(relative_vectors[-1], normalize(config.world_up))
    relation["camera_height_change_ratio"] = (
        relation["camera_height_end"] - relation["camera_height_start"]
    ) / max(start_radius, 1e-8)
    return relation


def _parameter_metrics(samples: Sequence[CameraSample]) -> Dict[str, object]:
    """统计镜头参数(焦距/光圈/对焦距离/快门角)的起止与极值,以及景深开关变化。"""

    metrics: Dict[str, object] = {}
    fields = (
        ("focal_length", "focal_length_mm"),
        ("f_stop", "f_stop"),
        ("focus_distance", "focus_distance"),
        ("shutter_angle", "shutter_angle_degrees"),
    )
    for attribute, key in fields:
        values = [getattr(sample, attribute) for sample in samples]
        numeric = [float(value) for value in values if value is not None]
        if numeric:
            metrics[f"{key}_start"] = numeric[0]
            metrics[f"{key}_end"] = numeric[-1]
            metrics[f"{key}_min"] = min(numeric)
            metrics[f"{key}_max"] = max(numeric)
    # 景深开关:记录起止状态与段内是否发生过切换
    metrics["depth_of_field_start"] = samples[0].depth_of_field
    metrics["depth_of_field_end"] = samples[-1].depth_of_field
    metrics["depth_of_field_changed"] = any(
        sample.depth_of_field != samples[0].depth_of_field for sample in samples[1:]
    )
    return metrics


def _build_segments(
    steps: Sequence[_Step], labels: Sequence[Tuple[str, ...]], config: AnalysisConfig
) -> List[MotionSegment]:
    """把稳定后的标签区间合成为 MotionSegment,并计算速度、置信度与全部度量。"""

    segments: List[MotionSegment] = []
    for start, end, label in _runs(labels):
        segment_steps = steps[start : end + 1]
        # 速度曲线只跟踪该段的“主运动”(标签中优先级最高的那个)
        primary = label[0]
        speed = _speed_profile(
            [_primary_value(step, primary) for step in segment_steps]
        )
        # “持续对准主体”:全部采样（含结束帧）均需在角度容差内。
        aimed_values = [
            step.target_aim_angle
            for step in segment_steps
            if step.target_aim_angle is not None
        ]
        last_sample = segment_steps[-1].end_sample
        if last_sample.target_center is not None:
            aimed_values.append(angle_between_degrees(
                quaternion_rotate(last_sample.orientation, (0.0, 0.0, -1.0)),
                subtract(last_sample.target_center, last_sample.position),
            ))
        target_aimed = bool(aimed_values) and max(aimed_values) <= (
            config.target_aim_tolerance_degrees
        )
        # 复杂运动比率:活跃轴多于 maximum_spatial_motions 的帧占比
        complex_ratio = sum(
            step.active_spatial_count > config.maximum_spatial_motions
            for step in segment_steps
        ) / len(segment_steps)
        # 基础置信度 0.95;复杂段扣 0.20;纯固定机位最高(0.99)
        confidence = 0.95
        if complex_ratio > 0.25:
            confidence -= 0.20
        if label == ("static",):
            confidence = 0.99
        metrics = {
            "median_target_aim_angle_degrees": (
                statistics.median(aimed_values) if aimed_values else -1.0
            ),
            "complex_frame_ratio": complex_ratio,
        }
        for axis in ("dolly", "truck", "pedestal", "pan", "tilt", "roll", "zoom"):
            metrics[f"total_{axis}"] = sum(step.values[axis] for step in segment_steps)
        # 结构化详情不受“提示词最多显示两个主要运动”的限制：七个通道全部保留，
        # 供后续 Maya 工具读取真实累计量、峰值和各自速度趋势。
        metrics["motion_channels"] = {
            axis: {
                "total": sum(step.values[axis] for step in segment_steps),
                "absolute_total": sum(abs(step.values[axis]) for step in segment_steps),
                "peak_step": max(abs(step.values[axis]) for step in segment_steps),
                "speed_profile": _speed_profile(
                    [step.values[axis] for step in segment_steps]
                ),
            }
            for axis in ("dolly", "truck", "pedestal", "pan", "tilt", "roll", "zoom")
        }
        # 本段完整采样序列 → 参数度量 + 与主体关系
        segment_samples = [segment_steps[0].start_sample]
        segment_samples.extend(step.end_sample for step in segment_steps)
        metrics.update(_parameter_metrics(segment_samples))
        target_relation = _target_relation(segment_steps, target_aimed, config)
        # 运动发生于两帧之间；共享边界帧，时间轴无空档且与事件范围一致。
        display_start_frame = segment_steps[0].start_frame
        segments.append(
            MotionSegment(
                start_frame=display_start_frame,
                end_frame=segment_steps[-1].end_frame,
                motions=label,
                speed_profile=speed,
                confidence=max(0.0, confidence),
                target_aimed=target_aimed,
                complex_motion=complex_ratio > 0.25,
                metrics=metrics,
                target_relation=target_relation,
            )
        )
    return segments


def analyze_samples(
    samples: Sequence[CameraSample],
    camera_name: str = "camera",
    target_names: Sequence[str] = (),
    config: Optional[AnalysisConfig] = None,
    extra_warnings: Sequence[str] = (),
) -> MotionReport:
    """分析已求值的世界空间相机采样序列,返回完整 MotionReport。

    这是分析内核的入口:输入纯数据(不依赖 Maya),
    内部依次执行 清洗 → 分解 → 打标 → 稳定 → 分段 → 生成提示词。
    """

    config = config or AnalysisConfig()
    if not math.isfinite(config.spike_multiplier) or config.spike_multiplier <= 0:
        raise ValueError("异常判定容差倍数必须是大于零的有限数值。")
    if not math.isfinite(config.fps) or config.fps <= 0:
        raise ValueError("帧率必须是大于零的有限数值。")
    if len(samples) < 2:
        raise ValueError("至少需要两个摄像机采样帧。")
    samples = sorted(samples, key=lambda sample: sample.frame)
    for previous, current in zip(samples, samples[1:]):
        if current.frame <= previous.frame:
            raise ValueError("采样帧必须严格递增。")

    # 1) 单帧毛刺清洗(原始 samples 保持不变,anomalies 用于提示复检)
    cleaned, anomalies = clean_camera_spikes(samples, config)
    cleaned, target_anomalies = clean_target_spikes(cleaned, config)
    anomalies.extend(target_anomalies)
    # 位置阈值 = 绝对下限与相对下限(整个相机路径的尺度 × 系数)取大者
    path_scale = max(bounding_diagonal(sample.position for sample in cleaned), 1.0)
    position_threshold = max(
        config.position_epsilon,
        path_scale * config.relative_position_epsilon,
    )
    # 2) 逐步长分解 → 3) 打标 → 4) 稳定 → 5) 分段
    steps = _extract_steps(cleaned)
    _label_steps(steps, position_threshold, config)
    # 突变是否过滤由用户设置决定；不要再把已保留的短动作无条件并入邻段。
    labels = [step.motions for step in steps]
    segments = _build_segments(steps, labels, config)

    # 警告来源:外部传入(如目标轴心偏移)+ 毛刺异常 + 复杂运动提示
    warnings = list(extra_warnings)
    warnings.extend(item.message for item in anomalies)
    if any(segment.complex_motion for segment in segments):
        warnings.append(
            "部分时间段同时包含多种明显运动；提示词仅保留两个主要空间运动，请人工复检。"
        )

    # 全镜头的参数度量(用于末尾“全镜头摄影参数”汇总)
    parameter_metadata = _parameter_metrics(cleaned)
    metadata = {
        "schema_version": "1.0.0",
        "coordinate_system": {
            "translation_space": "camera_local",
            "camera_forward": "-Z",
            "camera_right": "+X",
            "camera_up": "+Y",
            "world_up": list(config.world_up),
        },
        "channel_units": {
            "dolly": "maya_scene_unit",
            "truck": "maya_scene_unit",
            "pedestal": "maya_scene_unit",
            "pan": "degree",
            "tilt": "degree",
            "roll": "degree",
            "zoom": "millimeter",
        },
        "motion_steps": [
            {
                "frame_range": [step.start_frame, step.end_frame],
                "channels": {
                    axis: step.values[axis]
                    for axis in (
                        "dolly", "truck", "pedestal", "pan", "tilt", "roll", "zoom"
                    )
                },
            }
            for step in steps
        ],
        "sample_count": len(cleaned),
        "filter_spikes": config.filter_spikes,
        "spike_multiplier": config.spike_multiplier,
        "anomaly_count": len(anomalies),
        "anomaly_action": "filtered" if config.filter_spikes else "retained",
        "position_threshold": position_threshold,
        "rotation_threshold_degrees": config.rotation_epsilon_degrees,
        "focal_threshold_mm": config.focal_epsilon_mm,
        "horizontal_fov_start_degrees": cleaned[0].horizontal_fov,
        "horizontal_fov_end_degrees": cleaned[-1].horizontal_fov,
        # 提示词阶段数量:细分段按运动变化率合并后的段落数(只影响提示词排版)
        "prompt_phase_count": len(group_phases(segments, config.fps)),
        "prompt_merge_threshold": PROMPT_MERGE_THRESHOLD_DEFAULT,
        "prompt_min_phase_seconds": PROMPT_MIN_PHASE_SECONDS,
    }
    metadata.update(parameter_metadata)
    # 6) 把结构化结果渲染成中文提示词
    prompt = build_prompt(
        segments=segments,
        metadata=metadata,
        fps=config.fps,
        has_targets=bool(target_names),
        anomalies=anomalies,
    )
    return MotionReport(
        camera_name=camera_name,
        start_frame=cleaned[0].frame,
        end_frame=cleaned[-1].frame,
        fps=config.fps,
        target_names=tuple(target_names),
        segments=tuple(segments),
        anomalies=tuple(anomalies),
        warnings=tuple(dict.fromkeys(warnings)),
        prompt=prompt,
        metadata=metadata,
    )
