"""提示词分段合并、段内去重与红字复检提示的单元测试。

覆盖 group_phases 的运动变化率拆分、碎段保护与阈值语义,
以及 build_prompt 的段落渲染、红字复检行与 strip_display_warnings。
"""

import unittest

from camera_motion_describer.models import MotionAnomaly, MotionSegment
from camera_motion_describer.prompt import (
    DISPLAY_WARNING_PREFIX,
    build_prompt,
    group_phases,
    strip_display_warnings,
)


def segment(
    start,
    end,
    motions=("static",),
    speed="steady",
    aimed=False,
    complex_motion=False,
    metrics=None,
    relation=None,
):
    """构造测试用细分段:默认静止、匀速、无主体关系。"""

    return MotionSegment(
        start_frame=float(start),
        end_frame=float(end),
        motions=tuple(motions),
        speed_profile=speed,
        confidence=1.0,
        target_aimed=aimed,
        complex_motion=complex_motion,
        metrics=metrics or {},
        target_relation=relation or {},
    )


def trajectory_relation(distance=(), screen=(), orbit=(), framed=()):
    """构造走逐事件轨迹路径的 target_relation(含 distance_events 键)。"""

    return {
        "subject_path_length": 0.0,
        "subject_motion_events": [],
        "subject_axis_events": {"horizontal": [], "vertical": [], "depth": []},
        "orbit_events": list(orbit),
        "distance_events": list(distance),
        "height_events": [],
        "screen_events": list(screen),
        "framing_events": list(framed),
        "screen_start": {"label": "画面中央"},
        "screen_end": {"label": "画面中央"},
    }


def distance_event(start, end, direction):
    return {"start_frame": start, "end_frame": end, "direction": direction}


def screen_event(start, end, label):
    return {"start_frame": start, "end_frame": end, "label": label}


class PhaseGroupingTests(unittest.TestCase):
    """group_phases:运动变化率拆分、碎段保护与阈值语义。"""

    def test_demo_scene_turn_segment_splits_and_short_tail_joins_right(self):
        # demo 场景:60-90(横移+横摇)是转向段,应独立;
        # 90-93(3 帧碎段)与同为拉远变焦的 93-120 更相似,并入右侧。
        segments = [
            segment(1, 24, ("static",)),
            segment(24, 60, ("dolly_in",)),
            segment(60, 90, ("truck_right", "pan_left")),
            segment(90, 93, ("dolly_out", "truck_right", "zoom_in")),
            segment(93, 120, ("dolly_out", "zoom_in")),
        ]
        phases = group_phases(segments, 24.0)
        self.assertEqual(len(phases), 4)
        self.assertEqual(phases[2][0].start_frame, 60.0)
        self.assertEqual(phases[2][-1].end_frame, 90.0)
        self.assertEqual(phases[3][0].start_frame, 90.0)
        self.assertEqual(phases[3][-1].end_frame, 120.0)

    def test_long_segments_stay_in_separate_phases(self):
        segments = [
            segment(1, 25, ("dolly_in",)),
            segment(25, 49, ("truck_left",)),
        ]
        self.assertEqual(len(group_phases(segments, 24.0)), 2)
        prompt = build_prompt(segments, {}, 24.0, False)
        self.assertEqual(prompt.count("【"), 2)
        self.assertIn("【0.00–1.00 秒｜第 1–25 帧】", prompt)
        self.assertIn("【1.00–2.00 秒｜第 25–49 帧】", prompt)

    def test_full_change_splits_at_any_threshold(self):
        # 完全换动作(变化率 1.0):即使阈值拉满也拆开
        segments = [
            segment(1, 25, ("dolly_in",)),
            segment(25, 49, ("truck_left",)),
        ]
        self.assertEqual(len(group_phases(segments, 24.0, 1.0)), 2)
        self.assertEqual(len(group_phases(segments, 24.0, 0.0)), 2)

    def test_partial_overlap_merge_depends_on_threshold(self):
        # 部分重叠(变化率 0.5):默认阈值合并,调低阈值拆开
        segments = [
            segment(1, 25, ("dolly_out", "truck_right")),
            segment(25, 49, ("dolly_out",)),
        ]
        self.assertEqual(len(group_phases(segments, 24.0, 0.6)), 1)
        self.assertEqual(len(group_phases(segments, 24.0, 0.4)), 2)

    def test_higher_threshold_never_increases_phase_count(self):
        # 单调性:阈值越高段落数不增(界面滑杆"越高越少"的依据)
        segments = [
            segment(1, 25, ("dolly_out", "truck_right")),
            segment(25, 49, ("dolly_out",)),
            segment(49, 73, ("dolly_out", "truck_right")),
            segment(73, 97, ("static",)),
        ]
        counts = [
            len(group_phases(segments, 24.0, threshold))
            for threshold in (0.0, 0.25, 0.5, 0.75, 1.0)
        ]
        self.assertEqual(counts, sorted(counts, reverse=True))

    def test_short_phase_absorbs_into_more_similar_right_side(self):
        # 中间碎段(3 帧)与右侧标签重叠更多,并入右侧阶段
        segments = [
            segment(1, 25, ("truck_right", "pan_left")),
            segment(25, 28, ("dolly_out", "truck_right", "zoom_in")),
            segment(28, 52, ("dolly_out", "zoom_in")),
        ]
        phases = group_phases(segments, 24.0, 0.3)
        self.assertEqual(len(phases), 2)
        self.assertEqual(phases[1][0].start_frame, 25.0)
        self.assertEqual(phases[1][-1].end_frame, 52.0)

    def test_short_phase_absorbs_into_more_similar_left_side(self):
        # 中间碎段与左侧标签重叠更多,并入左侧阶段
        segments = [
            segment(1, 25, ("dolly_out", "zoom_in")),
            segment(25, 28, ("dolly_out", "truck_right", "zoom_in")),
            segment(28, 52, ("static",)),
        ]
        phases = group_phases(segments, 24.0, 0.3)
        self.assertEqual(len(phases), 2)
        self.assertEqual(phases[0][0].start_frame, 1.0)
        self.assertEqual(phases[0][-1].end_frame, 28.0)

    def test_short_phase_protection_boundary(self):
        # 恰好达标(5 帧 ≈ 0.208 秒)保留独立;不足(4 帧 ≈ 0.167 秒)并入邻居
        kept = [
            segment(1, 21, ("dolly_in",)),
            segment(21, 26, ("truck_left",)),
            segment(26, 46, ("pedestal_up",)),
        ]
        self.assertEqual(len(group_phases(kept, 24.0)), 3)
        absorbed = [
            segment(1, 21, ("dolly_in",)),
            segment(21, 25, ("truck_left",)),
            segment(25, 45, ("pedestal_up",)),
        ]
        self.assertEqual(len(group_phases(absorbed, 24.0)), 2)


