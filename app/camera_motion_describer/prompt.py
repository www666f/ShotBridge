"""把结构化的运动分析结果渲染成精确的中文提示词(供 AI 视频平台使用)。

本模块只读取 MotionSegment / metadata / MotionAnomaly,不修改分析结果;
渲染分两层:
- 阶段:相邻细分段按运动变化率合并(见 group_phases),每个阶段一个段落,
  多细分段时用“先/随后/最后”串联整体运动趋势;
- 段内排版:事件按时间排序,相同时间范围只写一次,贯穿整段的不再重复范围。
异常帧与复杂复合运动以“【复检提示】”红字行显示(仅供界面查看,
复制或提交前由 strip_display_warnings 整行剔除)。
"""

from __future__ import annotations

from typing import Dict, List, Mapping, Sequence, Tuple

from .models import MotionAnomaly, MotionSegment


# 运动标签 → 提示词短语(比 analysis.py 里的展示名更口语化,直接写进句子)
MOTION_PHRASES = {
    "static": "保持固定机位",
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
    "zoom_in": "逐渐增加焦距",
    "zoom_out": "逐渐减小焦距",
}

# 速度曲线标签 → 描述短语
SPEED_PHRASES = {
    "steady": "速度基本均匀",
    "accelerating": "运动由慢到快",
    "decelerating": "运动由快到慢",
    "ease_in_out": "缓慢起步、中段加速并在结尾减速",
    "variable": "速度变化不规则",
}

# 仅用于界面展示的复检提示:界面把它显示为红色,复制提示词时会被剔除
COMPLEX_MOTION_WARNING = "该段包含未完全展开的复杂复合运动"
DISPLAY_WARNING_PREFIX = "【复检提示】"

# 分段合并强度的默认值(0~1):界面滑杆取 0~100,除以 100 得到本阈值;
# 相邻细分段的运动变化率达到它就在边界处拆开,阈值越高段落越少
PROMPT_MERGE_THRESHOLD_DEFAULT = 0.6

# 提示词阶段的最短时长(秒):短于它的阶段并入更相似的一侧,避免提示词细碎
PROMPT_MIN_PHASE_SECONDS = 0.2

# 帧号比较容差(小数帧场景下避免浮点误差)
_FRAME_EPSILON = 1.0e-9

# 事件短语:(起始帧, 结束帧, 文本)
_Event = Tuple[float, float, str]


def group_phases(
    segments: Sequence[MotionSegment],
    fps: float,
    merge_threshold: float = PROMPT_MERGE_THRESHOLD_DEFAULT,
) -> List[List[MotionSegment]]:
    """把细分段合并成“提示词阶段”,只影响提示词排版。

    两步处理:
    1. 按运动变化拆分:相邻细分段的运动标签变化率达到阈值就在边界处拆开。
       完全换动作(变化率 1.0,如静止变推进)在任何阈值下都会拆开;
       调低阈值则更小的变化也会拆开,段落随之变多。
    2. 碎段保护:短于最短时长的阶段并入标签更相似的一侧,避免细碎。
    结构化详情仍使用原始细分段,不受本函数影响。
    """

    phases: List[List[MotionSegment]] = []
    for segment in segments:
        if phases and _motion_change_ratio(phases[-1][-1], segment) < merge_threshold:
            phases[-1].append(segment)
        else:
            phases.append([segment])
    return _absorb_short_phases(phases, fps)


def _motion_change_ratio(left: MotionSegment, right: MotionSegment) -> float:
    """相邻两段的运动标签变化率(0~1):1 表示没有任何共同运动。"""

    left_labels = set(left.motions)
    right_labels = set(right.motions)
    union = left_labels | right_labels
    if not union:
        return 0.0
    return 1.0 - len(left_labels & right_labels) / len(union)


def _label_similarity(left: MotionSegment, right: MotionSegment) -> float:
    """相邻两段的运动标签相似度(0~1):交集数 ÷ 并集数,用于判断并入方向。"""

    left_labels = set(left.motions)
    right_labels = set(right.motions)
    union = left_labels | right_labels
    if not union:
        return 1.0
    return len(left_labels & right_labels) / len(union)


def _phase_duration(phase: Sequence[MotionSegment], fps: float) -> float:
    """阶段累计时长(秒)。"""

    return (phase[-1].end_frame - phase[0].start_frame) / fps


