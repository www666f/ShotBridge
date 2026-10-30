"""逐帧序列的时间事件。保留折返和停顿，不用首尾平均替代整条路径。"""

import math

from .math3d import dot, cross, subtract, scale, normalize, distance, world_vector_to_camera_local


def scalar_events(frames, values, epsilon):
    """按相邻采样的增/减/停分段，转折帧由相邻两段共享。"""
    events = []
    for index in range(1, len(values)):
        delta = values[index] - values[index - 1]
        direction = 0 if abs(delta) <= epsilon else (1 if delta > 0 else -1)
        if events and events[-1]["direction"] == direction:
            events[-1]["end_frame"] = frames[index]
            events[-1]["end_value"] = values[index]
        else:
            events.append({
                "start_frame": frames[index - 1], "end_frame": frames[index],
                "start_value": values[index - 1], "end_value": values[index],
                "direction": direction,
            })
    return events


def screen_events(frames, screens):
    events = []
    for frame, screen in zip(frames, screens):
        label = screen["label"]
        if events and events[-1]["label"] == label:
            events[-1]["end_frame"] = frame
        else:
            events.append({"start_frame": frame, "end_frame": frame, "label": label})
    return events


def measure_trajectory(samples, screens, config):
    """主体中心的路径、相对距离/高度、水平绕行及构图的完整时间范围。"""
    frames = [sample.frame for sample in samples]
    targets = [sample.target_center for sample in samples]
    vectors = [subtract(sample.position, target) for sample, target in zip(samples, targets)]
    radii = [distance(sample.position, target) for sample, target in zip(samples, targets)]
    up = normalize(config.world_up)
    heights = [dot(vector, up) for vector in vectors]
    horizontal = [subtract(vector, scale(up, height)) for vector, height in zip(vectors, heights)]

    # 每步带符号角度累加：360° 不再被首尾重合抵消；纯升降不成为左右环绕。
    azimuths = [0.0]
    for left, right in zip(horizontal, horizontal[1:]):
        if dot(left, left) < 1e-12 or dot(right, right) < 1e-12:
            azimuths.append(azimuths[-1])
            continue
        angle = math.degrees(math.atan2(dot(cross(left, right), up), dot(left, right)))
        azimuths.append(azimuths[-1] + angle)

    epsilon = max(config.position_epsilon, max(radii) * config.relative_position_epsilon)
    subject_path = [0.0]
    for left, right in zip(targets, targets[1:]):
        subject_path.append(subject_path[-1] + distance(left, right))
    # 固定使用段首相机坐标作方向参照，避免相机旋转伪装成主体自身移动。
    subject_positions = [world_vector_to_camera_local(
        samples[0].orientation, subtract(target, targets[0])
    ) for target in targets]
    return {
        "distance_events": scalar_events(frames, radii, epsilon),
        "height_events": scalar_events(frames, heights, epsilon),
        "orbit_events": scalar_events(frames, azimuths, config.rotation_epsilon_degrees),
        "screen_events": screen_events(frames, screens),
        "subject_motion_events": scalar_events(frames, subject_path, epsilon),
        "subject_axis_events": {
            axis: scalar_events(frames, [position[index] for position in subject_positions], epsilon)
            for index, axis in enumerate(("horizontal", "vertical", "depth"))
        },
        "subject_path_length": subject_path[-1],
        "subject_center_bounds": {
            "min": [min(target[axis] for target in targets) for axis in range(3)],
            "max": [max(target[axis] for target in targets) for axis in range(3)],
        },
        "distance_min": min(radii), "distance_max": max(radii),
        "height_min": min(heights), "height_max": max(heights),
        "signed_orbit_degrees": azimuths[-1],
        "horizontal_orbit_path_degrees": sum(abs(b-a) for a, b in zip(azimuths, azimuths[1:])),
    }
