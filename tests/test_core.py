import ctypes
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import maple_bowman as bot
import game_overlay as hud


class CoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with (ROOT / "config.json").open("r", encoding="utf-8") as handle:
            cls.config = json.load(handle)

    def test_win32_input_structure_size(self):
        expected = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
        self.assertEqual(ctypes.sizeof(bot.INPUT), expected)

    def test_keyboard_scan_codes_exist(self):
        self.assertGreater(int(bot.user32.MapVirtualKeyW(bot.vk_for("shift"), 0)), 0)
        self.assertGreater(int(bot.user32.MapVirtualKeyW(bot.vk_for("left"), 0)), 0)

    def test_current_process_integrity_is_readable(self):
        pid = int(ctypes.windll.kernel32.GetCurrentProcessId())
        self.assertGreater(bot.process_integrity_level(pid), 0)

    def test_roi_conversion(self):
        roi = {"x": 0.1, "y": 0.2, "w": 0.5, "h": 0.4}
        self.assertEqual(bot.roi_pixels((100, 200, 3), roi), (20, 20, 100, 40))

    def test_red_bar_fill(self):
        image = np.zeros((12, 100, 3), dtype=np.uint8)
        image[:, :50] = (0, 0, 255)
        ratio = bot.bar_fill(image, self.config["vision"]["hp_hsv_ranges"])
        self.assertAlmostEqual(ratio, 0.5, delta=0.03)

    def test_player_marker(self):
        image = np.zeros((100, 100, 3), dtype=np.uint8)
        cv2.rectangle(image, (20, 40), (25, 45), (0, 255, 0), -1)
        fixed_green_range = [{"lower": [35, 80, 80], "upper": [85, 255, 255]}]
        marker, _mask = bot.player_marker(
            image,
            fixed_green_range,
            2,
            180,
            None,
        )
        self.assertIsNotNone(marker)
        self.assertAlmostEqual(marker[0], 0.225, delta=0.03)
        self.assertAlmostEqual(marker[1], 0.425, delta=0.03)

    def test_template_match(self):
        rng = np.random.default_rng(7)
        template_image = rng.integers(0, 256, (12, 10, 3), dtype=np.uint8)
        scene = np.zeros((80, 120, 3), dtype=np.uint8)
        scene[31:43, 47:57] = template_image
        template = bot.Template("test.png", template_image)
        box, score, name = bot.find_monster(scene, [template], 0.95)
        self.assertEqual(box, (47, 31, 10, 12))
        self.assertGreaterEqual(score, 0.99)
        self.assertEqual(name, "test.png")

    def test_multiple_template_detections(self):
        rng = np.random.default_rng(11)
        template_image = rng.integers(0, 256, (14, 12, 3), dtype=np.uint8)
        scene = np.zeros((90, 150, 3), dtype=np.uint8)
        scene[20:34, 25:37] = template_image
        scene[48:62, 101:113] = template_image
        template = bot.Template("multi.png", template_image, np.full((14, 12), 255, dtype=np.uint8))
        detections, _score, _name = bot.find_detections(scene, [template], 0.98)
        boxes = {item.box for item in detections}
        self.assertIn((25, 20, 12, 14), boxes)
        self.assertIn((101, 48, 12, 14), boxes)

    def test_target_selection_prefers_nearest_in_range_on_same_level(self):
        player = (100, 100, 40, 80)
        detections = [
            bot.Detection((30, 100, 30, 80), 0.99, "left.png"),
            bot.Detection((150, 100, 30, 80), 0.90, "near-right.png"),
            bot.Detection((300, 100, 30, 80), 0.99, "far.png"),
            bot.Detection((115, 0, 30, 40), 0.99, "other-level.png"),
        ]
        target = bot.choose_nearest_target(detections, player, 400, 240, 0.25, 0.15)
        self.assertIsNotNone(target)
        self.assertEqual(target.name, "near-right.png")

    def test_nameplate_top_is_used_as_player_feet_anchor(self):
        nameplate = (100, 180, 80, 30)
        same_platform = bot.Detection((180, 100, 40, 80), 0.9, "same-platform.png")
        target = bot.choose_nearest_target(
            [same_platform],
            nameplate,
            400,
            300,
            0.3,
            0.05,
            player_feet_at_top=True,
        )
        self.assertIsNotNone(target)
        self.assertEqual(target.name, "same-platform.png")

    def test_target_selection_requires_player_position(self):
        detections = [bot.Detection((30, 40, 20, 20), 0.99, "monster.png")]
        self.assertIsNone(bot.choose_nearest_target(detections, None, 200, 100, 0.3, 0.2))

    def test_chase_selection_ignores_range_but_requires_same_level(self):
        nameplate = (100, 180, 80, 30)
        detections = [
            bot.Detection((360, 100, 40, 80), 0.85, "far-same-level.png"),
            bot.Detection((170, 10, 40, 50), 0.99, "near-other-level.png"),
        ]
        target = bot.choose_nearest_same_level_target(
            detections,
            nameplate,
            300,
            0.05,
            player_feet_at_top=True,
        )
        self.assertIsNotNone(target)
        self.assertEqual(target.name, "far-same-level.png")

    def test_in_range_target_takes_priority_over_chase_target(self):
        nameplate = (100, 180, 80, 30)
        detections = [
            bot.Detection((230, 100, 40, 80), 0.85, "in-range.png"),
            bot.Detection((380, 100, 40, 80), 0.99, "far.png"),
        ]
        attack = bot.choose_nearest_target(
            detections, nameplate, 500, 300, 0.312, 0.05, player_feet_at_top=True
        )
        chase = None if attack is not None else bot.choose_nearest_same_level_target(
            detections, nameplate, 300, 0.05, player_feet_at_top=True
        )
        self.assertEqual(attack.name, "in-range.png")
        self.assertIsNone(chase)

    def test_player_anchor_falls_back_from_nameplate_to_head_and_title(self):
        nameplate = bot.Detection((200, 300, 80, 30), 0.8, "nameplate.png")
        head = bot.Detection((208, 220, 64, 64), 0.9, "head.png")
        title = bot.Detection((208, 335, 64, 25), 0.85, "title.png")
        primary = bot.choose_fused_player_anchor(
            [("姓名板", [nameplate]), ("头部", [head]), ("称号勋章", [title])],
            None,
            800,
            500,
            0.032,
            0.07,
            0.18,
        )
        self.assertEqual(primary.source, "姓名板")
        from_head = bot.choose_fused_player_anchor(
            [("姓名板", []), ("头部", [head]), ("称号勋章", [title])],
            primary,
            800,
            500,
            0.032,
            0.07,
            0.18,
        )
        self.assertEqual(from_head.source, "头部")
        from_title = bot.choose_fused_player_anchor(
            [("姓名板", []), ("头部", []), ("称号勋章", [title])],
            from_head,
            800,
            500,
            0.032,
            0.07,
            0.18,
        )
        self.assertEqual(from_title.source, "称号勋章")
        self.assertLess(abs(primary.box[1] - from_head.box[1]), 20)
        self.assertLess(abs(primary.box[1] - from_title.box[1]), 20)

    def test_player_anchor_rejects_far_other_player(self):
        previous = bot.PlayerAnchor((100, 200, 80, 1), 0.8, "姓名板", (100, 200, 80, 30))
        other = bot.Detection((600, 200, 80, 30), 0.99, "other-nameplate.png")
        anchor = bot.choose_fused_player_anchor(
            [("姓名板", [other])], previous, 800, 500, 0.07, 0.076, 0.18
        )
        self.assertIsNone(anchor)

    def test_two_aux_sources_outvote_wrong_nameplate(self):
        wrong_nameplate = bot.Detection((420, 200, 80, 30), 0.99, "other-player.png")
        own_head = bot.Detection((100, 100, 64, 64), 0.82, "own-head.png")
        own_title = bot.Detection((100, 235, 64, 25), 0.86, "own-title.png")
        anchor = bot.choose_fused_player_anchor(
            [
                ("姓名板", [wrong_nameplate]),
                ("头部", [own_head]),
                ("称号勋章", [own_title]),
            ],
            None,
            800,
            500,
            0.07,
            0.07,
            0.8,
            0.07,
        )
        self.assertEqual(anchor.source, "头部")
        self.assertLess(anchor.box[0], 200)

    def test_chinese_text_rendering(self):
        image = np.zeros((60, 260, 3), dtype=np.uint8)
        rendered = bot.draw_chinese_texts(image, [("中文提示", (5, 5), (0, 255, 255), 20)])
        self.assertEqual(rendered.shape, image.shape)
        self.assertGreater(int(rendered.sum()), 0)

    def test_frozen_selection_displays_and_crops_the_same_captured_frame(self):
        frame = np.arange(8 * 10 * 3, dtype=np.uint8).reshape((8, 10, 3))
        window = bot.WindowInfo(123, "MapleStory", 10, 20, 10, 8)
        result = type("Result", (), {"cancelled": False, "rectangle": (2, 1, 4, 4)})()

        with (
            patch.object(bot, "focus_game_window") as focus,
            patch.object(bot.mss, "MSS") as mss_factory,
            patch.object(bot, "capture_client", return_value=frame) as capture,
            patch.object(bot, "interactive_overlay", return_value=result) as overlay,
        ):
            frozen = bot.capture_frozen_selection(window, "框选目标", "已取消")

        focus.assert_called_once_with(window)
        capture.assert_called_once_with(mss_factory.return_value.__enter__.return_value, window)
        self.assertIs(overlay.call_args.kwargs["frozen_frame"], frame)
        self.assertTrue(np.array_equal(frozen, frame[1:5, 2:6]))

    def test_frozen_selection_cancel_does_not_return_pixels(self):
        frame = np.zeros((6, 8, 3), dtype=np.uint8)
        window = bot.WindowInfo(123, "MapleStory", 10, 20, 8, 6)
        result = type("Result", (), {"cancelled": True, "rectangle": None})()

        with (
            patch.object(bot, "focus_game_window"),
            patch.object(bot.mss, "MSS"),
            patch.object(bot, "capture_client", return_value=frame),
            patch.object(bot, "interactive_overlay", return_value=result),
            self.assertRaisesRegex(RuntimeError, "已取消怪物模板框选"),
        ):
            bot.capture_frozen_selection(window, "框选目标", "已取消怪物模板框选")

    def test_frozen_frame_converts_bgr_pixels_for_display(self):
        frame = np.zeros((2, 3, 3), dtype=np.uint8)
        frame[0, 0] = (255, 0, 0)
        frame[0, 1] = (0, 0, 255)

        image = hud.frozen_frame_image(frame, 3, 2)

        self.assertEqual((3, 2), image.size)
        self.assertEqual((0, 0, 255), image.getpixel((0, 0)))
        self.assertEqual((255, 0, 0), image.getpixel((1, 0)))

    def test_zero_runtime_limit_is_unlimited(self):
        self.assertFalse(bot.runtime_limit_reached(100.0, 100000.0, 0))

    def test_negative_runtime_limit_is_unlimited(self):
        self.assertFalse(bot.runtime_limit_reached(100.0, 100000.0, -1))

    def test_positive_runtime_limit_expires_at_boundary(self):
        self.assertEqual(
            [False, True],
            [
                bot.runtime_limit_reached(100.0, 159.999, 1),
                bot.runtime_limit_reached(100.0, 160.0, 1),
            ],
        )


if __name__ == "__main__":
    unittest.main()