def _absorb_short_phases(
    phases: Sequence[Sequence[MotionSegment]], fps: float
) -> List[List[MotionSegment]]:
    """碎段保护:过短的阶段并入更相似的一侧,直到没有过短阶段。"""

    result = [list(phase) for phase in phases]
    while len(result) > 1:
        index = _first_short_index(result, fps)
        if index is None:
            break
        _absorb_phase_at(result, index)
    return result


def _first_short_index(phases: Sequence[Sequence[MotionSegment]], fps: float):
    """第一个短于最短时长的阶段下标;全部达标时返回 None。"""

    for index, phase in enumerate(phases):
        if _phase_duration(phase, fps) < PROMPT_MIN_PHASE_SECONDS:
            return index
    return None


def _absorb_phase_at(phases: List[List[MotionSegment]], index: int) -> None:
    """把 index 处的短阶段并入更相似的一侧(相同或只有左侧时并入左侧)。"""

    phase = phases[index]
    if index == 0:
        phases[1] = phase + phases[1]
        phases.pop(0)
        return
    if index == len(phases) - 1:
        phases[index - 1] = phases[index - 1] + phase
        phases.pop(index)
        return
    left_similarity = _label_similarity(phases[index - 1][-1], phase[0])
    right_similarity = _label_similarity(phase[-1], phases[index + 1][0])
    if left_similarity >= right_similarity:
        phases[index - 1] = phases[index - 1] + phase
        phases.pop(index)
    else:
        phases[index + 1] = phase + phases[index + 1]
        phases.pop(index)


def strip_display_warnings(text: str) -> str:
    """复制时去掉仅供界面查看的红字复检提示(兼容旧结果的内联警告)。"""

    lines = [
        line
        for line in text.replace("；" + COMPLEX_MOTION_WARNING, "").splitlines()
        if not line.startswith(DISPLAY_WARNING_PREFIX)
    ]
    return "\n".join(lines).strip()


def _format_number(value) -> str:
    """把数值格式化成简洁字符串(去掉多余小数;None 返回空串)。"""

    if value is None:
        return ""
    return f"{float(value):g}"


def _camera_local_motions(segment: MotionSegment) -> str:
    """把本段运动标签翻译成短语,用“，同时”连接(如“向前推进，同时向左横摇”)。"""

    phrases = [MOTION_PHRASES.get(motion, motion) for motion in segment.motions]
    return "，同时".join(phrases)


def _time_range(start: float, end: float, first_frame: float, fps: float) -> str:
    """事件的时间范围文本,如“0.25–0.50 秒（第 7–13 帧）”。"""

    return (
        f"{(start - first_frame) / fps:.2f}–{(end - first_frame) / fps:.2f} 秒"
        f"（第 {start:g}–{end:g} 帧）"
    )


def _trajectory_events(segment: MotionSegment) -> List[_Event]:
    """把本段的轨迹事件整理成 (起始帧, 结束帧, 文本) 列表(文本不含时间前缀)。"""

    relation = segment.target_relation
    events: List[_Event] = []
    subject_moves = relation["subject_path_length"] > 1e-5
    for event in relation["subject_motion_events"]:
        if event["direction"]:
            events.append(
                (event["start_frame"], event["end_frame"], "主体整体中心自身发生移动")
            )
        elif subject_moves:
            events.append(
                (event["start_frame"], event["end_frame"], "主体整体中心保持静止")
            )
    subject_directions = {
        "horizontal": {-1: "向左", 1: "向右"},
        "vertical": {-1: "向下", 1: "向上"},
        "depth": {-1: "沿画面纵深向远处", 1: "沿画面纵深向近处"},
    }
    for axis, axis_events in relation["subject_axis_events"].items():
        for event in axis_events:
            if event["direction"]:
                direction = subject_directions[axis][event["direction"]]
                events.append((
                    event["start_frame"], event["end_frame"],
                    f"以本段起始画面方向为参照，主体整体中心{direction}移动",
                ))
    for event in relation["orbit_events"]:
        angle = event["end_value"] - event["start_value"]
        if abs(angle) < 3.0:
            continue
        direction = "右" if angle > 0 else "左"
        if segment.target_aimed and not subject_moves:
            text = f"相对被摄主体向{direction}环绕约 {abs(angle):.1f}°"
        else:
            text = f"摄像机与主体的相对观察方位向{direction}变化约 {abs(angle):.1f}°"
        events.append((event["start_frame"], event["end_frame"], text))
    for event in relation["distance_events"]:
        descriptions = {-1: "距离逐渐缩短", 0: "距离基本不变", 1: "距离逐渐增加"}
        events.append((
            event["start_frame"], event["end_frame"],
            "摄像机与被摄主体的" + descriptions[event["direction"]],
        ))
    for event in relation["height_events"]:
        if event["direction"]:
            text = "升高" if event["direction"] > 0 else "降低"
            events.append((
                event["start_frame"], event["end_frame"],
                "摄像机相对主体的高度" + text,
            ))
    for event in relation["screen_events"]:
        events.append((
            event["start_frame"], event["end_frame"], "被摄主体位于" + event["label"]
        ))
    for event in relation.get("framing_events", []):
        events.append((event["start_frame"], event["end_frame"], event["label"]))
    return events