class PromptRenderingTests(unittest.TestCase):
    """build_prompt 与 strip_display_warnings:段落渲染、去重与红字剔除。"""

    def test_short_segments_merge_and_render_trend_sentence(self):
        segments = [
            segment(1, 12, ("dolly_in",), speed="decelerating"),
            segment(12, 14, ("truck_right",), speed="steady"),
            segment(14, 16, ("static",)),
        ]
        self.assertEqual(len(group_phases(segments, 24.0)), 1)
        prompt = build_prompt(segments, {}, 24.0, False)
        self.assertIn(
            "摄像机先向前推进，运动由快到慢；"
            "随后向右横移，速度基本均匀；最后保持固定机位。",
            prompt,
        )

    def test_phase_wide_events_do_not_repeat_time_range(self):
        relation = trajectory_relation(distance=[distance_event(1, 13, -1)])
        segments = [segment(1, 13, ("dolly_in",), relation=relation)]
        prompt = build_prompt(segments, {}, 24.0, True)
        self.assertEqual(
            prompt,
            "【0.00–0.50 秒｜第 1–13 帧】\n"
            "摄像机向前推进，速度基本均匀；"
            "摄像机与被摄主体的距离逐渐缩短。",
        )

    def test_adjacent_same_events_merge_into_one_range(self):
        relation = trajectory_relation(
            distance=[distance_event(1, 5, 1), distance_event(5, 9, 1)]
        )
        segments = [segment(1, 9, ("dolly_out",), relation=relation)]
        prompt = build_prompt(segments, {}, 24.0, True)
        self.assertEqual(prompt.count("距离逐渐增加"), 1)
        self.assertNotIn("第 1–5 帧", prompt)
        self.assertNotIn("第 5–9 帧", prompt)

    def test_partial_range_events_share_one_prefix_group(self):
        relation = trajectory_relation(
            distance=[distance_event(3, 7, 1)],
            screen=[screen_event(3, 7, "画面左侧")],
        )
        segments = [segment(1, 13, relation=relation)]
        prompt = build_prompt(segments, {}, 24.0, True)
        self.assertEqual(prompt.count("0.08–0.25 秒（第 3–7 帧）"), 1)
        self.assertIn(
            "摄像机与被摄主体的距离逐渐增加，被摄主体位于画面左侧", prompt
        )

    def test_anomaly_warning_only_in_its_own_phase(self):
        segments = [
            segment(1, 25, ("dolly_in",)),
            segment(25, 49, ("truck_left",)),
        ]
        anomalies = [
            MotionAnomaly(
                frame=5.0,
                kind="position_spike",
                magnitude=1.0,
                message="第 5 帧摄像机位置出现单帧跳变。",
            )
        ]
        prompt = build_prompt(segments, {}, 24.0, False, anomalies)
        first, second = prompt.split("\n\n")
        self.assertIn(DISPLAY_WARNING_PREFIX + "第 5 帧摄像机位置出现单帧跳变。", first)
        self.assertNotIn(DISPLAY_WARNING_PREFIX, second)
        self.assertEqual(prompt.count(DISPLAY_WARNING_PREFIX), 1)

    def test_complex_motion_adds_review_warning(self):
        segments = [segment(1, 25, ("dolly_in", "pan_left"), complex_motion=True)]
        prompt = build_prompt(segments, {}, 24.0, False)
        self.assertIn(
            DISPLAY_WARNING_PREFIX + "该段包含未完全展开的复杂复合运动，请人工复检。",
            prompt,
        )

    def test_strip_keeps_body_and_removes_warning_lines(self):
        segments = [segment(1, 25, ("dolly_in",), complex_motion=True)]
        prompt = build_prompt(segments, {}, 24.0, False)
        clean = strip_display_warnings(prompt)
        self.assertNotIn(DISPLAY_WARNING_PREFIX, clean)
        self.assertNotIn("复杂复合运动", clean)
        self.assertIn("摄像机向前推进", clean)
        self.assertIn("【0.00–1.00 秒｜第 1–25 帧】", clean)

    def test_strip_removes_legacy_inline_complex_motion_text(self):
        legacy = "摄像机向前推进；该段包含未完全展开的复杂复合运动。"
        self.assertEqual(strip_display_warnings(legacy), "摄像机向前推进。")

    def test_single_segment_output_matches_legacy_format(self):
        relation = trajectory_relation(distance=[distance_event(3, 7, 1)])
        segments = [segment(1, 13, ("dolly_in",), relation=relation)]
        prompt = build_prompt(segments, {}, 24.0, True)
        self.assertEqual(
            prompt,
            "【0.00–0.50 秒｜第 1–13 帧】\n"
            "摄像机向前推进，速度基本均匀；"
            "0.08–0.25 秒（第 3–7 帧），摄像机与被摄主体的距离逐渐增加。",
        )

    def test_phase_parameters_span_first_and_last_segment(self):
        segments = [
            segment(
                1,
                11,
                ("dolly_in",),
                metrics={
                    "focal_length_mm_start": 35.0,
                    "focal_length_mm_end": 40.0,
                    "focal_length_mm_min": 35.0,
                    "focal_length_mm_max": 40.0,
                },
            ),
            segment(
                11,
                21,
                ("dolly_in", "truck_right"),
                metrics={
                    "focal_length_mm_start": 40.0,
                    "focal_length_mm_end": 45.0,
                    "focal_length_mm_min": 40.0,
                    "focal_length_mm_max": 45.0,
                },
            ),
        ]
        self.assertEqual(len(group_phases(segments, 24.0)), 1)
        prompt = build_prompt(segments, {}, 24.0, False)
        self.assertEqual(prompt.count("焦距从"), 1)
        self.assertIn("焦距从 35 mm 变化到 45 mm", prompt)

    def test_build_prompt_respects_merge_threshold(self):
        # 界面滑杆重排链路:同一份细分段,阈值不同则段落数不同
        segments = [
            segment(1, 25, ("dolly_out", "truck_right")),
            segment(25, 49, ("dolly_out",)),
        ]
        merged = build_prompt(segments, {}, 24.0, False, merge_threshold=0.6)
        split = build_prompt(segments, {}, 24.0, False, merge_threshold=0.4)
        self.assertEqual(merged.count("【"), 1)
        self.assertEqual(split.count("【"), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
