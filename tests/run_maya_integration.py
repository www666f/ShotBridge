"""Maya standalone integration test for the saved validation scene."""

from __future__ import annotations

import os
import sys
import unittest
import tempfile

import maya.cmds as cmds
import maya.standalone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from camera_motion_describer.maya_adapter import (
    analyze_maya_camera,
    weighted_target_center,
    sample_camera,
)
from camera_motion_describer.demo_scene import build_demo_scene
from camera_motion_describer.models import AnalysisConfig


class MayaIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="sb_camera_test_")
        cls.addClassCleanup(cls.temporary.cleanup)
        # 用户可能编辑过 examples 场景；自动验收在独立进程中创建已知样本。
        build_demo_scene(os.path.join(cls.temporary.name, "known_camera.ma"))

    def test_known_camera_produces_expected_description(self):
        report = analyze_maya_camera(
            "SB_TestCamera",
            targets=("SB_Target_A", "SB_Target_B"),
            start_frame=1,
            end_frame=120,
        )
        all_motions = {
            motion for segment in report.segments for motion in segment.motions
        }
        self.assertIn("static", all_motions)
        self.assertIn("dolly_in", all_motions)
        self.assertIn("truck_right", all_motions)
        self.assertIn("pan_left", all_motions)
        self.assertIn("dolly_out", all_motions)
        self.assertIn("zoom_in", all_motions)
        self.assertTrue(any(item.kind == "position_spike" for item in report.anomalies))
        self.assertTrue(any("轴心明显偏离" in warning for warning in report.warnings))
        self.assertIn("向前推进", report.prompt)
        self.assertIn("相对被摄主体向右环绕", report.prompt)
        self.assertIn("被摄主体位于画面中央", report.prompt)
        self.assertTrue(any(
            segment.target_relation.get("framing_events")
            for segment in report.segments
        ))
        self.assertIn("光圈保持在 f/2.8", report.prompt)
        self.assertIn("【", report.prompt)
        self.assertTrue(any(segment.target_relation for segment in report.segments))
        print("\n--- CAMERA DESCRIPTION ---\n" + report.prompt)

    def test_multiple_targets_use_equal_weighted_visual_center(self):
        center = weighted_target_center(("SB_Target_A", "SB_Target_B"))
        self.assertAlmostEqual(center[0], 0.0, places=5)
        self.assertAlmostEqual(center[1], 1.0, places=5)
        self.assertAlmostEqual(center[2], 0.0, places=5)

        weighted = weighted_target_center(
            ("SB_Target_A", "SB_Target_B"),
            {"SB_Target_A": 3.0, "SB_Target_B": 1.0},
        )
        self.assertAlmostEqual(weighted[0], -0.75, places=5)

    def test_moving_subject_group_and_tolerance(self):
        # 本测试只创建带独立名称的节点，结束后删除，不污染其余样本。
        camera, shape = cmds.camera(name="SB_MovingSubjectCamera")
        a = cmds.polyCube(name="SB_MovingSubjectA")[0]
        b = cmds.polyCube(name="SB_MovingSubjectB")[0]
        self.addCleanup(cmds.delete, [camera, a, b])
        cmds.setAttr(camera + ".translateZ", 10)
        for node, offset in ((a, -1), (b, 1)):
            for frame, x in ((1, 0), (7, -4), (13, 0)):
                cmds.setKeyframe(node, attribute="translateX", time=frame, value=x+offset)
            cmds.keyTangent(node, inTangentType="linear", outTangentType="linear")
        before_time = cmds.currentTime(query=True)
        samples, _, _ = sample_camera(camera, (a, b), 1, 13)
        self.assertAlmostEqual(samples[0].target_center[0], 0)
        self.assertAlmostEqual(samples[6].target_center[0], -4)
        self.assertAlmostEqual(samples[-1].target_center[0], 0)
        self.assertEqual(cmds.currentTime(query=True), before_time)
        report = analyze_maya_camera(camera, (a, b), 1, 13)
        self.assertIn("主体整体中心向左移动", report.prompt)
        self.assertIn("主体整体中心向右移动", report.prompt)
        self.assertTrue(all(s.motions == ("static",) for s in report.segments))

        for node in (a, b):
            for frame, y in ((1, 0), (5, 0), (6, 10), (7, 0), (13, 0)):
                cmds.setKeyframe(node, attribute="translateY", time=frame, value=y)
            cmds.keyTangent(node, inTangentType="linear", outTangentType="linear")
        filtered = analyze_maya_camera(camera, (a, b), 1, 13)
        retained = analyze_maya_camera(camera, (a, b), 1, 13,
                                       config=AnalysisConfig(filter_spikes=False))
        self.assertTrue(any(a.kind == "target_position_spike" for a in filtered.anomalies))
        self.assertIn("之外", retained.prompt)
        cmds.currentTime(6)
        self.assertAlmostEqual(cmds.getAttr(a + ".translateY"), 10)
        cmds.currentTime(before_time)


if __name__ == "__main__":
    maya.standalone.initialize(name="python")
    try:
        result = unittest.main(exit=False, verbosity=2).result
    finally:
        maya.standalone.uninitialize()
    sys.exit(0 if result.wasSuccessful() else 1)
