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

    def test_scene_features_cache_matches_direct_detection(self):
        from mbv.vision import SceneFeatures

        rng = np.random.default_rng(23)
        template_image = rng.integers(0, 256, (16, 12, 3), dtype=np.uint8)
        scene = np.zeros((120, 200, 3), dtype=np.uint8)
        scene[40:56, 60:72] = template_image
        template = bot.Template("cache.png", template_image)

        direct, direct_score, _ = bot.find_detections(scene, [template], 0.9, 1.0, structure_weight=0.4)
        shared = SceneFeatures(scene)
        first, first_score, _ = bot.find_detections(shared, [template], 0.9, 1.0, structure_weight=0.4)
        second, second_score, _ = bot.find_detections(shared, [template], 0.9, 1.0, structure_weight=0.4)
        self.assertEqual([item.box for item in direct], [item.box for item in first])
        self.assertEqual([item.box for item in first], [item.box for item in second])
        self.assertAlmostEqual(direct_score, first_score, places=6)
        self.assertAlmostEqual(first_score, second_score, places=6)
        self.assertTrue(direct)

        scaled_direct, scaled_score, _ = bot.find_detections(scene, [template], 0.7, 0.5, structure_weight=0.35)
        scaled_shared, shared_score, _ = bot.find_detections(shared, [template], 0.7, 0.5, structure_weight=0.35)
        self.assertEqual([item.box for item in scaled_direct], [item.box for item in scaled_shared])
        self.assertAlmostEqual(scaled_score, shared_score, places=6)

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
        attack_box = {"forward": 0.25, "back": 0.25, "up": 0.15, "down": 0.15}
        target = bot.choose_nearest_target(detections, player, 400, 240, attack_box, facing="right")
        self.assertIsNotNone(target)
        self.assertEqual(target.name, "near-right.png")

    def test_attack_box_follows_facing_and_player_center(self):
        player = (100, 100, 40, 80)
        forward = bot.Detection((200, 110, 30, 40), 0.9, "forward.png")
        behind = bot.Detection((40, 110, 30, 40), 0.9, "behind.png")
        attack_box = {"forward": 0.3, "back": 0.05, "up": 0.2, "down": 0.2}
        right = bot.choose_nearest_target([forward, behind], player, 400, 240, attack_box, facing="right")
        left = bot.choose_nearest_target([forward, behind], player, 400, 240, attack_box, facing="left")
        self.assertEqual(right.name, "forward.png")
        self.assertEqual(left.name, "behind.png")

    def test_target_selection_requires_player_position(self):
        detections = [bot.Detection((30, 40, 20, 20), 0.99, "monster.png")]
        attack_box = {"forward": 0.3, "back": 0.3, "up": 0.2, "down": 0.2}
        self.assertIsNone(bot.choose_nearest_target(detections, None, 200, 100, attack_box))

    def test_chase_selection_uses_boxed_height_without_horizontal_limit(self):
        player = (100, 100, 40, 80)
        detections = [
            bot.Detection((360, 110, 40, 40), 0.85, "far-same-band.png"),
            bot.Detection((170, 10, 40, 50), 0.99, "near-other-band.png"),
        ]
        attack_box = {"forward": 0.1, "back": 0.1, "up": 0.12, "down": 0.12}
        target = bot.choose_nearest_same_level_target(detections, player, 400, 240, attack_box)
        self.assertIsNotNone(target)
        self.assertEqual(target.name, "far-same-band.png")

    def test_in_range_target_takes_priority_over_chase_target(self):
        player = (100, 100, 40, 80)
        detections = [
            bot.Detection((180, 110, 40, 40), 0.85, "in-range.png"),
            bot.Detection((380, 110, 40, 40), 0.99, "far.png"),
        ]
        attack_box = {"forward": 0.25, "back": 0.05, "up": 0.15, "down": 0.15}
        attack = bot.choose_nearest_target(detections, player, 500, 240, attack_box, facing="right")
        chase = None if attack is not None else bot.choose_nearest_same_level_target(
            detections, player, 500, 240, attack_box
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

    def test_hue_ranges_wrap_around_red(self):
        wrapped = bot.hue_ranges(2, 120, 120)
        self.assertEqual(len(wrapped), 2)
        self.assertEqual(wrapped[0]["lower"][0], 0)
        self.assertEqual(wrapped[1]["upper"][0], 179)
        mid = bot.hue_ranges(90, 120, 120)
        self.assertEqual(len(mid), 1)
        self.assertEqual(mid[0]["lower"][0], 81)
        self.assertEqual(mid[0]["upper"][0], 99)

    def test_frozen_selection_displays_and_crops_the_same_captured_frame(self):
        frame = np.arange(8 * 10 * 3, dtype=np.uint8).reshape((8, 10, 3))
        window = bot.WindowInfo(123, "MapleStory", 10, 20, 10, 8)
        result = type("Result", (), {"cancelled": False, "rectangle": (2, 1, 4, 4)})()

        with (
            patch("mbv.calibrate.focus_game_window") as focus,
            patch("mbv.calibrate.mss.MSS") as mss_factory,
            patch("mbv.calibrate.capture_client", return_value=frame) as capture,
            patch("mbv.calibrate.interactive_overlay", return_value=result) as overlay,
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
            patch("mbv.calibrate.focus_game_window"),
            patch("mbv.calibrate.mss.MSS"),
            patch("mbv.calibrate.capture_client", return_value=frame),
            patch("mbv.calibrate.interactive_overlay", return_value=result),
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

    def test_input_delivery_aliases(self):
        self.assertEqual(bot.input_delivery({}), "foreground")
        self.assertEqual(bot.input_delivery({"input": {"delivery": "background"}}), "background")
        self.assertEqual(bot.input_delivery({"input": {"delivery": "SendInput"}}), "foreground")
        with self.assertRaises(ValueError):
            bot.input_delivery({"input": {"delivery": "inject"}})

    def test_key_lparam_marks_extended_arrow_and_keyup(self):
        vk = bot.vk_for("left")
        down = bot.key_lparam(vk, False)
        up = bot.key_lparam(vk, True, was_down=True)
        self.assertTrue(down & (1 << 24))
        self.assertFalse(down & (1 << 31))
        self.assertFalse(down & (1 << 30))
        self.assertTrue(up & (1 << 24))
        self.assertTrue(up & (1 << 30))
        self.assertTrue(up & (1 << 31))

    def test_background_keyboard_posts_to_bound_hwnd(self):
        posted = []
        original_post = bot.user32.PostMessageW
        original_is_window = bot.user32.IsWindow
        original_fg = bot.user32.GetForegroundWindow
        original_timeout = bot.user32.SendMessageTimeoutW
        original_send = bot.user32.SendInput

        def fake_post(hwnd, message, wparam, lparam):
            posted.append((int(hwnd), int(message), int(wparam), int(lparam)))
            return True

        bot.user32.PostMessageW = fake_post
        bot.user32.IsWindow = lambda _hwnd: True
        bot.user32.GetForegroundWindow = lambda: 99999
        bot.user32.SendMessageTimeoutW = lambda *_args: 1
        bot.user32.SendInput = lambda *_args: 1
        try:
            keyboard = bot.Keyboard("background")
            keyboard.root_hwnd = 12345
            keyboard.hwnd = 12345
            keyboard.tap("z", 0.01)
        finally:
            bot.user32.PostMessageW = original_post
            bot.user32.IsWindow = original_is_window
            bot.user32.GetForegroundWindow = original_fg
            bot.user32.SendMessageTimeoutW = original_timeout
            bot.user32.SendInput = original_send

        messages = [item[1] for item in posted]
        self.assertGreaterEqual(len(posted), 2)
        self.assertEqual(posted[0][:3], (12345, bot.WM_KEYDOWN, bot.vk_for("z")))
        self.assertEqual(posted[-1][:3], (12345, bot.WM_KEYUP, bot.vk_for("z")))
        self.assertIn(bot.WM_CHAR, messages)
        self.assertFalse(posted[0][3] & (1 << 31))
        self.assertTrue(posted[-1][3] & (1 << 31))

    def test_background_keyboard_uses_sendinput_when_game_is_foreground(self):
        sent = []
        original_send = bot.user32.SendInput
        original_fg = bot.user32.GetForegroundWindow
        original_ancestor = bot.user32.GetAncestor
        original_is_window = bot.user32.IsWindow

        def fake_send(_count, _ptr, _size):
            sent.append(1)
            return 1

        bot.user32.SendInput = fake_send
        bot.user32.GetForegroundWindow = lambda: 12345
        bot.user32.GetAncestor = lambda hwnd, _flags=2: hwnd
        bot.user32.IsWindow = lambda _hwnd: True
        try:
            keyboard = bot.Keyboard("background")
            keyboard.root_hwnd = 12345
            keyboard.hwnd = 12345
            keyboard.tap("shift", 0.01)
        finally:
            bot.user32.SendInput = original_send
            bot.user32.GetForegroundWindow = original_fg
            bot.user32.GetAncestor = original_ancestor
            bot.user32.IsWindow = original_is_window

        self.assertEqual(len(sent), 2)

    def test_background_keyboard_keeps_sendinput_when_game_is_unfocused(self):
        sent = []
        original_send = bot.user32.SendInput
        original_fg = bot.user32.GetForegroundWindow
        original_post = bot.user32.PostMessageW
        original_timeout = bot.user32.SendMessageTimeoutW
        original_is_window = bot.user32.IsWindow

        def fake_send(_count, _ptr, _size):
            sent.append(1)
            return 1

        bot.user32.SendInput = fake_send
        bot.user32.GetForegroundWindow = lambda: 99999
        bot.user32.PostMessageW = lambda *_args: True
        bot.user32.SendMessageTimeoutW = lambda *_args: 1
        bot.user32.IsWindow = lambda _hwnd: True
        try:
            keyboard = bot.Keyboard("background")
            keyboard.root_hwnd = 12345
            keyboard.hwnd = 12345
            keyboard.tap("shift", 0.01)
        finally:
            bot.user32.SendInput = original_send
            bot.user32.GetForegroundWindow = original_fg
            bot.user32.PostMessageW = original_post
            bot.user32.SendMessageTimeoutW = original_timeout
            bot.user32.IsWindow = original_is_window

        self.assertEqual(len(sent), 2)

    def test_template_counts_are_non_negative(self):
        counts = bot.template_counts()
        self.assertEqual(set(counts), {"monster", "player", "head", "title"})
        for value in counts.values():
            self.assertGreaterEqual(value, 0)

    def test_apply_config_switches_delivery_without_sendinput(self):
        instance = bot.BowmanBot.__new__(bot.BowmanBot)
        instance.armed = False
        instance.delivery = "foreground"
        instance.background_input = False
        instance.keyboard = bot.Keyboard("foreground")
        instance.keyboard.hwnd = 0
        instance.config_lock = __import__("threading").Lock()
        config = json.loads(json.dumps(self.config))
        config.setdefault("input", {})["delivery"] = "background"
        instance.apply_config(config)
        self.assertEqual(instance.delivery, "background")
        self.assertTrue(instance.background_input)
        self.assertEqual(instance.keyboard.delivery, "background")

    def test_name_for_vk_roundtrip(self):
        for name in ("shift", "home", "end", "left", "a", "1"):
            self.assertEqual(bot.name_for_vk(bot.vk_for(name)), name)
        for name in bot.VK:
            self.assertEqual(bot.name_for_vk(bot.vk_for(name)), name)
        with self.assertRaises(ValueError):
            bot.name_for_vk(0x00)

    def test_attack_box_from_rectangle_keeps_asymmetric_extents(self):
        combat = (10, 20, 400, 300)
        player = (100, 100, 40, 80)
        # 角色中心在客户区 (130, 160)；框在身后 20、身前 180、上 40、下 30。
        rectangle = (110, 120, 200, 70)
        box = bot.attack_box_from_rectangle(rectangle, combat, player, facing="right")
        self.assertEqual(box["back"], round(20 / 400, 6))
        self.assertEqual(box["forward"], round(180 / 400, 6))
        self.assertEqual(box["up"], round(40 / 300, 6))
        self.assertEqual(box["down"], round(30 / 300, 6))

    def test_attack_box_from_rectangle_requires_player(self):
        with self.assertRaises(TypeError):
            bot.attack_box_from_rectangle((80, 90, 200, 80), (10, 20, 400, 300), None)

    def test_boxed_attack_range_feeds_choose_nearest_target(self):
        combat = (0, 0, 400, 240)
        player = (100, 100, 40, 80)
        rectangle = (20, 70, 200, 120)
        attack_box = bot.attack_box_from_rectangle(rectangle, combat, player, facing="right")
        in_range = bot.Detection((200, 120, 30, 20), 0.9, "in.png")
        far = bot.Detection((350, 120, 30, 20), 0.9, "far.png")
        target = bot.choose_nearest_target(
            [in_range, far], player, 400, 240, attack_box, facing="right"
        )
        self.assertIsNotNone(target)
        self.assertEqual(target.name, "in.png")
        self.assertIsNone(
            bot.choose_nearest_target([far], player, 400, 240, attack_box, facing="right")
        )

    def test_overlay_maps_win32_and_keysym_to_bind_names(self):
        import game_overlay as overlay

        vk_map = dict(bot.VK)
        self.assertEqual(overlay._vk_name_from_code(vk_map, bot.vk_for("shift")), "shift")
        self.assertEqual(overlay._vk_name_from_code(vk_map, bot.vk_for("home")), "home")
        self.assertEqual(overlay._vk_name_from_keysym(vk_map, "Shift_L"), "shift")
        self.assertEqual(overlay._vk_name_from_keysym(vk_map, "Left"), "left")
        self.assertEqual(overlay._vk_name_from_keysym(vk_map, "Escape"), "esc")
        self.assertEqual(overlay._vk_name_from_keysym(vk_map, "F7"), "f7")
        self.assertIsNone(overlay._vk_name_from_keysym(vk_map, "F1"))

    def test_overlay_draw_plan_hides_calibration_and_keeps_hint(self):
        import game_overlay as overlay

        visible = overlay.overlay_draw_plan(
            {"show_calibration": True, "banner": "运行中", "notice": "已暂停"}
        )
        self.assertTrue(visible["show_calibration"])
        self.assertEqual(visible["banner"], "运行中")
        self.assertEqual(visible["hint"], "")
        self.assertEqual(visible["notice"], "已暂停")

        hidden = overlay.overlay_draw_plan(
            {"show_calibration": False, "banner": "运行中", "notice": "已暂停"}
        )
        self.assertFalse(hidden["show_calibration"])
        self.assertEqual(hidden["banner"], "")
        self.assertEqual(hidden["hint"], overlay.CALIBRATION_HINT)
        self.assertEqual(hidden["notice"], "已暂停")
        self.assertTrue(overlay.overlay_draw_plan({})["show_calibration"])

    def test_toggle_calibration_overlay_is_independent_of_hide(self):
        instance = bot.BowmanBot.__new__(bot.BowmanBot)
        instance.calibration_overlay_visible = True
        instance.log = type("Log", (), {"write": lambda *_args, **_kwargs: None})()
        instance.toggle_calibration_overlay()
        self.assertFalse(instance.calibration_overlay_visible)
        instance.toggle_calibration_overlay()
        self.assertTrue(instance.calibration_overlay_visible)
        self.assertEqual(bot.vk_for("f7"), 0x76)
        self.assertEqual(bot.name_for_vk(0x76), "f7")


if __name__ == "__main__":
    unittest.main()