def _composition_phrase(segment: MotionSegment, has_targets: bool) -> str:
    """描述被摄主体在画面中的构图变化(保持原位 / 从哪个方位移到哪个方位)。"""

    if not has_targets or not segment.target_relation:
        return ""
    start = segment.target_relation.get("screen_start", {})
    end = segment.target_relation.get("screen_end", {})
    if not isinstance(start, Mapping) or not isinstance(end, Mapping):
        return ""
    start_label = start.get("label")
    end_label = end.get("label")
    if not start_label or not end_label:
        return ""
    if start_label == end_label:
        return f"被摄主体保持在{start_label}"
    return f"被摄主体从{start_label}移动到{end_label}"


def _legacy_viewpoint_events(segment: MotionSegment, has_targets: bool) -> List[_Event]:
    """兼容没有逐事件轨迹数据的旧结果:短语挂在本段整体范围上。"""

    if not has_targets or not segment.target_relation:
        return []
    relation = segment.target_relation
    phrases: List[str] = []
    orbit_direction = relation.get("orbit_direction")
    viewpoint_direction = relation.get("viewpoint_direction")
    viewpoint_angle = float(relation.get("viewpoint_angle_degrees", 0.0))
    # 环绕与普通视角变化用不同措辞:环绕强调“绕行”,普通只写观察角改变
    if orbit_direction:
        direction = "右" if orbit_direction == "right" else "左"
        phrases.append(f"相对被摄主体向{direction}环绕约 {viewpoint_angle:.1f}°")
    elif viewpoint_direction:
        direction = "右" if viewpoint_direction == "right" else "左"
        phrases.append(f"相对被摄主体向{direction}改变观察角度约 {viewpoint_angle:.1f}°")

    # 距离变化:±3% 以上才写,避免把微小噪声描述成变化
    distance_change = float(relation.get("distance_change_ratio", 0.0))
    if distance_change < -0.03:
        phrases.append("摄像机与被摄主体的距离逐渐缩短")
    elif distance_change > 0.03:
        phrases.append("摄像机与被摄主体的距离逐渐增加")
    else:
        phrases.append("摄像机与被摄主体的距离基本不变")

    # 相对高度变化(±3% 以上才写)
    height_change = float(relation.get("camera_height_change_ratio", 0.0))
    if height_change > 0.03:
        phrases.append("摄像机相对被摄主体升高")
    elif height_change < -0.03:
        phrases.append("摄像机相对被摄主体降低")

    composition = _composition_phrase(segment, has_targets)
    if composition:
        phrases.append(composition)
    return [(segment.start_frame, segment.end_frame, phrase) for phrase in phrases]


def _segment_events(segment: MotionSegment, has_targets: bool) -> List[_Event]:
    """本段的全部事件短语:新结果走逐事件轨迹,旧结果走整段兜底。"""

    if not has_targets or not segment.target_relation:
        return []
    if "distance_events" in segment.target_relation:
        return _trajectory_events(segment)
    return _legacy_viewpoint_events(segment, has_targets)


def _merge_and_group_events(
    events: Sequence[_Event],
) -> List[Tuple[float, float, List[str]]]:
    """先并合“同文本且首尾相接”的事件,再按时间范围分组,供段内去重排版。"""

    ranges_by_text: Dict[str, List[Tuple[float, float]]] = {}
    for start, end, text in events:
        ranges_by_text.setdefault(text, []).append((start, end))

    merged: List[_Event] = []
    for text, ranges in ranges_by_text.items():
        ranges.sort()
        current_start, current_end = ranges[0]
        for start, end in ranges[1:]:
            if start <= current_end + _FRAME_EPSILON:
                current_end = max(current_end, end)
            else:
                merged.append((current_start, current_end, text))
                current_start, current_end = start, end
        merged.append((current_start, current_end, text))
    merged.sort(key=lambda item: (item[0], item[1], item[2]))

    groups: List[Tuple[float, float, List[str]]] = []
    for start, end, text in merged:
        if (
            groups
            and abs(groups[-1][0] - start) <= _FRAME_EPSILON
            and abs(groups[-1][1] - end) <= _FRAME_EPSILON
        ):
            groups[-1][2].append(text)
        else:
            groups.append((start, end, [text]))
    return groups


