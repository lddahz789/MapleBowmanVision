from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import cv2
import numpy as np

from mbv.bot import BowmanBot
from mbv.player_tracking import HeadRecoveryWindow, PlayerTrackState
from mbv.vision import Detection, PlayerAnchor, SceneFeatures, Template, find_detections


class HeadRecoveryWindowTests(unittest.TestCase):
    def setUp(self):
        self.window = HeadRecoveryWindow()
        self.background = np.random.default_rng(7).integers(0, 255, (96, 320), np.uint8)
        self.anchor = PlayerAnchor((400, 200, 40, 1), .8, "头部", (400, 145, 40, 30))

    def observe(self, frame, candidate=True, **overrides):
        args = dict(now=10 + frame * .15, frame=frame, marker=(.5, .5),
                    size=(1000, 400, 100, 80), background=self.background, eligible=True)
        args.update(overrides)
        return self.window.observe(self.anchor if candidate else None, **args)

    def test_one_gap_keeps_three_actual_hits_and_returns_only_current_frame(self):
        self.assertIsNone(self.observe(1))
        self.assertIsNone(self.observe(2, False))
        self.assertEqual(self.window.hits, 1)
        self.assertIsNone(self.observe(3))
        self.assertIs(self.observe(4), self.anchor)

    def test_second_gap_clears_window(self):
        for i, hit in enumerate((True, False, True, False), 1):
            self.assertIsNone(self.observe(i, hit))
        self.assertEqual(self.window.hits, 0)
        self.assertEqual(self.window.reason, "gap_limit")

    def test_repeated_frame_cannot_increment_confirmation(self):
        self.observe(1)
        self.observe(1)
        self.assertEqual(self.window.hits, 1)

    def test_motion_size_background_timeout_or_ambiguity_clears(self):
        for override in (
            {"marker": (.51, .5)}, {"size": (1000, 400, 101, 80)},
            {"background": 255 - self.background}, {"now": 12.0},
            {"eligible": False}, {"marker": None}, {"frame": 4},
        ):
            with self.subTest(override=list(override)):
                self.window.clear()
                self.observe(1)
                args = dict(override)
                frame = args.pop("frame", 2)
                self.assertIsNone(self.observe(frame, **args))
                self.assertEqual(self.window.hits, 0)

    def test_fixed_anchor_rejects_cumulative_drift(self):
        self.observe(1)
        self.anchor = PlayerAnchor((408, 200, 40, 1), .8, "头部", (408, 145, 40, 30))
        self.assertIsNone(self.observe(2))
        self.anchor = PlayerAnchor((416, 200, 40, 1), .8, "头部", (416, 145, 40, 30))
        self.assertIsNone(self.observe(3))
        self.assertEqual(self.window.reason, "head_position_changed")

    def test_title_and_flat_background_cannot_start(self):
        self.assertIsNone(self.observe(1, background=np.zeros_like(self.background)))
        self.assertEqual(self.window.hits, 0)
        self.anchor = PlayerAnchor(self.anchor.box, .99, "称号勋章", self.anchor.raw_box)
        self.assertIsNone(self.observe(2))
        self.assertEqual(self.window.hits, 0)

    def test_reset_and_pause_cancel_window(self):
        track = PlayerTrackState(head_recovery=self.window)
        self.observe(1)
        track.cancel_reacquisition()
        self.assertEqual(self.window.hits, 0)
        self.observe(2)
        track.reset()
        self.assertEqual(self.window.hits, 0)


