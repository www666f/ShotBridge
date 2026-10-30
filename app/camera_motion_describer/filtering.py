"""单帧异常过滤：只修改采样副本，不修改 Maya 场景。"""
from __future__ import annotations

import statistics
from dataclasses import replace
from typing import List, Sequence, Tuple

from .math3d import (
    add, bounding_diagonal, distance, dot, midpoint, normalize,
    quaternion_angle_degrees, quaternion_slerp, subtract,
)
from .models import AnalysisConfig, CameraSample, MotionAnomaly


def _median_absolute_deviation(values: Sequence[float]) -> float:
    """中位数绝对偏差(MAD):比标准差更稳健的离散度,不受个别离群值影响。"""

    if not values:
        return 0.0
    center = statistics.median(values)
    return statistics.median(abs(value - center) for value in values)


def _robust_spike_threshold(
    step_values: Sequence[float],
    multiplier: float,
    absolute_floor: float,
) -> float:
    """由数据自身的统计量推导毛刺阈值:中位数 + 倍数 × 噪声。

    噪声取 MAD、中位数的 5% 与给定绝对下限三者的最大值,
    避免数据过于干净时阈值过小、把正常运动误判为毛刺。
    """

    if not step_values:
        return absolute_floor
    center = statistics.median(step_values)
    deviation = _median_absolute_deviation(step_values)
    noise = max(deviation, center * 0.05, absolute_floor)
    return center + multiplier * noise


