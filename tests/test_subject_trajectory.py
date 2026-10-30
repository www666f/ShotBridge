"""已知轨迹验收：中途变化不得被相同的首尾位置抵消。"""

import unittest
import math
from dataclasses import replace

from camera_motion_describer.analysis import analyze_samples
from tests.test_analysis import CONFIG, sample
from camera_motion_describer.math3d import quaternion_from_axis_angle


class SubjectTrajectoryTests(unittest.TestCase):
    def test_complete_orbit_is_not_cancelled_by_matching_endpoints(self):
        samples = []
        for index in range(73):
            degrees = index * 5.0
            angle = math.radians(degrees)
            samples.append(sample(index + 1, (10*math.sin(angle), 0, 10*math.cos(angle)),
                                  quaternion_from_axis_angle((0, 1, 0), degrees), target=(0, 0, 0)))
        report = analyze_samples(samples, target_names=("subject",), config=CONFIG)
        total = sum(s.target_relation["signed_orbit_degrees"] for s in report.segments)
        self.assertAlmostEqual(total, 360.0)
        self.assertIn("360.0°", report.prompt)

    def test_target_exits_frame_and_returns(self):
        xs = [0, 2, 4, 6, 8, 6, 4, 2, 0]
        report = analyze_samples([sample(i+1, (0, 0, 10), target=(x, 0, 0))
                                  for i, x in enumerate(xs)], target_names=("subject",), config=CONFIG)
        labels = [event["label"] for event in report.segments[0].target_relation["screen_events"]]
        self.assertIn("画面右侧之外", labels)
        self.assertEqual(labels[-1], "画面中央")

    def test_subject_pause_and_return_have_ordered_ranges(self):
        xs = [0, -1, -2, -2, -2, -1, 0]
        report = analyze_samples([sample(i+1, (0, 0, 10), target=(x, 0, 0))
                                  for i, x in enumerate(xs)], target_names=("subject",), config=CONFIG)
        events = report.segments[0].target_relation["subject_axis_events"]["horizontal"]
        self.assertEqual([event["direction"] for event in events], [-1, 0, 1])
        self.assertEqual([(e["start_frame"], e["end_frame"]) for e in events], [(1, 3), (3, 5), (5, 7)])

    def test_moving_camera_does_not_invent_subject_world_motion(self):
        report = analyze_samples([sample(i+1, (i*0.2, 0, 10), target=(0, 0, 0))
                                  for i in range(12)], target_names=("subject",), config=CONFIG)
        self.assertNotIn("主体整体中心自身发生移动", report.prompt)
        self.assertTrue(all(s.target_relation["subject_path_length"] == 0 for s in report.segments))

    def test_time_ranges_follow_frame_rate_and_have_no_gaps(self):
        samples = [sample(i+101, (0, 0, 10-i*0.2 if i < 6 else 8.8), target=(0, 0, 0))
                   for i in range(13)]
        report = analyze_samples(samples, target_names=("subject",), config=replace(CONFIG, fps=48))
        self.assertIn("0.25 秒", report.prompt)
        for left, right in zip(report.segments, report.segments[1:]):
            self.assertEqual(left.end_frame, right.start_frame)

    def test_z_up_relative_height_does_not_become_horizontal_orbit(self):
        report = analyze_samples([sample(i+1, (0, 10, i*0.2), target=(0, 0, 0))
                                  for i in range(12)], target_names=("subject",),
                                 config=replace(CONFIG, world_up=(0, 0, 1)))
        self.assertAlmostEqual(sum(s.target_relation["signed_orbit_degrees"] for s in report.segments), 0)
        self.assertIn("相对主体的高度升高", report.prompt)

    def test_subject_moves_left_then_returns_in_fixed_camera(self):
        xs = [0, -1, -2, -3, -4, -3, -2, -1, 0]
        report = analyze_samples([
            sample(i + 1, (0, 0, 10), target=(x, 0, 0))
            for i, x in enumerate(xs)
        ], target_names=("subject",), config=CONFIG)
        relation = report.segments[0].target_relation
        self.assertEqual(report.segments[0].motions, ("static",))
        self.assertEqual(
            [item["label"] for item in relation["screen_events"]],
            ["画面中央", "画面左侧", "画面中央"],
        )
        self.assertIn("第 3–7 帧），被摄主体位于画面左侧", report.prompt)
        self.assertIn("主体整体中心自身发生移动", report.prompt)

    def test_subject_approaches_then_retreats(self):
        zs = [0, 1, 2, 3, 4, 3, 2, 1, 0]
        report = analyze_samples([
            sample(i + 1, (0, 0, 10), target=(0, 0, z))
            for i, z in enumerate(zs)
        ], target_names=("subject",), config=CONFIG)
        events = report.segments[0].target_relation["distance_events"]
        self.assertEqual([item["direction"] for item in events], [-1, 1])
        self.assertEqual(events[0]["end_frame"], 5)
        self.assertEqual(events[1]["start_frame"], 5)
        self.assertNotIn("距离基本不变", report.prompt)
        self.assertIn("距离逐渐缩短", report.prompt)
        self.assertIn("距离逐渐增加", report.prompt)