class HeadRecoveryIntegrationTests(unittest.TestCase):
    def bot(self, established=True):
        bot = BowmanBot.__new__(BowmanBot)
        bot.log = MagicMock()
        bot.player_templates = []
        bot.player_track = PlayerTrackState(
            anchor=PlayerAnchor((400, 200, 40, 1), .9, "姓名板", (400, 200, 40, 16)),
            last_seen_at=10, last_identity_at=10 if established else 0,
            nameplate_identity_established=established,
        )
        bot._detect_player_nameplate = MagicMock(return_value=([], .1, None))
        bot._detect_player_auxiliary = MagicMock()
        return bot

    def test_bot_recovers_from_head_hit_gap_hit_hit_without_nameplate(self):
        for established in (True, False):
            bot = self.bot(established)
            pixels = np.random.default_rng(3).integers(0, 255, (400, 1000, 3), np.uint8)
            head = Detection((400, 145, 40, 30), .8, "head", anchor_offset=(.5, 55 / 30))
            results = []
            for index, hit in enumerate((True, False, True, True)):
                bot._detect_player_auxiliary.return_value = ([head] if hit else [], .8 if hit else .6, [], .1)
                result = bot._track_player(SceneFeatures(pixels), {}, 11 + index * .15,
                                          (.5, .5), marker_unambiguous=True, marker_size=(100, 80))
                results.append(result)
                if index < 3:
                    self.assertEqual(bot.player_track.last_seen_at, 10)
            self.assertEqual(results[:3], [None, None, None])
            if established:
                self.assertIsNotNone(results[-1])
                self.assertEqual(results[-1].source, "头部")
            else:
                self.assertIsNone(results[-1])

    def test_native_head_fallback_keeps_threshold_and_local_roi(self):
        bot = self.bot()
        bot.player_head_templates = [Template("head", np.zeros((30, 40, 3), np.uint8))]
        bot.player_title_templates = []
        # 调用真实方法，不使用辅助检测的 mock。
        del bot._detect_player_auxiliary
        head = Detection((400, 145, 40, 30), .8, "head")
        with patch("mbv.bot.find_detections", side_effect=[([], .6, None), ([head], .8, "head"), ([], -1, None)]) as scan:
            result = bot._detect_player_auxiliary(SceneFeatures(np.zeros((400, 1000, 3), np.uint8)), {}, None)
        self.assertEqual(result[0], [head])
        self.assertEqual(scan.call_args_list[1].args[2:4], (.74, 1.0))
        self.assertIsNotNone(scan.call_args_list[1].kwargs["search_roi"])


class AchromaticNameplateTests(unittest.TestCase):
    def test_black_white_glyph_recovers_without_changing_default_matcher(self):
        plate = np.full((24, 64, 3), 30, np.uint8)
        cv2.putText(plate, "AB", (15, 18), cv2.FONT_HERSHEY_SIMPLEX, .5, (255, 255, 255), 1)
        template = Template("name", plate, np.full((24, 64), 255, np.uint8))
        pixels = np.zeros((120, 180, 3), np.uint8)
        pixels[41:65, 41:105] = plate
        old = find_detections(pixels, [template], .7, structure_weight=.55)
        self.assertEqual(old[0], [])
        scene = SceneFeatures(pixels)
        ds, score, _ = find_detections(scene, [template], .7, structure_weight=.55,
                                      search_roi=(21, 21, 110, 80), achromatic_fallback=True)
        self.assertEqual(ds[0].box, (41, 41, 64, 24))
        self.assertGreater(score, .8)
        bot = BowmanBot.__new__(BowmanBot)
        bot.player_templates = [template]
        verified, _, _ = bot._detect_player_nameplate(scene, {}, None)
        self.assertTrue(any(d.identity_score >= .9 for d in verified))

    def test_colored_template_retains_opponent_scores(self):
        pixels = np.random.default_rng(5).integers(0, 255, (80, 120, 3), np.uint8)
        template = Template("color", pixels[20:40, 30:60].copy(), np.full((20, 30), 255, np.uint8))
        self.assertEqual(find_detections(pixels, [template], .7, structure_weight=.55),
                         find_detections(pixels, [template], .7, structure_weight=.55, achromatic_fallback=True))

    def test_blank_template_does_not_become_valid_by_fallback(self):
        template = Template("blank", np.zeros((20, 30, 3), np.uint8), np.full((20, 30), 255, np.uint8))
        result = find_detections(np.zeros((80, 120, 3), np.uint8), [template], .7,
                                 structure_weight=.55, achromatic_fallback=True)
        self.assertEqual(result[0], [])


if __name__ == "__main__":
    unittest.main()