def clean_camera_spikes(
    samples: Sequence[CameraSample], config: AnalysisConfig
) -> Tuple[List[CameraSample], List[MotionAnomaly]]:
    """清洗“单帧跳变、下一帧基本恢复”的毛刺。

    做法:分别对 位置 / 旋转 / 焦距 三种步长序列计算稳健阈值;
    若某帧在两侧都超阈值、前后两帧几乎回到一起(去而复返),
    就把该帧替换为前后帧的插值(位置取中点、旋转取 slerp 中点、焦距取均值),
    并记录一条 MotionAnomaly 供人工复检。
    cleaned 是副本,原始 samples 不会被修改。
    """

    cleaned = list(samples)
    anomalies: List[MotionAnomaly] = []
    if len(samples) < 3:
        return cleaned, anomalies

    # 下方三组统计:相邻帧之间 位置距离 / 旋转角 / 焦距差 的序列(用于推导毛刺阈值)
    path_scale = max(bounding_diagonal(sample.position for sample in samples), 1.0)
    # 位置步长与阈值:绝对下限与相对下限(路径尺度 × 系数)取大者
    position_steps = [
        distance(samples[index - 1].position, samples[index].position)
        for index in range(1, len(samples))
    ]
    position_threshold = _robust_spike_threshold(
        position_steps,
        config.spike_multiplier,
        max(config.position_epsilon, path_scale * config.relative_position_epsilon),
    )

    # 旋转步长(度)与阈值
    rotation_steps = [
        quaternion_angle_degrees(
            samples[index - 1].orientation, samples[index].orientation
        )
        for index in range(1, len(samples))
    ]
    rotation_threshold = _robust_spike_threshold(
        rotation_steps,
        config.spike_multiplier,
        config.rotation_epsilon_degrees,
    )

    # 焦距步长(mm)与阈值
    focal_steps = [
        abs(samples[index].focal_length - samples[index - 1].focal_length)
        for index in range(1, len(samples))
    ]
    focal_threshold = _robust_spike_threshold(
        focal_steps,
        config.spike_multiplier,
        config.focal_epsilon_mm,
    )

    for index in range(1, len(samples) - 1):
        previous = cleaned[index - 1]
        current = samples[index]
        following = samples[index + 1]
        replacement = current

        # —— 位置毛刺:两侧位移都超阈值、前后两帧几乎重合、且两段方向相反
        # (说明是“弹出去又弹回来”的单帧错误,而不是真实运动)
        left_distance = distance(previous.position, current.position)
        right_distance = distance(current.position, following.position)
        return_distance = distance(previous.position, following.position)
        left_vector = subtract(current.position, previous.position)
        right_vector = subtract(following.position, current.position)
        # 左右方向相反(单位向量点积 < -0.5 视为基本反向),排除真实折返
        opposite_direction = dot(normalize(left_vector), normalize(right_vector)) < -0.50
        if (
            left_distance > position_threshold
            and right_distance > position_threshold
            and return_distance
            < min(left_distance, right_distance) * config.spike_return_ratio
            and opposite_direction
        ):
            expected = midpoint(previous.position, following.position)
            magnitude = distance(current.position, expected)
            replacement = replace(replacement, position=expected)
            anomalies.append(
                MotionAnomaly(
                    frame=current.frame,
                    kind="position_spike",
                    magnitude=magnitude,
                    message=(
                        f"第 {current.frame:g} 帧摄像机位置出现单帧跳变，"
                        "下一帧基本恢复；分析时已忽略，请检查该帧关键帧。"
                    ),
                )
            )

        # —— 旋转毛刺:两侧角变化都超阈值,且几乎转回原来的朝向
        left_angle = quaternion_angle_degrees(
            previous.orientation, current.orientation
        )
        right_angle = quaternion_angle_degrees(
            current.orientation, following.orientation
        )
        return_angle = quaternion_angle_degrees(
            previous.orientation, following.orientation
        )
        if (
            left_angle > rotation_threshold
            and right_angle > rotation_threshold
            and return_angle < min(left_angle, right_angle) * config.spike_return_ratio
        ):
            expected_orientation = quaternion_slerp(
                previous.orientation, following.orientation, 0.5
            )
            magnitude = quaternion_angle_degrees(
                current.orientation, expected_orientation
            )
            replacement = replace(replacement, orientation=expected_orientation)
            anomalies.append(
                MotionAnomaly(
                    frame=current.frame,
                    kind="rotation_spike",
                    magnitude=magnitude,
                    message=(
                        f"第 {current.frame:g} 帧摄像机旋转出现单帧跳变，"
                        "下一帧基本恢复；分析时已忽略，请检查该帧关键帧。"
                    ),
                )
            )

        # —— 焦距毛刺:焦距在前后两侧都超阈值变化,一帧后又恢复
        left_focal = abs(current.focal_length - previous.focal_length)
        right_focal = abs(following.focal_length - current.focal_length)
        return_focal = abs(following.focal_length - previous.focal_length)
        if (
            left_focal > focal_threshold
            and right_focal > focal_threshold
            and return_focal
            < min(left_focal, right_focal) * config.spike_return_ratio
        ):
            expected_focal = (previous.focal_length + following.focal_length) * 0.5
            magnitude = abs(current.focal_length - expected_focal)
            replacement = replace(replacement, focal_length=expected_focal)
            anomalies.append(
                MotionAnomaly(
                    frame=current.frame,
                    kind="focal_spike",
                    magnitude=magnitude,
                    message=(
                        f"第 {current.frame:g} 帧焦距出现单帧跳变，"
                        "下一帧基本恢复；分析时已忽略，请检查该帧关键帧。"
                    ),
                )
            )

        cleaned[index] = replacement

    if not config.filter_spikes:
        return list(samples), [replace(
            item, message=item.message.replace("分析时已忽略", "按当前设置保留参与分析")
        ) for item in anomalies]
    return cleaned, anomalies


def clean_target_spikes(samples, config):
    """复用位置毛刺检测来处理主体整体中心；不修改 Maya 网格或动画。"""
    if any(sample.target_center is None for sample in samples):
        return list(samples), []
    proxy = [replace(
        sample, position=sample.target_center, orientation=(0.0, 0.0, 0.0, 1.0),
        focal_length=35.0, target_center=None,
    ) for sample in samples]
    filtered, anomalies = clean_camera_spikes(proxy, config)
    result = []
    for sample, item in zip(samples, filtered):
        offset = subtract(item.position, sample.target_center)
        result.append(replace(
            sample,
            target_center=item.position,
            target_bounds_min=(
                add(sample.target_bounds_min, offset)
                if sample.target_bounds_min is not None else None
            ),
            target_bounds_max=(
                add(sample.target_bounds_max, offset)
                if sample.target_bounds_max is not None else None
            ),
        ))
    return result, [replace(
        item, kind="target_position_spike",
        message=item.message.replace("摄像机位置", "主体整体中心位置"),
    ) for item in anomalies]