class SpikeToleranceTests(unittest.TestCase):
    def camera_spike(self):
        return [sample(i+1, (0, 20 if i == 5 else 0, 10-i*0.5)) for i in range(12)]

    def test_filter_off_preserves_short_motion_and_original_samples(self):
        samples = self.camera_spike()
        original = list(samples)
        filtered = analyze_samples(samples, config=CONFIG)
        retained = analyze_samples(samples, config=replace(CONFIG, filter_spikes=False))
        self.assertEqual(samples, original)
        self.assertFalse(any("pedestal_up" in s.motions for s in filtered.segments))
        self.assertTrue(any("pedestal_up" in s.motions for s in retained.segments))
        self.assertIn("保留参与分析", retained.warnings[0])
        self.assertTrue(any(s.end_frame - s.start_frame == 1 for s in retained.segments))

    def test_higher_tolerance_keeps_camera_spike(self):
        strict = analyze_samples(self.camera_spike(), config=CONFIG)
        tolerant = analyze_samples(self.camera_spike(), config=replace(CONFIG, spike_multiplier=1000000))
        self.assertEqual(len(strict.anomalies), 1)
        self.assertEqual(len(tolerant.anomalies), 0)
        self.assertTrue(any("pedestal_up" in s.motions for s in tolerant.segments))

    def test_target_spike_filtered_or_retained_by_same_controls(self):
        samples = [sample(i+1, (0, 0, 10), target=(20 if i == 5 else 0, 0, i*0.2))
                   for i in range(12)]
        filtered = analyze_samples(samples, target_names=("subject",), config=CONFIG)
        retained = analyze_samples(samples, target_names=("subject",), config=replace(CONFIG, filter_spikes=False))
        self.assertTrue(any(a.kind == "target_position_spike" for a in filtered.anomalies))
        self.assertNotIn("画面右侧之外", filtered.prompt)
        self.assertIn("画面右侧之外", retained.prompt)

    def test_normal_multi_frame_return_is_not_removed(self):
        xs = [0, 1, 2, 3, 4, 3, 2, 1, 0]
        report = analyze_samples([sample(i+1, (0, 0, 10), target=(x, 0, 0))
                                  for i, x in enumerate(xs)], target_names=("subject",), config=CONFIG)
        self.assertFalse(report.anomalies)
        self.assertEqual(report.segments[0].target_relation["subject_center_bounds"]["max"][0], 4)

    def test_invalid_tolerance_rejected(self):
        for value in (0, -1, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                analyze_samples(self.camera_spike(), config=replace(CONFIG, spike_multiplier=value))


if __name__ == "__main__":
    unittest.main()