def _render_event_groups(
    phase: Sequence[MotionSegment],
    events: Sequence[_Event],
    first_frame: float,
    fps: float,
) -> List[str]:
    """渲染去重后的短语:与阶段同范围的不重复时间,其余范围每组只写一次。"""

    phase_start, phase_end = phase[0].start_frame, phase[-1].end_frame
    phrases: List[str] = []
    for start, end, texts in _merge_and_group_events(events):
        body = "，".join(texts)
        if (
            abs(start - phase_start) <= _FRAME_EPSILON
            and abs(end - phase_end) <= _FRAME_EPSILON
        ):
            phrases.append(body)
        else:
            phrases.append(_time_range(start, end, first_frame, fps) + "，" + body)
    return phrases


def _phase_parameter_phrases(phase: Sequence[MotionSegment]) -> List[str]:
    """描述阶段内发生变化的镜头参数(start 取首段、end 取末段、极值取各段包络)。"""

    metrics_list = [segment.metrics for segment in phase]
    phrases: List[str] = []
    # (metrics 键前缀, 判定公差, 中文标签, 单位后缀)
    parameter_specs = (
        ("focal_length_mm", 0.01, "焦距", " mm"),
        ("f_stop", 0.01, "光圈", ""),
        ("shutter_angle_degrees", 0.01, "快门角度", "°"),
        ("focus_distance", 0.01, "对焦距离", ""),
    )
    for key, tolerance, label, suffix in parameter_specs:
        starts = [metrics.get(f"{key}_start") for metrics in metrics_list]
        ends = [metrics.get(f"{key}_end") for metrics in metrics_list]
        minimums = [metrics.get(f"{key}_min") for metrics in metrics_list]
        maximums = [metrics.get(f"{key}_max") for metrics in metrics_list]
        if any(value is None for value in starts + ends + minimums + maximums):
            continue
        start = float(starts[0])
        end = float(ends[-1])
        minimum = min(float(value) for value in minimums)
        maximum = max(float(value) for value in maximums)
        if maximum - minimum <= tolerance:
            continue
        if label == "光圈":
            phrases.append(
                f"光圈从 f/{_format_number(start)} 变化到 f/{_format_number(end)}"
            )
        elif abs(end - start) <= tolerance:
            phrases.append(
                f"{label}在 {_format_number(minimum)}–{_format_number(maximum)}{suffix} 间变化"
            )
        else:
            phrases.append(
                f"{label}从 {_format_number(start)}{suffix} 变化到 "
                f"{_format_number(end)}{suffix}"
            )

    # 景深在阶段内发生切换时,写明本阶段结束时的状态
    if any(metrics.get("depth_of_field_changed") for metrics in metrics_list):
        phrases.append(
            "本段开启景深"
            if metrics_list[-1].get("depth_of_field_end")
            else "本段关闭景深"
        )
    return phrases


def _phase_motion_sentence(phase: Sequence[MotionSegment]) -> str:
    """阶段首句:单细分段沿用原句式;多细分段用“先/随后/最后”串联趋势。"""

    if len(phase) == 1:
        return f"摄像机{_camera_local_motions(phase[0])}"
    parts: List[str] = []
    last_index = len(phase) - 1
    for index, segment in enumerate(phase):
        phrase = _camera_local_motions(segment)
        if segment.motions != ("static",):
            phrase += "，" + SPEED_PHRASES[segment.speed_profile]
        if index == 0:
            prefix = "先"
        elif index == last_index and last_index > 1:
            prefix = "最后"
        else:
            prefix = "随后"
        parts.append(prefix + phrase)
    return "摄像机" + "；".join(parts)


def _display_warning_lines(
    phase: Sequence[MotionSegment], anomalies: Sequence[MotionAnomaly]
) -> List[str]:
    """本阶段的红字复检提示行:异常帧落在阶段内或含复杂运动时生成。"""

    start_frame = phase[0].start_frame
    end_frame = phase[-1].end_frame
    messages: List[str] = []
    for anomaly in anomalies:
        if start_frame - _FRAME_EPSILON <= anomaly.frame <= end_frame + _FRAME_EPSILON:
            messages.append(anomaly.message)
    if any(segment.complex_motion for segment in phase):
        messages.append(COMPLEX_MOTION_WARNING + "，请人工复检。")
    return [DISPLAY_WARNING_PREFIX + message for message in dict.fromkeys(messages)]


