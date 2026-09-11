from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import cv2
import numpy as np

from mbv.bot import BowmanBot
from mbv.player_tracking import PlayerTrackState
from mbv.vision import Detection, SceneFeatures, Template, find_detections


class NameplateResolutionTests(unittest.TestCase):
    def setUp(self):
        plate = np.full((24, 64, 3), (180, 70, 20), dtype=np.uint8)
        cv2.putText(plate, "AB", (18, 17), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1)
        self.template = Template("own.png", plate, np.full((24, 64), 255, dtype=np.uint8))
        self.bot = BowmanBot.__new__(BowmanBot)
        self.bot.player_templates = [self.template]
        self.bot.player_track = PlayerTrackState()
        self.bot._detect_player_auxiliary = MagicMock(return_value=([], -1.0, [], -1.0))
        self.vision = {"player_template_threshold": 0.7, "player_detection_scale": 0.5}

    def scene(self, x=41, y=41):
        pixels = np.zeros((120, 180, 3), dtype=np.uint8)
        pixels[y:y + 24, x:x + 64] = self.template.image
        return SceneFeatures(pixels)

    def test_real_pixels_recover_all_four_sampling_phases(self):
        for x, y in ((40, 40), (41, 40), (40, 41), (41, 41)):
            with self.subTest(x=x, y=y):
                scene = self.scene(x, y)
                coarse, _, _ = find_detections(
                    scene, [self.template], 0.7, 0.5, structure_weight=0.55,
                )
                if (x, y) != (40, 40):
                    self.assertEqual(coarse, [])  # 旧半分辨率路径的无遮挡漏检。
                detections, score, _ = self.bot._detect_player_nameplate(scene, self.vision, None)
                self.assertGreater(score, 0.7)
                self.assertEqual(detections[0].box, (x, y, 64, 24))
                self.assertGreater(detections[0].identity_score, 0.9)

    def test_fallback_keeps_local_roi_and_global_coordinates(self):
        scene = self.scene()
        roi = (21, 21, 110, 80)
        with patch("mbv.bot.find_detections", wraps=find_detections) as scan:
            detections, _, _ = self.bot._detect_player_nameplate(scene, self.vision, roi)
        self.assertEqual(detections[0].box, (41, 41, 64, 24))
        self.assertEqual([c.args[3] for c in scan.call_args_list], [0.5, 1.0])
        for invocation in scan.call_args_list:
            self.assertIs(invocation.args[0], scene)
            self.assertEqual(invocation.kwargs["search_roi"], roi)
            self.assertFalse(invocation.kwargs["nms_across_templates"])
        outside, _, _ = self.bot._detect_player_nameplate(scene, self.vision, (110, 20, 70, 80))
        self.assertFalse(any(d.identity_score >= 0.5 for d in outside))

    def test_valid_coarse_identity_does_not_pay_for_full_resolution(self):
        with patch("mbv.bot.find_detections", wraps=find_detections) as scan:
            self.bot._detect_player_nameplate(self.scene(40, 40), self.vision, None)
        self.assertEqual(scan.call_count, 1)

    def test_full_resolution_configuration_does_not_scan_twice(self):
        self.vision["player_detection_scale"] = 1.0
        with patch("mbv.bot.find_detections", wraps=find_detections) as scan:
            self.bot._detect_player_nameplate(SceneFeatures(np.zeros((120, 180, 3), np.uint8)), self.vision, None)
        self.assertEqual(scan.call_count, 1)

    def test_no_templates_does_not_scan_twice(self):
        self.bot.player_templates = []
        with patch("mbv.bot.find_detections", wraps=find_detections) as scan:
            self.assertEqual(self.bot._detect_player_nameplate(self.scene(), self.vision, None), ([], -1.0, None))
        self.assertEqual(scan.call_count, 1)

    def test_invalid_identity_triggers_fallback_and_cannot_suppress_own_name(self):
        # 粗检原始分较高但字形错误，不能跳过复核或在去重时盖掉有效名字。
        invalid = Detection((41, 41, 64, 24), 0.99, "unknown.png")
        valid = Detection((41, 41, 64, 24), 0.85, "own.png")
        with patch("mbv.bot.find_detections", side_effect=[([invalid], 0.99, invalid.name), ([valid], 0.85, valid.name)]) as scan:
            detections, _, _ = self.bot._detect_player_nameplate(self.scene(), self.vision, None)
        self.assertEqual(scan.call_count, 2)
        self.assertEqual(len(detections), 1)
        self.assertEqual(detections[0].name, "own.png")
        self.assertGreater(detections[0].identity_score, 0.9)

    def test_full_resolution_does_not_relax_recovery_threshold(self):
        roi = (21, 21, 110, 80)
        with patch("mbv.bot.find_detections", return_value=([], -1.0, None)) as scan:
            self.bot._detect_player_nameplate(self.scene(), self.vision, roi, threshold=0.66)
        self.assertEqual([c.args[2] for c in scan.call_args_list], [0.66, 0.7])
        self.assertEqual([c.kwargs["search_roi"] for c in scan.call_args_list], [roi, roi])

    def test_real_fallback_requires_two_frames_after_minimap_hold_blocked(self):
        self.bot.player_track.minimap_stationary_blocked = True
        self.assertIsNone(self.bot._track_player(self.scene(), self.vision, 10.0))
        self.assertTrue(self.bot.player_track.minimap_stationary_blocked)
        anchor = self.bot._track_player(self.scene(), self.vision, 10.1)
        self.assertIsNotNone(anchor)
        self.assertEqual(anchor.source, "姓名板")
        self.assertEqual(anchor.raw_box, (41, 41, 64, 24))
        self.assertEqual(self.bot.player_track.last_seen_at, 10.1)
        self.assertFalse(self.bot.player_track.minimap_stationary_blocked)

    def test_ambiguous_full_resolution_candidates_do_not_establish_identity(self):
        pixels = self.scene().scene
        pixels[81:105, 111:175] = self.template.image
        for now in (10.0, 10.1, 10.2):
            anchor = self.bot._track_player(SceneFeatures(pixels), self.vision, now)
            self.assertIsNone(anchor)
        self.assertFalse(self.bot.player_track.has_confirmed_identity())


if __name__ == "__main__":
    unittest.main()
