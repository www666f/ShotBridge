import unittest
import math

from camera_motion_describer.analysis import analyze_samples
from camera_motion_describer.math3d import (
    add,
    quaternion_from_axis_angle,
    quaternion_rotate,
)
from camera_motion_describer.models import AnalysisConfig, CameraSample
from camera_motion_describer.prompt import strip_display_warnings


IDENTITY = (0.0, 0.0, 0.0, 1.0)
CONFIG = AnalysisConfig(
    fps=24.0,
    minimum_segment_frames=2,
    bridge_gap_frames=1,
)


def sample(
    frame,
    position,
    orientation=IDENTITY,
    focal=35.0,
    target=None,
    horizontal_fov=60.0,
    vertical_fov=40.0,
    target_bounds=None,
):
    return CameraSample(
        frame=float(frame),
        position=position,
        orientation=orientation,
        focal_length=focal,
        horizontal_fov=horizontal_fov,
        vertical_fov=vertical_fov,
        f_stop=2.8,
        shutter_angle=180.0,
        depth_of_field=True,
        target_center=target,
        target_bounds_min=target_bounds[0] if target_bounds else None,
        target_bounds_max=target_bounds[1] if target_bounds else None,
    )


class CameraAnalysisTests(unittest.TestCase):
    def test_static_camera(self):
        samples = [sample(frame, (0.0, 0.0, 10.0)) for frame in range(1, 11)]
        report = analyze_samples(samples, config=CONFIG)
        self.assertEqual(len(report.segments), 1)
        self.assertEqual(report.segments[0].motions, ("static",))
        self.assertIn("保持固定机位", report.prompt)

    def test_local_negative_z_is_dolly_in(self):
        samples = [
            sample(frame, (0.0, 0.0, 10.0 - (frame - 1) * 0.5))
            for frame in range(1, 11)
        ]
        report = analyze_samples(samples, config=CONFIG)
        self.assertEqual(report.segments[0].motions[0], "dolly_in")
        self.assertIn("向前推进", report.prompt)

    def test_pan_left_from_positive_local_y_rotation(self):
        samples = [
            sample(
                frame,
                (0.0, 0.0, 10.0),
                quaternion_from_axis_angle((0.0, 1.0, 0.0), (frame - 1) * 2.0),
            )
            for frame in range(1, 11)
        ]
        report = analyze_samples(samples, config=CONFIG)
        self.assertEqual(report.segments[0].motions[0], "pan_left")

    def test_compound_dolly_and_pan(self):
        samples = []
        position = (0.0, 0.0, 10.0)
        for frame in range(1, 16):
            orientation = quaternion_from_axis_angle(
                (0.0, 1.0, 0.0), (frame - 1) * 1.5
            )
            samples.append(sample(frame, position, orientation))
            position = add(position, quaternion_rotate(orientation, (0.0, 0.0, -0.4)))
        report = analyze_samples(samples, config=CONFIG)
        motions = report.segments[0].motions
        self.assertIn("dolly_in", motions)
        self.assertIn("pan_left", motions)

    def test_structured_data_keeps_all_motion_channels(self):
        samples = []
        for frame in range(1, 6):
            samples.append(sample(frame, (frame * 0.5, frame * 0.25, 10.0 - frame)))
        report = analyze_samples(samples, config=CONFIG)
        details = report.to_dict()
        self.assertEqual(details["metadata"]["schema_version"], "1.0.0")
        self.assertEqual(
            set(details["segments"][0]["metrics"]["motion_channels"]),
            {"dolly", "truck", "pedestal", "pan", "tilt", "roll", "zoom"},
        )
        self.assertEqual(len(details["metadata"]["motion_steps"]), 4)
        self.assertEqual(
            details["metadata"]["coordinate_system"]["camera_forward"], "-Z"
        )

    def test_position_spike_is_warned_and_ignored(self):
        samples = []
        for frame in range(1, 13):
            position = (0.0, 0.0, 10.0 - (frame - 1) * 0.5)
            if frame == 6:
                position = (0.0, 20.0, position[2])
            samples.append(sample(frame, position))
        report = analyze_samples(samples, config=CONFIG)
        self.assertEqual(len(report.anomalies), 1)
        self.assertEqual(report.anomalies[0].kind, "position_spike")
        self.assertTrue(
            all("pedestal" not in motion for segment in report.segments for motion in segment.motions)
        )
        self.assertIn("第 6 帧", report.warnings[0])
        # 异常帧所在提示词段落末尾显示红字复检提示行;
        # 复制时由 strip_display_warnings 整行剔除,正文保持完整。
        self.assertIn("【复检提示】", report.prompt)
        self.assertIn("第 6 帧", report.prompt.split("【复检提示】")[1])
        clean = strip_display_warnings(report.prompt)
        self.assertNotIn("【复检提示】", clean)
        self.assertNotIn("单帧跳变", clean)
        self.assertIn("摄像机向前推进", clean)

    def test_focal_change_is_reported_as_zoom(self):
        samples = [
            sample(frame, (0.0, 0.0, 10.0), focal=35.0 + frame)
            for frame in range(1, 11)
        ]
        report = analyze_samples(samples, config=CONFIG)
        self.assertIn("zoom_in", report.segments[0].motions)
        self.assertIn("焦距从", report.prompt)

    def test_target_aim_is_detected(self):
        samples = [
            sample(frame, (0.0, 0.0, 10.0 - frame * 0.25), target=(0.0, 0.0, 0.0))
            for frame in range(1, 11)
        ]
        report = analyze_samples(
            samples, target_names=("target",), config=CONFIG
        )
        self.assertTrue(report.segments[0].target_aimed)
        self.assertIn("被摄主体的整体中心", report.prompt)

    def test_target_screen_position_is_described(self):
        samples = [
            sample(
                frame,
                (0.0, 0.0, 0.0),
                target=(-4.0, 2.0, -10.0),
            )
            for frame in range(1, 9)
        ]
        report = analyze_samples(samples, target_names=("subject",), config=CONFIG)
        screen = report.segments[0].target_relation["screen_start"]
        self.assertEqual(screen["label"], "画面左侧偏上")
        self.assertIn("被摄主体位于画面左侧偏上", report.prompt)

    def test_subject_bounds_report_full_and_partial_framing(self):
        samples = [
            sample(frame, (0, 0, 0), target=(0, 0, -10),
                   target_bounds=((-1, -1, -11), (1, 1, -9)))
            for frame in range(1, 5)
        ]
        samples.extend(
            sample(frame, (0, 0, 0), target=(5, 0, -10),
                   target_bounds=((4, -1, -11), (6, 1, -9)))
            for frame in range(5, 9)
        )
        report = analyze_samples(samples, target_names=("subject",), config=CONFIG)
        states = [
            event["state"]
            for segment in report.segments
            for event in segment.target_relation["framing_events"]
        ]
        self.assertIn("fully_in_frame", states)
        self.assertIn("partially_in_frame", states)

    def test_target_orbit_keeps_local_and_subject_relationships(self):
        samples = []
        target = (0.0, 0.0, 0.0)
        for frame in range(1, 17):
            amount = (frame - 1) / 15.0
            angle = math.radians(24.0 * amount)
            radius = 10.0 - 1.5 * amount
            position = (math.sin(angle) * radius, 0.0, math.cos(angle) * radius)
            orientation = quaternion_from_axis_angle(
                (0.0, 1.0, 0.0), math.degrees(angle)
            )
            samples.append(sample(frame, position, orientation, target=target))
        report = analyze_samples(samples, target_names=("subject",), config=CONFIG)
        relation = report.segments[0].target_relation
        self.assertEqual(relation["orbit_direction"], "right")
        self.assertIn("向右横移", report.prompt)
        self.assertIn("相对被摄主体向右环绕", report.prompt)
        self.assertIn("距离逐渐缩短", report.prompt)

    def test_changed_focal_is_inside_its_time_segment(self):
        samples = []
        for frame in range(1, 11):
            focal = 35.0 if frame <= 4 else 35.0 + (frame - 4) * 2.0
            samples.append(sample(frame, (0.0, 0.0, 10.0), focal=focal))
        report = analyze_samples(samples, config=CONFIG)
        lines = report.prompt.splitlines()
        focal_lines = [line for line in lines if "焦距从" in line]
        self.assertEqual(len(focal_lines), 1)
        self.assertTrue(report.prompt.startswith("【"))
        self.assertNotIn("焦距保持", lines[-1])

    def test_constant_parameters_are_only_written_globally(self):
        samples = [sample(frame, (0.0, 0.0, 10.0)) for frame in range(1, 9)]
        report = analyze_samples(samples, config=CONFIG)
        lines = report.prompt.splitlines()
        self.assertNotIn("焦距保持", report.prompt.split("\n\n")[0])
        self.assertTrue(lines[-1].startswith("【全镜头摄影参数】"))
        self.assertIn("焦距保持在 35 mm", lines[-1])

    def test_local_axes_work_after_camera_rotation(self):
        orientation = quaternion_from_axis_angle((0.0, 1.0, 0.0), 90.0)
        position = (0.0, 0.0, 0.0)
        samples = []
        for frame in range(1, 9):
            samples.append(sample(frame, position, orientation))
            position = add(
                position, quaternion_rotate(orientation, (0.0, 0.0, -0.5))
            )
        report = analyze_samples(samples, config=CONFIG)
        self.assertEqual(report.segments[0].motions[0], "dolly_in")

    def test_translation_directions(self):
        cases = (
            ((0.5, 0.0, 0.0), "truck_right"),
            ((-0.5, 0.0, 0.0), "truck_left"),
            ((0.0, 0.5, 0.0), "pedestal_up"),
            ((0.0, -0.5, 0.0), "pedestal_down"),
            ((0.0, 0.0, 0.5), "dolly_out"),
        )
        for delta, expected in cases:
            with self.subTest(expected=expected):
                position = (0.0, 0.0, 0.0)
                samples = []
                for frame in range(1, 9):
                    samples.append(sample(frame, position))
                    position = add(position, delta)
                report = analyze_samples(samples, config=CONFIG)
                self.assertEqual(report.segments[0].motions[0], expected)

    def test_rotation_directions(self):
        cases = (
            ((1.0, 0.0, 0.0), 2.0, "tilt_up"),
            ((1.0, 0.0, 0.0), -2.0, "tilt_down"),
            ((0.0, 1.0, 0.0), -2.0, "pan_right"),
            ((0.0, 0.0, 1.0), 2.0, "roll_left"),
            ((0.0, 0.0, 1.0), -2.0, "roll_right"),
        )
        for axis, degrees, expected in cases:
            with self.subTest(expected=expected):
                samples = [
                    sample(
                        frame,
                        (0.0, 0.0, 10.0),
                        quaternion_from_axis_angle(axis, (frame - 1) * degrees),
                    )
                    for frame in range(1, 9)
                ]
                report = analyze_samples(samples, config=CONFIG)
                self.assertEqual(report.segments[0].motions[0], expected)

    def test_rotation_spike_is_warned_and_ignored(self):
        samples = []
        for frame in range(1, 13):
            degrees = 60.0 if frame == 6 else 0.0
            samples.append(
                sample(
                    frame,
                    (0.0, 0.0, 10.0 - frame * 0.25),
                    quaternion_from_axis_angle((0.0, 1.0, 0.0), degrees),
                )
            )
        report = analyze_samples(samples, config=CONFIG)
        self.assertTrue(any(item.kind == "rotation_spike" for item in report.anomalies))
        self.assertTrue(
            all("pan" not in motion for segment in report.segments for motion in segment.motions)
        )

    def test_focal_spike_is_warned_and_ignored(self):
        samples = [
            sample(
                frame,
                (0.0, 0.0, 10.0),
                focal=100.0 if frame == 6 else 35.0,
            )
            for frame in range(1, 13)
        ]
        report = analyze_samples(samples, config=CONFIG)
        self.assertTrue(any(item.kind == "focal_spike" for item in report.anomalies))
        self.assertEqual(report.segments[0].motions, ("static",))


if __name__ == "__main__":
    unittest.main()