def _phase_paragraph(
    phase: Sequence[MotionSegment],
    anomalies: Sequence[MotionAnomaly],
    has_targets: bool,
    first_frame: float,
    fps: float,
) -> str:
    """渲染一个提示词阶段:标题 + 运动趋势句 + 去重事件 + 红字复检提示。"""

    start_frame, end_frame = phase[0].start_frame, phase[-1].end_frame
    start_seconds = (start_frame - first_frame) / fps
    end_seconds = (end_frame - first_frame) / fps
    # 秒数以整段起点为 0,便于视频模型对照
    heading = (
        f"【{start_seconds:.2f}–{end_seconds:.2f} 秒｜第 "
        f"{start_frame:g}–{end_frame:g} 帧】"
    )

    sentence = _phase_motion_sentence(phase)
    events: List[_Event] = []
    for segment in phase:
        events.extend(_segment_events(segment, has_targets))
    # 细节按固定顺序拼接:速度(仅单段) → 事件短语 → 是否持续对准 → 段内参数变化
    details: List[str] = []
    if len(phase) == 1 and phase[0].motions != ("static",):
        details.append(SPEED_PHRASES[phase[0].speed_profile])
    details.extend(_render_event_groups(phase, events, first_frame, fps))
    if (
        has_targets
        and all(segment.target_relation and segment.target_aimed for segment in phase)
    ):
        details.append("镜头持续朝向被摄主体的整体中心")
    details.extend(_phase_parameter_phrases(phase))

    lines = [heading]
    if len(phase) == 1:
        if details:
            sentence += "，" + "；".join(details)
        lines.append(sentence + "。")
    else:
        lines.append(sentence + "。")
        if details:
            lines.append("；".join(details) + "。")
    lines.extend(_display_warning_lines(phase, anomalies))
    return "\n".join(lines)


def _constant_global_parameters(metadata: Mapping[str, object]) -> List[str]:
    """汇总全镜头保持不变的参数,写进结尾的“【全镜头摄影参数】”行。"""

    phrases: List[str] = []
    parameter_specs = (
        ("focal_length_mm", 0.01, "焦距", " mm"),
        ("f_stop", 0.01, "光圈", ""),
        ("shutter_angle_degrees", 0.01, "快门角度", "°"),
        ("focus_distance", 0.01, "对焦距离", ""),
    )
    for key, tolerance, label, suffix in parameter_specs:
        start = metadata.get(f"{key}_start")
        minimum = metadata.get(f"{key}_min")
        maximum = metadata.get(f"{key}_max")
        if start is None or minimum is None or maximum is None:
            continue
        if abs(float(maximum) - float(minimum)) > tolerance:
            continue
        if label == "光圈":
            phrases.append(f"光圈保持在 f/{_format_number(start)}")
        else:
            phrases.append(f"{label}保持在 {_format_number(start)}{suffix}")

    # 全程开着景深且从未切换过,才写“全程启用景深”
    if (
        not metadata.get("depth_of_field_changed")
        and metadata.get("depth_of_field_start")
    ):
        phrases.append("全程启用景深")
    return phrases


def build_prompt(
    segments: Sequence[MotionSegment],
    metadata: Mapping[str, object],
    fps: float,
    has_targets: bool,
    anomalies: Sequence[MotionAnomaly] = (),
    merge_threshold: float = PROMPT_MERGE_THRESHOLD_DEFAULT,
) -> str:
    """把分析结果渲染成完整的中文提示词文本。

    结构:每个提示词阶段一个段落(标题 + 运动趋势 + 去重事件 + 红字复检提示),
    段落之间留空行,末尾追加“【全镜头摄影参数】”。
    merge_threshold 控制阶段拆分粒度(见 group_phases),界面滑杆可实时重排段落。
    红字复检提示仅供界面查看,复制时由 strip_display_warnings 整行剔除;
    完整警告列表仍保留在结构化详情与界面状态中。
    """

    lines = []
    if segments:
        first_frame = segments[0].start_frame
        for phase in group_phases(segments, fps, merge_threshold):
            lines.append(
                _phase_paragraph(phase, anomalies, has_targets, first_frame, fps)
            )
    global_parameters = _constant_global_parameters(metadata)
    if global_parameters:
        lines.append("【全镜头摄影参数】" + "，".join(global_parameters) + "。")
    return "\n\n".join(lines)
