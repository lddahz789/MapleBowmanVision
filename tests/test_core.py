import ctypes
import json
from pathlib import Path
import queue
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, call, patch

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import maple_bowman as bot
import game_overlay as hud


class CoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with (ROOT / "config.example.json").open("r", encoding="utf-8") as handle:
            cls.config = json.load(handle)

    def test_win32_input_structure_size(self):
        expected = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
        self.assertEqual(ctypes.sizeof(bot.INPUT), expected)

    def test_keyboard_scan_codes_exist(self):
        self.assertGreater(int(bot.user32.MapVirtualKeyW(bot.vk_for("shift"), 0)), 0)
        self.assertGreater(int(bot.user32.MapVirtualKeyW(bot.vk_for("left"), 0)), 0)

    def test_hotkey_monitor_registers_all_bindings(self):
        import threading
        from mbv import bot as runtime_bot

        instance = runtime_bot.BowmanBot.__new__(runtime_bot.BowmanBot)
        instance.hotkey_stop = threading.Event()
        instance.hotkey_thread_id = 0
        instance.log = type("Log", (), {"write": lambda _self, *_args, **_kwargs: None})()

        with (
            patch.object(runtime_bot.user32, "RegisterHotKey", return_value=True) as register,
            patch.object(runtime_bot.user32, "GetMessageW", return_value=0),
            patch.object(runtime_bot.user32, "UnregisterHotKey", return_value=True) as unregister,
        ):
            instance.monitor_hotkeys()

        self.assertEqual(register.call_count, 4)
        self.assertEqual(unregister.call_count, 4)
        self.assertEqual(instance.hotkey_thread_id, 0)

    def test_current_process_integrity_is_readable(self):
        pid = int(ctypes.windll.kernel32.GetCurrentProcessId())
        self.assertGreater(bot.process_integrity_level(pid), 0)

    def test_game_window_topmost_can_be_enabled_and_released(self):
        window = bot.WindowInfo(12345, "MapleStory", 10, 20, 800, 600)
        with patch("mbv.window.user32.SetWindowPos", return_value=True) as set_window_pos:
            bot.set_window_topmost(window, True)
            bot.set_window_topmost(window, False)

        self.assertEqual(set_window_pos.call_count, 2)
        self.assertEqual(set_window_pos.call_args_list[0].args[1].value, ctypes.c_void_p(-1).value)
        self.assertEqual(set_window_pos.call_args_list[1].args[1].value, ctypes.c_void_p(-2).value)
        for call in set_window_pos.call_args_list:
            self.assertEqual(call.args[2:6], (0, 0, 0, 0))

    def test_game_window_topmost_failure_is_visible(self):
        window = bot.WindowInfo(12345, "MapleStory", 10, 20, 800, 600)
        with (
            patch("mbv.window.user32.SetWindowPos", return_value=False),
            self.assertRaisesRegex(OSError, "无法置顶游戏窗口"),
        ):
            bot.set_window_topmost(window, True)

    def test_background_arm_can_skip_foreground_and_topmost(self):
        from mbv import bot as runtime_bot

        instance = runtime_bot.BowmanBot.__new__(runtime_bot.BowmanBot)
        instance.armed = False
        instance.input_authorized = True
        instance.config = {
            "calibrated": True,
            "calibration": {},
            "window": {"topmost_while_armed": False},
        }
        instance.strategy = MagicMock(display_name="测试策略", capture_fields=())
        instance.player_templates = [MagicMock()]
        instance.integrity_ok = True
        instance.background_input = True
        instance.delivery = "background"
        instance.keyboard = MagicMock(hwnd=12345)
        instance.templates = []
        instance.window_topmost = False
        instance.window = None
        instance.notify = MagicMock()
        instance.log = MagicMock()
        window = bot.WindowInfo(12345, "MapleStory", 10, 20, 800, 600)

        with (
            patch("mbv.bot.missing_recognition_data", return_value=[]),
            patch("mbv.bot.user32.IsWindow", return_value=True),
            patch("mbv.bot.user32.IsIconic", return_value=False),
            patch("mbv.bot.user32.GetForegroundWindow", return_value=99999),
            patch("mbv.bot.user32.MessageBeep"),
            patch("mbv.bot.client_window", return_value=window),
            patch("mbv.bot.focus_game_window") as focus_window,
            patch("mbv.bot.set_window_topmost") as set_topmost,
        ):
            instance._toggle(window)

        focus_window.assert_not_called()
        set_topmost.assert_not_called()
        self.assertTrue(instance.armed)
        self.assertFalse(instance.window_topmost)
        instance.log.write.assert_called_once_with(
            "arm",
            window="MapleStory",
            templates=0,
            delivery="background",
            input_hwnd=12345,
            window_topmost=False,
        )

    def test_control_panel_does_not_dock_move_or_resize_game_window(self):
        panel_source = (ROOT / "mbv" / "panel.py").read_text(encoding="utf-8")
        window_source = (ROOT / "mbv" / "window.py").read_text(encoding="utf-8")

        self.assertNotIn("GameWindowDock", panel_source)
        self.assertNotIn("_sync_game_stage", panel_source)
        self.assertNotIn("GameWindowDock", window_source)
        self.assertNotIn("SetWindowPlacement", window_source)

    def test_disarm_releases_game_window_topmost(self):
        instance = bot.BowmanBot.__new__(bot.BowmanBot)
        instance.armed = True
        instance.state = "SCANNING"
        instance.keyboard = MagicMock()
        instance.window = bot.WindowInfo(12345, "MapleStory", 10, 20, 800, 600)
        instance.window_topmost = True
        instance.log = MagicMock()
        instance.notify = MagicMock()

        with (
            patch("mbv.bot.user32.IsWindow", return_value=True),
            patch("mbv.bot.user32.MessageBeep"),
            patch("mbv.bot.set_window_topmost") as set_topmost,
        ):
            instance.disarm("测试暂停")

        instance.keyboard.release_all.assert_called_once_with()
        set_topmost.assert_called_once_with(instance.window, False)
        self.assertFalse(instance.window_topmost)
        self.assertFalse(instance.armed)

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

    def test_recursive_monster_template_loading_keeps_category_names(self):
        rng = np.random.default_rng(29)
        image = rng.integers(0, 256, (12, 10, 3), dtype=np.uint8)
        ok, encoded = cv2.imencode(".png", image)
        self.assertTrue(ok)
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            category = root / "绿水灵"
            category.mkdir()
            encoded.tofile(root / "legacy.png")
            encoded.tofile(category / "jump.png")

            recursive = bot.load_templates(root, recursive=True)
            shallow = bot.load_templates(root)

        self.assertEqual({item.name for item in recursive}, {"legacy.png", "绿水灵/jump.png"})
        self.assertEqual([item.name for item in shallow], ["legacy.png"])

    def test_monster_templates_only_keep_selected_category(self):
        image = np.zeros((8, 8, 3), dtype=np.uint8)
        mask = np.full((8, 8), 255, dtype=np.uint8)
        templates = [
            bot.Template("legacy.png", image, mask),
            bot.Template("绿水灵/a.png", image, mask),
            bot.Template("蓝蜗牛/b.png", image, mask),
        ]

        self.assertEqual(
            [item.name for item in bot.monster_templates_for_category(templates, "绿水灵")],
            ["绿水灵/a.png"],
        )
        self.assertEqual(
            [item.name for item in bot.monster_templates_for_category(templates, "")],
            ["legacy.png"],
        )

    def test_monster_filters_only_suppress_overlapping_same_category(self):
        detections = [
            bot.Detection((20, 20, 30, 30), 0.95, "绿水灵/a.png"),
            bot.Detection((20, 20, 30, 30), 0.94, "蓝蜗牛/b.png"),
            bot.Detection((120, 20, 30, 30), 0.93, "绿水灵/c.png"),
            bot.Detection((20, 90, 30, 30), 0.92, "legacy.png"),
        ]
        filters = [
            bot.Detection((16, 16, 38, 38), 0.9, "绿水灵/wall.png"),
            bot.Detection((18, 88, 36, 36), 0.9, "root-wall.png"),
        ]

        kept = bot.suppress_monster_detections(detections, filters, min_overlap=0.5)

        self.assertEqual([item.name for item in kept], ["蓝蜗牛/b.png", "绿水灵/c.png"])
        self.assertEqual(bot.monster_template_category("legacy.png"), "")
        self.assertEqual(bot.monster_template_category("绿水灵/jump.png"), "绿水灵")

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

    def test_player_auxiliary_detection_schedule_keeps_safe_fallbacks(self):
        from mbv.bot import player_anchor_within_hold, should_run_player_auxiliary_detections

        nameplate = bot.PlayerAnchor((100, 200, 80, 1), 0.9, "姓名板", (100, 200, 80, 30))
        head = bot.PlayerAnchor((100, 200, 64, 1), 0.9, "头部", (100, 120, 64, 64))

        self.assertTrue(should_run_player_auxiliary_detections(None, nameplate, 10.0, 9.8, 0.75))
        self.assertTrue(should_run_player_auxiliary_detections(nameplate, None, 10.0, 9.8, 0.75))
        self.assertTrue(should_run_player_auxiliary_detections(head, nameplate, 10.0, 9.8, 0.75))
        self.assertFalse(should_run_player_auxiliary_detections(nameplate, nameplate, 10.0, 9.8, 0.75))
        self.assertTrue(should_run_player_auxiliary_detections(nameplate, nameplate, 10.0, 9.25, 0.75))
        self.assertTrue(should_run_player_auxiliary_detections(nameplate, nameplate, 10.0, 0.0, 0.75))
        self.assertTrue(should_run_player_auxiliary_detections(nameplate, nameplate, 10.0, 9.8, 0.0))

        self.assertIs(nameplate, player_anchor_within_hold(nameplate, 9.25, 10.0, 0.8))
        self.assertIsNone(player_anchor_within_hold(nameplate, 9.19, 10.0, 0.8))

    def test_expired_player_anchor_allows_distant_reacquisition(self):
        from mbv.bot import player_anchor_within_hold

        previous = bot.PlayerAnchor((100, 200, 80, 1), 0.9, "姓名板", (100, 200, 80, 30))
        distant = bot.Detection((600, 200, 80, 30), 0.95, "distant.png")
        args = (800, 500, 0.07, 0.076, 0.18)

        fresh = player_anchor_within_hold(previous, 9.5, 10.0, 0.8)
        rejected = bot.choose_fused_player_anchor([("姓名板", [distant])], fresh, *args)
        self.assertIsNone(rejected)

        expired = player_anchor_within_hold(previous, 9.0, 10.0, 0.8)
        reacquired = bot.choose_fused_player_anchor([("姓名板", [distant])], expired, *args)
        self.assertIsNotNone(reacquired)
        self.assertGreater(reacquired.box[0], 500)

    def test_attack_anchor_height_is_stable_across_three_detection_sources(self):
        player = (100, 220, 40, 1)
        nameplate = (90, 230, 60, 15)
        head = (100, 100, 40, 40)
        title = (80, 300, 80, 20)
        anchors = [bot.player_attack_anchor(player, raw) for raw in (nameplate, head, title)]
        self.assertEqual(anchors, [(120.0, 220.0), (120.0, 220.0), (120.0, 220.0)])

    def test_attack_anchor_uses_light_smoothing_and_snaps_on_large_move(self):
        smoothed = bot.smooth_player_attack_anchor((100.0, 200.0), (108.0, 204.0), 0.25, 60.0)
        self.assertEqual(smoothed, (102.0, 201.0))
        snapped = bot.smooth_player_attack_anchor(smoothed, (220.0, 320.0), 0.25, 60.0)
        self.assertEqual(snapped, (220.0, 320.0))
        self.assertIsNone(bot.smooth_player_attack_anchor(snapped, None, 0.25, 60.0))

    def test_hue_ranges_wrap_around_red(self):
        wrapped = bot.hue_ranges(2, 120, 120)
        self.assertEqual(len(wrapped), 2)
        self.assertEqual(wrapped[0]["lower"][0], 0)
        self.assertEqual(wrapped[1]["upper"][0], 179)
        mid = bot.hue_ranges(90, 120, 120)
        self.assertEqual(len(mid), 1)
        self.assertEqual(mid[0]["lower"][0], 81)
        self.assertEqual(mid[0]["upper"][0], 99)

    def test_old_config_gets_safe_monster_filter_defaults(self):
        legacy = json.loads(json.dumps(self.config))
        legacy["vision"].pop("active_monster_category", None)
        legacy["vision"].pop("monster_filter_threshold", None)
        legacy["vision"].pop("monster_filter_overlap", None)
        legacy["vision"].pop("monster_structure_weight", None)
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps(legacy), encoding="utf-8")
            loaded = bot.load_config(path)
        self.assertEqual(loaded["vision"]["monster_filter_threshold"], 0.84)
        self.assertEqual(loaded["vision"]["monster_filter_overlap"], 0.5)
        self.assertEqual(loaded["vision"]["monster_structure_weight"], 0.15)
        self.assertEqual(loaded["vision"]["active_monster_category"], "")

    def test_old_combat_platform_center_is_invalidated_for_minimap_recapture(self):
        legacy = json.loads(json.dumps(self.config))
        legacy["recognition"].pop("platform_center_space", None)
        legacy["recognition"]["platform_center"] = {"x": 0.37, "y": 0.66}
        legacy["recognition"]["platform_center_captured"] = True
        legacy["calibration"]["items"]["platform_center"] = {
            "complete": True,
            "timestamp": "2026-08-14T21:57:00",
        }
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps(legacy), encoding="utf-8")
            loaded = bot.load_config(path)

        self.assertEqual(loaded["recognition"]["platform_center_space"], "minimap")
        self.assertFalse(loaded["recognition"]["platform_center_captured"])
        self.assertFalse(loaded["calibration"]["items"]["platform_center"]["complete"])
        self.assertEqual(
            loaded["calibration"]["items"]["platform_center"]["previous_timestamp"],
            "2026-08-14T21:57:00",
        )

    def test_old_config_migrates_once_to_bowman_dynamic_strategy(self):
        legacy = json.loads(json.dumps(self.config))
        legacy.pop("strategy", None)
        legacy["behavior"]["bow_attack_box"] = {
            "forward": 0.312,
            "back": 0.08,
            "up": 0.12,
            "down": 0.12,
        }
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps(legacy), encoding="utf-8")
            first = bot.load_config(path)
            bot.save_config(path, first)
            second = bot.load_config(path)
        first_box = first["targeting"]["box"]
        second_box = second["targeting"]["box"]
        self.assertEqual(first["strategy"]["active"], "bowman_dynamic")
        self.assertEqual(first_box, {"forward": 0.2808, "back": 0.072, "up": 0.144, "down": 0.144})
        self.assertEqual(second_box, first_box)
        self.assertNotIn("bow_attack_box", second["behavior"])

    def test_strategy_scoped_attack_box_migrates_to_common_targeting_without_resizing(self):
        transitional = json.loads(json.dumps(self.config))
        transitional.pop("targeting", None)
        transitional["strategy"]["options"]["bowman_dynamic"]["attack_box"] = {
            "forward": 0.33,
            "back": 0.11,
            "up": 0.22,
            "down": 0.07,
        }
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps(transitional), encoding="utf-8")
            loaded = bot.load_config(path)
        self.assertEqual(
            loaded["targeting"]["box"],
            {"forward": 0.33, "back": 0.11, "up": 0.22, "down": 0.07},
        )

    def test_strategy_registry_exposes_description_and_settings(self):
        from mbv.strategies import list_strategies, missing_recognition_data

        strategies = list_strategies()
        self.assertEqual(
            [item.display_name for item in strategies],
            ["弓箭手动态", "原地攻击", "标飞安全输出"],
        )
        self.assertTrue(all(item.description for item in strategies))
        bowman, stationary, throwing_star = strategies
        self.assertIn("platform_center", bowman.required_recognition_data)
        self.assertEqual(stationary.required_recognition_data, ())
        self.assertEqual(throwing_star.required_recognition_data, ())
        self.assertEqual(len(throwing_star.capture_fields), 1)
        self.assertEqual(missing_recognition_data(self.config, bowman), ("platform_center",))
        self.assertEqual(missing_recognition_data(self.config, stationary), ())
        self.assertEqual(missing_recognition_data(self.config, throwing_star), ())
        ready = json.loads(json.dumps(self.config))
        ready["recognition"]["platform_center_captured"] = True
        self.assertEqual(missing_recognition_data(ready, bowman), ())
        ready["recognition"]["throwing_star_safe_output_area_captured"] = True
        self.assertEqual(missing_recognition_data(ready, throwing_star), ())
        enabled_without_capture = json.loads(json.dumps(self.config))
        enabled_without_capture["strategy"]["options"]["throwing_star_safe"]["use_safe_output_area"] = True
        self.assertEqual(
            missing_recognition_data(enabled_without_capture, throwing_star),
            ("throwing_star_safe_output_area",),
        )

    def test_throwing_star_only_selects_targets_below_player(self):
        from mbv.strategies import get_strategy
        from mbv.strategies.base import TargetSelectionContext

        strategy = get_strategy("throwing_star_safe")
        same_level = bot.Detection((180, 90, 20, 20), 0.99, "same.png")
        below = bot.Detection((220, 145, 20, 20), 0.85, "below.png")
        selected = strategy.select_targets(
            TargetSelectionContext(
                detections=[same_level, below],
                player_box=(100, 100, 40, 1),
                player_raw_box=(100, 60, 40, 40),
                player_anchor=(120.0, 100.0),
                scene_width=400,
                scene_height=240,
                facing="right",
                target_area={"forward": 0.4, "back": 0.1, "up": 0.1, "down": 0.4},
                settings={"minimum_target_vertical_gap": 0.02},
            )
        )
        self.assertIs(selected.target, below)
        self.assertIsNone(selected.chase_target)

    def test_throwing_star_jumps_toward_safe_area_before_attacking(self):
        from mbv.strategies import get_strategy
        from mbv.strategies.base import StrategyActionContext

        strategy = get_strategy("throwing_star_safe")
        decision = strategy.decide(
            StrategyActionContext(
                marker=(0.2, 0.7),
                player_box=(180, 350, 40, 1),
                player_anchor=(200.0, 350.0),
                target_box=(250, 360, 20, 20),
                chase_box=None,
                combat_width=1000,
                combat_height=500,
                has_monster_candidates=True,
                now=10.0,
                last_target_seen=9.0,
                last_pickup=0.0,
                last_jump=0.0,
                direction="right",
                behavior=self.config["behavior"],
                settings={
                    "use_safe_output_area": True,
                    "jump_interval_seconds": 0.35,
                    "minimum_target_vertical_gap": 0.02,
                },
                recognition={
                    "throwing_star_safe_output_area": {"x": 0.4, "y": 0.2, "w": 0.2, "h": 0.1}
                },
            )
        )
        self.assertEqual((decision.action, decision.direction), ("jump", "right"))
        self.assertEqual(decision.state, "RETURN_SAFE_JUMP_RIGHT")

    def test_throwing_star_holds_direction_between_jump_taps(self):
        from mbv.strategies import get_strategy
        from mbv.strategies.base import StrategyActionContext

        strategy = get_strategy("throwing_star_safe")
        decision = strategy.decide(
            StrategyActionContext(
                marker=(0.2, 0.7),
                player_box=(180, 350, 40, 1),
                player_anchor=(200.0, 350.0),
                target_box=None,
                chase_box=None,
                combat_width=1000,
                combat_height=500,
                has_monster_candidates=False,
                now=10.0,
                last_target_seen=9.0,
                last_pickup=0.0,
                last_jump=9.8,
                direction="right",
                behavior=self.config["behavior"],
                settings={
                    "use_safe_output_area": True,
                    "jump_interval_seconds": 0.35,
                    "minimum_target_vertical_gap": 0.02,
                },
                recognition={
                    "throwing_star_safe_output_area": {"x": 0.4, "y": 0.2, "w": 0.2, "h": 0.1}
                },
            )
        )
        self.assertEqual((decision.action, decision.direction), ("move", "right"))
        self.assertEqual(decision.state, "RETURN_SAFE_RIGHT")

    def test_jump_to_safe_holds_direction_and_taps_configured_jump_key(self):
        instance = bot.BowmanBot.__new__(bot.BowmanBot)
        instance.config = {"keys": {"left": "left", "right": "right", "jump": "alt"}}
        instance.keyboard = MagicMock()
        instance.direction = None
        instance.log = MagicMock()
        instance.last_jump = 0.0

        instance.jump_to_safe("right", 10.0, "RETURN_SAFE_JUMP_RIGHT")

        instance.keyboard.up.assert_called_once_with("left")
        instance.keyboard.down.assert_called_once_with("right")
        instance.keyboard.tap.assert_called_once_with("alt")
        self.assertEqual(instance.last_jump, 10.0)
        self.assertEqual(instance.state, "RETURN_SAFE_JUMP_RIGHT")

    def test_throwing_star_attacks_inside_safe_area_without_chasing(self):
        from mbv.strategies import get_strategy
        from mbv.strategies.base import StrategyActionContext

        strategy = get_strategy("throwing_star_safe")
        common = dict(
            marker=(0.5, 0.25),
            player_box=(480, 125, 40, 1),
            player_anchor=(500.0, 125.0),
            chase_box=(800, 300, 20, 20),
            combat_width=1000,
            combat_height=500,
            has_monster_candidates=True,
            now=10.0,
            last_target_seen=9.0,
            last_pickup=0.0,
            last_jump=0.0,
            direction="right",
            behavior=self.config["behavior"],
            settings={
                "use_safe_output_area": True,
                "jump_interval_seconds": 0.35,
                "minimum_target_vertical_gap": 0.02,
                "close_overlap_threshold": 0.2,
                "jump_attack_cooldown_seconds": 0.45,
            },
            recognition={
                "throwing_star_safe_output_area": {"x": 0.4, "y": 0.2, "w": 0.2, "h": 0.1}
            },
        )
        attack = strategy.decide(StrategyActionContext(target_box=(650, 300, 20, 20), **common))
        wait = strategy.decide(StrategyActionContext(target_box=None, **common))
        self.assertEqual(attack.action, "attack")
        self.assertFalse(attack.face_each_attack)
        self.assertEqual((wait.action, wait.state), ("stop", "TARGET_OUT_OF_RANGE"))

    def test_throwing_star_overlap_threshold_boundary_uses_jump_attack(self):
        from mbv.strategies import get_strategy
        from mbv.strategies.base import StrategyActionContext, horizontal_overlap_ratio

        strategy = get_strategy("throwing_star_safe")
        player_box = (100, 100, 100, 1)
        below_threshold = (181, 180, 100, 20)
        at_threshold = (180, 180, 100, 20)
        self.assertAlmostEqual(horizontal_overlap_ratio(player_box, below_threshold), 0.19)
        self.assertAlmostEqual(horizontal_overlap_ratio(player_box, at_threshold), 0.20)
        common = dict(
            marker=(0.15, 0.2),
            player_box=player_box,
            player_anchor=(150.0, 100.0),
            chase_box=None,
            combat_width=1000,
            combat_height=500,
            has_monster_candidates=True,
            now=10.0,
            last_target_seen=9.0,
            last_pickup=0.0,
            last_jump=0.0,
            last_jump_attack=0.0,
            direction="right",
            behavior=self.config["behavior"],
            settings={
                "use_safe_output_area": False,
                "close_overlap_threshold": 0.2,
                "jump_attack_cooldown_seconds": 0.45,
            },
            recognition={},
        )
        regular = strategy.decide(StrategyActionContext(target_box=below_threshold, **common))
        jumping = strategy.decide(StrategyActionContext(target_box=at_threshold, **common))
        self.assertEqual(regular.action, "attack")
        self.assertEqual(jumping.action, "jump_attack")
        self.assertAlmostEqual(float(jumping.close_overlap_ratio), 0.2)

        disabled_common = dict(common)
        disabled_common["settings"] = {
            **common["settings"],
            "use_close_jump_attack": False,
        }
        disabled = strategy.decide(StrategyActionContext(target_box=at_threshold, **disabled_common))
        self.assertEqual(disabled.action, "attack")

    def test_throwing_star_overlap_waits_during_jump_attack_cooldown(self):
        from mbv.strategies import get_strategy
        from mbv.strategies.base import StrategyActionContext

        strategy = get_strategy("throwing_star_safe")
        decision = strategy.decide(
            StrategyActionContext(
                marker=(0.15, 0.2),
                player_box=(100, 100, 100, 1),
                player_anchor=(150.0, 100.0),
                target_box=(180, 180, 100, 20),
                chase_box=None,
                combat_width=1000,
                combat_height=500,
                has_monster_candidates=True,
                now=10.0,
                last_target_seen=9.0,
                last_pickup=0.0,
                last_jump_attack=9.8,
                direction="right",
                behavior=self.config["behavior"],
                settings={
                    "use_safe_output_area": False,
                    "close_overlap_threshold": 0.2,
                    "jump_attack_cooldown_seconds": 0.45,
                },
                recognition={},
            )
        )
        self.assertEqual((decision.action, decision.state), ("stop", "WAITING_JUMP_ATTACK"))

    def test_jump_attack_holds_alt_then_shift_and_releases_in_reverse_order(self):
        instance = bot.BowmanBot.__new__(bot.BowmanBot)
        instance.config = {
            "keys": {"left": "left", "right": "right", "jump": "alt", "attack": "shift"},
            "behavior": {"face_tap_seconds": 0.025},
        }
        instance.keyboard = MagicMock()
        instance.direction = "right"
        instance.log = MagicMock()
        instance.last_attack = 0.0
        instance.last_jump_attack = 0.0

        with patch("mbv.bot.time.sleep") as sleep:
            instance.jump_attack(0.7, 0.5, 0.25, 10.0)

        calls = instance.keyboard.method_calls
        self.assertLess(calls.index(call.down("alt")), calls.index(call.down("shift")))
        self.assertLess(calls.index(call.down("shift")), calls.index(call.up("shift")))
        self.assertLess(calls.index(call.up("shift")), calls.index(call.up("alt")))
        self.assertEqual(sleep.call_args_list, [call(0.05), call(0.05)])
        self.assertEqual(instance.state, "JUMP_ATTACK_CLOSE")
        self.assertEqual(instance.last_jump_attack, 10.0)

    def test_jump_attack_releases_both_keys_when_attack_press_fails(self):
        instance = bot.BowmanBot.__new__(bot.BowmanBot)
        instance.config = {
            "keys": {"left": "left", "right": "right", "jump": "alt", "attack": "shift"},
            "behavior": {"face_tap_seconds": 0.025},
        }
        instance.keyboard = MagicMock()
        instance.direction = "right"
        instance.log = MagicMock()
        instance.last_attack = 0.0
        instance.last_jump_attack = 0.0
        instance.keyboard.down.side_effect = lambda key: (_ for _ in ()).throw(OSError("按键失败")) if key == "shift" else None

        with patch("mbv.bot.time.sleep"), self.assertRaises(OSError):
            instance.jump_attack(0.7, 0.5, 0.25, 10.0)

        instance.keyboard.up.assert_any_call("shift")
        instance.keyboard.up.assert_any_call("alt")

    def test_throwing_star_stops_above_safe_area_without_down_jump(self):
        from mbv.strategies import get_strategy
        from mbv.strategies.base import StrategyActionContext

        strategy = get_strategy("throwing_star_safe")
        decision = strategy.decide(
            StrategyActionContext(
                marker=(0.5, 0.1),
                player_box=(480, 50, 40, 1),
                player_anchor=(500.0, 50.0),
                target_box=(650, 300, 20, 20),
                chase_box=None,
                combat_width=1000,
                combat_height=500,
                has_monster_candidates=True,
                now=10.0,
                last_target_seen=9.0,
                last_pickup=0.0,
                direction="right",
                behavior=self.config["behavior"],
                settings={
                    "use_safe_output_area": True,
                    "jump_interval_seconds": 0.35,
                    "minimum_target_vertical_gap": 0.02,
                },
                recognition={
                    "throwing_star_safe_output_area": {"x": 0.4, "y": 0.2, "w": 0.2, "h": 0.1}
                },
            )
        )
        self.assertEqual((decision.action, decision.state), ("stop", "SAFE_OUTPUT_ABOVE"))

    def test_stationary_attack_selects_in_range_target_without_chasing(self):
        from mbv.strategies import get_strategy
        from mbv.strategies.base import TargetSelectionContext

        strategy = get_strategy("stationary_attack")
        near_left = bot.Detection((70, 100, 20, 20), 0.9, "left.png")
        far_right = bot.Detection((220, 100, 20, 20), 0.9, "right.png")
        selected = strategy.select_targets(
            TargetSelectionContext(
                detections=[far_right, near_left],
                player_box=(100, 110, 40, 1),
                player_raw_box=(100, 70, 40, 40),
                player_anchor=(120.0, 110.0),
                scene_width=400,
                scene_height=240,
                facing="right",
                target_area={"forward": 0.4, "back": 0.2, "up": 0.2, "down": 0.2},
                settings={},
            )
        )
        self.assertIs(selected.target, near_left)
        self.assertIsNone(selected.chase_target)

    def test_stationary_attack_searches_left_forward_range_while_facing_right(self):
        from mbv.strategies import get_strategy
        from mbv.strategies.base import TargetSelectionContext

        strategy = get_strategy("stationary_attack")
        player = (180, 110, 40, 1)
        left_only_when_facing_left = bot.Detection((75, 100, 20, 20), 0.92, "left.png")
        selected = strategy.select_targets(
            TargetSelectionContext(
                detections=[left_only_when_facing_left],
                player_box=player,
                player_raw_box=(180, 70, 40, 40),
                player_anchor=(200.0, 110.0),
                scene_width=400,
                scene_height=240,
                facing="right",
                target_area={"forward": 0.4, "back": 0.1, "up": 0.2, "down": 0.2},
                settings={},
            )
        )

        self.assertIs(selected.target, left_only_when_facing_left)
        self.assertIsNone(selected.chase_target)

    def test_stationary_attack_only_attacks_or_stops(self):
        from mbv.strategies import get_strategy
        from mbv.strategies.base import StrategyActionContext

        strategy = get_strategy("stationary_attack")
        common = dict(
            marker=(0.8, 0.5),
            player_box=(500, 100, 40, 1),
            player_anchor=(520.0, 100.0),
            chase_box=(800, 100, 20, 20),
            combat_width=1000,
            now=10.0,
            last_target_seen=0.0,
            last_pickup=0.0,
            direction="right",
            behavior=self.config["behavior"],
            settings={},
            recognition={"platform_center": {"x": 0.1, "y": 0.5}},
        )
        attack = strategy.decide(
            StrategyActionContext(target_box=(400, 100, 20, 20), has_monster_candidates=True, **common)
        )
        wait = strategy.decide(
            StrategyActionContext(target_box=None, has_monster_candidates=True, **common)
        )
        self.assertEqual((attack.action, attack.state), ("attack", "ATTACK"))
        self.assertLess(attack.target_x, attack.player_x)
        self.assertFalse(attack.face_each_attack)
        self.assertEqual((wait.action, wait.state), ("stop", "TARGET_OUT_OF_RANGE"))

    def test_stationary_attack_does_not_repeat_direction_tap_on_same_side(self):
        from mbv import bot as runtime_bot

        instance = runtime_bot.BowmanBot.__new__(runtime_bot.BowmanBot)
        instance.config = self.config
        instance.keyboard = MagicMock()
        instance.direction = None
        instance.last_attack = 0.0
        instance.log = MagicMock()

        instance.face_and_attack(0.2, 0.5, 10.0, face_each_attack=False)
        instance.face_and_attack(0.2, 0.5, 11.0, face_each_attack=False)

        left_taps = [call for call in instance.keyboard.tap.call_args_list if call.args[0] == "left"]
        attack_taps = [call for call in instance.keyboard.tap.call_args_list if call.args[0] == "shift"]
        self.assertEqual(len(left_taps), 1)
        self.assertEqual(len(attack_taps), 2)

    def test_bowman_strategy_consumes_common_target_area(self):
        from mbv.strategies import get_strategy
        from mbv.strategies.base import TargetSelectionContext

        strategy = get_strategy("bowman_dynamic")
        monster = bot.Detection((220, 100, 20, 20), 0.9, "monster.png")
        common = {
            "forward": 0.4,
            "back": 0.05,
            "up": 0.2,
            "down": 0.2,
        }
        selected = strategy.select_targets(
            TargetSelectionContext(
                detections=[monster],
                player_box=(100, 110, 40, 1),
                player_raw_box=(100, 70, 40, 40),
                player_anchor=(120.0, 110.0),
                scene_width=400,
                scene_height=240,
                facing="right",
                target_area=common,
                settings=self.config["strategy"]["options"]["bowman_dynamic"],
            )
        )
        self.assertIsNotNone(selected.target)
        self.assertNotIn("attack_box", strategy.default_settings)

    def test_bowman_dynamic_returns_to_platform_center_before_attacking(self):
        from mbv.strategies import get_strategy
        from mbv.strategies.base import StrategyActionContext

        strategy = get_strategy("bowman_dynamic")
        settings = json.loads(json.dumps(self.config["strategy"]["options"]["bowman_dynamic"]))
        settings["platform_center_tolerance"] = 0.1
        context = StrategyActionContext(
            marker=(0.8, 0.5),
            player_box=(500, 100, 40, 1),
            player_anchor=(520.0, 100.0),
            target_box=(600, 100, 20, 20),
            chase_box=None,
            combat_width=1000,
            has_monster_candidates=True,
            now=10.0,
            last_target_seen=9.0,
            last_pickup=0.0,
            direction="right",
            behavior=self.config["behavior"],
            settings=settings,
            recognition={"platform_center": {"x": 0.5, "y": 0.6}},
        )
        decision = strategy.decide(context)
        self.assertEqual(decision.action, "move")
        self.assertEqual(decision.direction, "left")
        self.assertEqual(decision.state, "RETURN_CENTER_LEFT")

    def test_bowman_dynamic_center_return_uses_minimap_marker_not_combat_anchor(self):
        from mbv.strategies import get_strategy
        from mbv.strategies.base import StrategyActionContext

        strategy = get_strategy("bowman_dynamic")
        settings = json.loads(json.dumps(self.config["strategy"]["options"]["bowman_dynamic"]))
        settings["platform_center_tolerance"] = 0.1
        context = StrategyActionContext(
            marker=(0.5, 0.5),
            player_box=(880, 100, 40, 1),
            player_anchor=(900.0, 100.0),
            target_box=(920, 100, 20, 20),
            chase_box=None,
            combat_width=1000,
            has_monster_candidates=True,
            now=10.0,
            last_target_seen=9.0,
            last_pickup=0.0,
            direction="right",
            behavior=self.config["behavior"],
            settings=settings,
            recognition={"platform_center": {"x": 0.5, "y": 0.6}},
        )

        decision = strategy.decide(context)

        self.assertEqual(decision.action, "attack")
        self.assertAlmostEqual(decision.player_x, 0.9)

    def test_bowman_dynamic_attacks_when_inside_center_safe_radius(self):
        from mbv.strategies import get_strategy
        from mbv.strategies.base import StrategyActionContext

        strategy = get_strategy("bowman_dynamic")
        context = StrategyActionContext(
            marker=(0.5, 0.5),
            player_box=(500, 100, 40, 1),
            player_anchor=(520.0, 100.0),
            target_box=(600, 100, 20, 20),
            chase_box=None,
            combat_width=1000,
            has_monster_candidates=True,
            now=10.0,
            last_target_seen=9.0,
            last_pickup=0.0,
            direction="right",
            behavior=self.config["behavior"],
            settings=self.config["strategy"]["options"]["bowman_dynamic"],
            recognition={"platform_center": {"x": 0.5, "y": 0.6}},
        )
        decision = strategy.decide(context)
        self.assertEqual(decision.action, "attack")
        self.assertTrue(decision.target_seen)

    def test_bowman_dynamic_stops_when_minimap_marker_is_temporarily_missing(self):
        from mbv.strategies import get_strategy
        from mbv.strategies.base import StrategyActionContext

        strategy = get_strategy("bowman_dynamic")
        context = StrategyActionContext(
            marker=None,
            player_box=(500, 100, 40, 1),
            player_anchor=(520.0, 100.0),
            target_box=(600, 100, 20, 20),
            chase_box=None,
            combat_width=1000,
            has_monster_candidates=True,
            now=10.0,
            last_target_seen=9.0,
            last_pickup=0.0,
            direction="right",
            behavior=self.config["behavior"],
            settings=self.config["strategy"]["options"]["bowman_dynamic"],
            recognition={"platform_center": {"x": 0.5, "y": 0.6}},
        )

        decision = strategy.decide(context)

        self.assertEqual((decision.action, decision.state), ("stop", "MARKER_LOST"))

    def test_recognition_region_capture_saves_minimap_platform_center(self):
        from mbv import calibrate as runtime_calibrate

        region = type("Result", (), {"cancelled": False, "rectangle": (100, 50, 800, 400)})()
        center = type("Result", (), {"cancelled": False, "point": (500, 200)})()
        window = bot.WindowInfo(123, "MapleStory", 0, 0, 1000, 500)
        preview = np.ones((500, 1000, 3), dtype=np.uint8)
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps(self.config), encoding="utf-8")
            with (
                patch.object(runtime_calibrate, "find_game_window", return_value=window),
                patch.object(runtime_calibrate, "focus_game_window"),
                patch.object(runtime_calibrate, "mss"),
                patch.object(runtime_calibrate, "capture_client", return_value=np.zeros((500, 1000, 3), dtype=np.uint8)),
                patch.object(
                    runtime_calibrate,
                    "magnified_roi_preview",
                    return_value=(preview, (300, 100, 400, 200), 4.0),
                ),
                patch.object(runtime_calibrate, "interactive_overlay", side_effect=[region, center]) as overlay,
            ):
                captured = runtime_calibrate.capture_recognition_region(path)
            saved = bot.load_config(path)
        self.assertEqual(captured, {"x": 0.5, "y": 0.5})
        self.assertIs(overlay.call_args_list[1].kwargs["frozen_frame"], preview)
        self.assertEqual(overlay.call_args_list[1].kwargs["guide_rect"], (300, 100, 400, 200))
        self.assertEqual(saved["recognition"]["platform_center_space"], "minimap")
        self.assertTrue(saved["recognition"]["platform_center_captured"])
        self.assertTrue(saved["calibration"]["recognition_region_complete"])

    def test_platform_center_capture_uses_magnified_minimap(self):
        from mbv import calibrate as runtime_calibrate

        selected = type("Result", (), {"cancelled": False, "point": (500, 200)})()
        window = bot.WindowInfo(123, "MapleStory", 0, 0, 1000, 500)
        frozen = np.zeros((500, 1000, 3), dtype=np.uint8)
        preview = np.ones_like(frozen)
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps(self.config), encoding="utf-8")
            with (
                patch.object(runtime_calibrate, "find_game_window", return_value=window),
                patch.object(runtime_calibrate, "focus_game_window"),
                patch.object(runtime_calibrate.mss, "MSS"),
                patch.object(runtime_calibrate, "capture_client", return_value=frozen),
                patch.object(
                    runtime_calibrate,
                    "magnified_roi_preview",
                    return_value=(preview, (300, 100, 400, 200), 4.0),
                ),
                patch.object(runtime_calibrate, "interactive_overlay", return_value=selected) as overlay,
            ):
                captured = runtime_calibrate.capture_platform_center(path)
            saved = bot.load_config(path)

        self.assertEqual(captured, {"x": 0.5, "y": 0.5})
        self.assertIs(overlay.call_args.kwargs["frozen_frame"], preview)
        self.assertEqual(overlay.call_args.kwargs["guide_rect"], (300, 100, 400, 200))
        self.assertIn("小地图已放大", overlay.call_args.args[1])
        self.assertEqual(saved["recognition"]["platform_center_space"], "minimap")
        self.assertTrue(saved["calibration"]["items"]["platform_center"]["complete"])

    def test_status_regions_are_captured_and_saved_independently(self):
        from mbv import calibrate as runtime_calibrate

        result = type("Result", (), {"cancelled": False, "rectangle": (100, 50, 300, 20)})()
        window = bot.WindowInfo(123, "MapleStory", 0, 0, 1000, 500)
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps(self.config), encoding="utf-8")
            with (
                patch.object(runtime_calibrate, "find_game_window", return_value=window),
                patch.object(runtime_calibrate, "focus_game_window"),
                patch.object(runtime_calibrate.mss, "MSS"),
                patch.object(runtime_calibrate, "capture_client", return_value=np.zeros((500, 1000, 3))),
                patch.object(runtime_calibrate, "interactive_overlay", return_value=result),
            ):
                captured = runtime_calibrate.capture_status_region(path, "hp_bar", "血条区域")
            saved = bot.load_config(path)

        self.assertEqual(captured, {"x": 0.1, "y": 0.1, "w": 0.3, "h": 0.04})
        self.assertTrue(saved["calibration"]["items"]["hp_bar"]["complete"])
        self.assertFalse(saved["calibration"]["items"]["mp_bar"]["complete"])

    def test_magnified_roi_preview_maps_click_back_to_original_frame(self):
        from mbv.calibrate import magnified_roi_preview, map_magnified_point

        frame = np.zeros((100, 200, 3), dtype=np.uint8)
        source_rect = (10, 20, 20, 10)
        frame[23, 15] = (20, 120, 240)

        preview, display_rect, zoom = magnified_roi_preview(
            frame,
            source_rect,
            max_zoom=4.0,
            top_inset=10,
            margin=10,
        )
        mapped = map_magnified_point((82, 44), display_rect, source_rect)

        self.assertEqual(preview.shape, frame.shape)
        self.assertEqual(display_rect, (60, 30, 80, 40))
        self.assertEqual(zoom, 4.0)
        self.assertEqual(mapped, (15, 23))
        self.assertTrue(np.array_equal(preview[44, 82], frame[23, 15]))
        with self.assertRaisesRegex(ValueError, "放大的小地图"):
            map_magnified_point((0, 0), display_rect, source_rect)

    def test_player_marker_capture_uses_magnified_minimap_but_samples_original_frame(self):
        from mbv import calibrate as runtime_calibrate

        background = cv2.cvtColor(
            np.asarray([[[28, 153, 170]]], dtype=np.uint8),
            cv2.COLOR_HSV2BGR,
        )[0, 0]
        marker_color = cv2.cvtColor(
            np.asarray([[[30, 119, 255]]], dtype=np.uint8),
            cv2.COLOR_HSV2BGR,
        )[0, 0]
        frame = np.full((500, 1000, 3), background, dtype=np.uint8)
        window = bot.WindowInfo(123, "MapleStory", 0, 0, 1000, 500)
        minimap_rect = bot.roi_pixels(frame.shape, self.config["regions"]["minimap"])
        mx, my, mw, mh = minimap_rect
        marker_x, marker_y = mw // 2, mh // 2
        frame[my + marker_y - 1 : my + marker_y + 3, mx + marker_x - 1 : mx + marker_x + 3] = marker_color
        preview, display_rect, _zoom = runtime_calibrate.magnified_roi_preview(frame, minimap_rect)
        dx, dy, dw, dh = display_rect
        selected = type(
            "Result",
            (),
            {
                "cancelled": False,
                "point": (
                    dx + int((marker_x + 0.5) * dw / mw),
                    dy + int((marker_y + 0.5) * dh / mh),
                ),
            },
        )()
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps(self.config), encoding="utf-8")
            with (
                patch.object(runtime_calibrate, "find_game_window", return_value=window),
                patch.object(runtime_calibrate, "focus_game_window"),
                patch.object(runtime_calibrate.mss, "MSS"),
                patch.object(runtime_calibrate, "capture_client", return_value=frame),
                patch.object(runtime_calibrate, "interactive_overlay", return_value=selected) as overlay,
            ):
                captured = runtime_calibrate.capture_player_marker(path)
            saved = bot.load_config(path)

        self.assertEqual(captured, (30, 119, 255))
        self.assertEqual(overlay.call_args.kwargs["guide_rect"], display_rect)
        self.assertTrue(np.array_equal(overlay.call_args.kwargs["frozen_frame"], preview))
        self.assertIn("放大", overlay.call_args.args[1])
        self.assertAlmostEqual(
            saved["calibration"]["player_marker_position"][0],
            (marker_x + 0.5) / mw,
            delta=0.02,
        )
        self.assertAlmostEqual(
            saved["calibration"]["player_marker_position"][1],
            (marker_y + 0.5) / mh,
            delta=0.02,
        )

    def test_player_marker_sample_rejects_a_color_that_matches_multiple_map_objects(self):
        from mbv.calibrate import analyze_player_marker_sample

        background = cv2.cvtColor(
            np.asarray([[[100, 30, 80]]], dtype=np.uint8),
            cv2.COLOR_HSV2BGR,
        )[0, 0]
        marker_color = cv2.cvtColor(
            np.asarray([[[30, 140, 255]]], dtype=np.uint8),
            cv2.COLOR_HSV2BGR,
        )[0, 0]
        minimap = np.full((80, 100, 3), background, dtype=np.uint8)
        minimap[19:23, 19:23] = marker_color
        minimap[59:63, 69:73] = marker_color

        with self.assertRaisesRegex(RuntimeError, "命中多个位置"):
            analyze_player_marker_sample(minimap, (20, 20), 2, 180)

    def test_proportional_resize_between_status_captures_preserves_previous_item(self):
        from mbv import calibrate as runtime_calibrate

        hp_result = type("Result", (), {"cancelled": False, "rectangle": (100, 50, 300, 20)})()
        mp_result = type("Result", (), {"cancelled": False, "rectangle": (120, 420, 360, 24)})()
        hp_window = bot.WindowInfo(123, "MapleStory", 0, 0, 1000, 500)
        mp_window = bot.WindowInfo(123, "MapleStory", 0, 0, 1200, 600)
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps(self.config), encoding="utf-8")
            with (
                patch.object(runtime_calibrate, "find_game_window", side_effect=[hp_window, mp_window]),
                patch.object(runtime_calibrate, "focus_game_window"),
                patch.object(runtime_calibrate.mss, "MSS"),
                patch.object(
                    runtime_calibrate,
                    "capture_client",
                    side_effect=[np.zeros((500, 1000, 3)), np.zeros((600, 1200, 3))],
                ),
                patch.object(runtime_calibrate, "interactive_overlay", side_effect=[hp_result, mp_result]),
            ):
                runtime_calibrate.capture_status_region(path, "hp_bar", "HP")
                runtime_calibrate.capture_status_region(path, "mp_bar", "MP")
            saved = bot.load_config(path)

        self.assertTrue(saved["calibration"]["items"]["hp_bar"]["complete"])
        self.assertTrue(saved["calibration"]["items"]["mp_bar"]["complete"])
        self.assertEqual(saved["calibration"]["window_size"], [1200, 600])

    def test_combat_region_recapture_preserves_minimap_platform_center(self):
        from mbv import calibrate as runtime_calibrate

        result = type("Result", (), {"cancelled": False, "rectangle": (20, 30, 900, 400)})()
        window = bot.WindowInfo(123, "MapleStory", 0, 0, 1000, 500)
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            ready = json.loads(json.dumps(self.config))
            ready["recognition"]["platform_center_captured"] = True
            ready["calibration"]["items"]["platform_center"] = {"complete": True}
            path.write_text(json.dumps(ready), encoding="utf-8")
            with (
                patch.object(runtime_calibrate, "find_game_window", return_value=window),
                patch.object(runtime_calibrate, "focus_game_window"),
                patch.object(runtime_calibrate.mss, "MSS"),
                patch.object(runtime_calibrate, "capture_client", return_value=np.zeros((500, 1000, 3))),
                patch.object(runtime_calibrate, "interactive_overlay", return_value=result),
            ):
                runtime_calibrate.capture_combat_region(path)
            saved = bot.load_config(path)

        self.assertTrue(saved["calibration"]["recognition_region_complete"])
        self.assertTrue(saved["recognition"]["platform_center_captured"])
        self.assertTrue(saved["calibration"]["items"]["platform_center"]["complete"])

    def test_minimap_recapture_invalidates_platform_center_and_player_marker(self):
        from mbv import calibrate as runtime_calibrate

        result = type("Result", (), {"cancelled": False, "rectangle": (20, 30, 120, 80)})()
        window = bot.WindowInfo(123, "MapleStory", 0, 0, 1000, 500)
        ready = json.loads(json.dumps(self.config))
        ready["recognition"]["platform_center_captured"] = True
        ready["calibration"]["items"]["platform_center"] = {"complete": True}
        ready["calibration"]["items"]["player_marker"] = {"complete": True}
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps(ready), encoding="utf-8")
            with (
                patch.object(runtime_calibrate, "find_game_window", return_value=window),
                patch.object(runtime_calibrate, "focus_game_window"),
                patch.object(runtime_calibrate.mss, "MSS"),
                patch.object(runtime_calibrate, "capture_client", return_value=np.zeros((500, 1000, 3))),
                patch.object(runtime_calibrate, "interactive_overlay", return_value=result),
            ):
                runtime_calibrate.capture_status_region(path, "minimap", "小地图")
            saved = bot.load_config(path)

        self.assertFalse(saved["recognition"]["platform_center_captured"])
        self.assertFalse(saved["calibration"]["items"]["platform_center"]["complete"])
        self.assertFalse(saved["calibration"]["items"]["player_marker"]["complete"])

    def test_strategy_area_capture_is_relative_to_combat_region(self):
        from mbv import calibrate as runtime_calibrate

        selected = type("Result", (), {"cancelled": False, "rectangle": (400, 100, 200, 100)})()
        window = bot.WindowInfo(123, "MapleStory", 0, 0, 1000, 500)
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps(self.config), encoding="utf-8")
            with (
                patch.object(runtime_calibrate, "find_game_window", return_value=window),
                patch.object(runtime_calibrate, "focus_game_window"),
                patch.object(runtime_calibrate.mss, "MSS"),
                patch.object(runtime_calibrate, "capture_client", return_value=np.zeros((500, 1000, 3))),
                patch.object(runtime_calibrate, "interactive_overlay", return_value=selected),
            ):
                captured = runtime_calibrate.capture_strategy_area(
                    path,
                    "throwing_star_safe_output_area",
                    "框选安全区",
                )
            saved = bot.load_config(path)
        self.assertEqual(
            captured,
            {"x": 0.4, "y": 0.166667, "w": 0.2, "h": 0.222222},
        )
        self.assertEqual(saved["recognition"]["throwing_star_safe_output_area"], captured)
        self.assertTrue(saved["recognition"]["throwing_star_safe_output_area_captured"])

    def test_old_config_gets_throwing_star_defaults_without_becoming_ready(self):
        legacy = json.loads(json.dumps(self.config))
        legacy["keys"].pop("jump", None)
        legacy["recognition"].pop("throwing_star_safe_output_area", None)
        legacy["recognition"].pop("throwing_star_safe_output_area_captured", None)
        legacy["strategy"]["options"].pop("throwing_star_safe", None)
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps(legacy), encoding="utf-8")
            loaded = bot.load_config(path)
        self.assertEqual(loaded["keys"]["jump"], "alt")
        self.assertFalse(loaded["recognition"]["throwing_star_safe_output_area_captured"])
        self.assertEqual(
            loaded["strategy"]["options"]["throwing_star_safe"],
            {
                "use_close_jump_attack": True,
                "use_safe_output_area": False,
                "jump_interval_seconds": 0.35,
                "minimum_target_vertical_gap": 0.02,
                "close_overlap_threshold": 0.2,
                "jump_attack_cooldown_seconds": 0.45,
            },
        )

    def test_captured_monster_filter_keeps_full_rectangle_mask(self):
        from mbv import calibrate as runtime_calibrate

        image = np.zeros((16, 20, 3), dtype=np.uint8)
        image[:, :10] = (30, 90, 180)
        window = type("Window", (), {})()
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with (
                patch.object(runtime_calibrate, "load_config", return_value=self.config),
                patch.object(runtime_calibrate, "find_game_window", return_value=window),
                patch.object(runtime_calibrate, "capture_frozen_selection", return_value=image),
                patch.object(runtime_calibrate, "monster_template_directory", return_value=directory),
            ):
                path = runtime_calibrate.capture_monster_filter(Path("unused.json"), category="绿水灵")
            decoded = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
        self.assertEqual(decoded.shape, (16, 20, 4))
        self.assertTrue(np.all(decoded[:, :, 3] == 255))

    def test_brown_monster_alpha_keeps_subject_and_removes_border_background(self):
        from mbv.vision import Template, find_detections, monster_template_alpha

        image = np.full((80, 100, 3), (185, 205, 215), dtype=np.uint8)
        cv2.ellipse(image, (50, 43), (31, 25), 0, 0, 360, (45, 80, 125), -1)
        cv2.circle(image, (60, 37), 4, (240, 240, 240), -1)

        alpha = monster_template_alpha(image)

        self.assertEqual(alpha[43, 50], 255)
        self.assertEqual(alpha[0, 0], 0)
        self.assertGreater(np.count_nonzero(alpha), image.shape[0] * image.shape[1] * 0.08)
        self.assertLess(np.count_nonzero(alpha), image.shape[0] * image.shape[1] * 0.88)

        template = Template("brown.png", image, alpha)
        flat_brown_scene = np.full((180, 240, 3), (45, 80, 125), dtype=np.uint8)
        color_only, _color_score, _name = find_detections(
            flat_brown_scene,
            [template],
            0.79,
            0.5,
        )
        structured, _structured_score, _name = find_detections(
            flat_brown_scene,
            [template],
            0.79,
            0.5,
            structure_weight=0.15,
        )
        exact, _exact_score, _name = find_detections(
            image,
            [template],
            0.79,
            0.5,
            structure_weight=0.15,
        )
        self.assertTrue(color_only)
        self.assertFalse(structured)
        self.assertTrue(exact)

    def test_captured_monster_template_persists_generated_alpha(self):
        from mbv import calibrate as runtime_calibrate

        image = np.full((80, 100, 3), (185, 205, 215), dtype=np.uint8)
        cv2.ellipse(image, (50, 43), (31, 25), 0, 0, 360, (45, 80, 125), -1)
        window = type("Window", (), {})()
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with (
                patch.object(runtime_calibrate, "load_config", return_value=self.config),
                patch.object(runtime_calibrate, "find_game_window", return_value=window),
                patch.object(runtime_calibrate, "capture_frozen_selection", return_value=image),
                patch.object(runtime_calibrate, "monster_template_directory", return_value=directory),
            ):
                path = runtime_calibrate.capture_template(Path("unused.json"), category="野猪树妖")
            decoded = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_UNCHANGED)

        self.assertEqual(decoded.shape[2], 4)
        self.assertLess(decoded.shape[0], image.shape[0])
        self.assertLess(decoded.shape[1], image.shape[1])
        self.assertGreater(np.count_nonzero(decoded[:, :, 3]), decoded.shape[0] * decoded.shape[1] * 0.4)
        self.assertEqual(decoded[0, 0, 3], 0)

    def test_monster_template_generation_accepts_loose_selection_and_tightens_it(self):
        from mbv.vision import monster_template_image

        image = np.full((180, 240, 3), (185, 205, 215), dtype=np.uint8)
        cv2.ellipse(image, (120, 96), (28, 21), 0, 0, 360, (45, 80, 125), -1)

        generated = monster_template_image(image)

        self.assertEqual(generated.shape[2], 4)
        self.assertLess(generated.shape[0], image.shape[0] // 2)
        self.assertLess(generated.shape[1], image.shape[1] // 2)
        self.assertGreater(np.count_nonzero(generated[:, :, 3]), generated.shape[0] * generated.shape[1] * 0.4)

    def test_monster_alpha_rejects_indistinguishable_full_frame(self):
        from mbv.vision import monster_template_alpha

        image = np.full((40, 60, 3), 120, dtype=np.uint8)

        with self.assertRaisesRegex(ValueError, "怪物"):
            monster_template_alpha(image)

    def test_captured_template_never_overwrites_an_existing_timestamp(self):
        from datetime import datetime as real_datetime
        from mbv import calibrate as runtime_calibrate

        class FixedDateTime:
            @classmethod
            def now(cls):
                return real_datetime(2026, 8, 14, 12, 30, 0, 123456)

        first_image = np.full((8, 8, 3), 20, dtype=np.uint8)
        second_image = np.full((8, 8, 3), 220, dtype=np.uint8)
        with TemporaryDirectory() as temporary, patch.object(
            runtime_calibrate,
            "datetime",
            FixedDateTime,
        ):
            directory = Path(temporary)
            first = runtime_calibrate._save_captured_template(first_image, directory, "head", "玩家头部")
            second = runtime_calibrate._save_captured_template(second_image, directory, "head", "玩家头部")
            first_decoded = cv2.imdecode(np.fromfile(first, dtype=np.uint8), cv2.IMREAD_COLOR)
            second_decoded = cv2.imdecode(np.fromfile(second, dtype=np.uint8), cv2.IMREAD_COLOR)

        self.assertNotEqual(first, second)
        self.assertTrue(np.all(first_decoded == 20))
        self.assertTrue(np.all(second_decoded == 220))

    def test_captured_player_template_persists_independent_foot_anchor(self):
        from mbv import calibrate as runtime_calibrate

        image = np.full((12, 18, 4), 200, dtype=np.uint8)
        with TemporaryDirectory() as temporary:
            path = runtime_calibrate._save_captured_template(
                image,
                Path(temporary),
                "nameplate",
                "玩家姓名板",
                anchor_offset=(9.5, -6.0),
            )
            metadata = json.loads(path.with_suffix(".anchor.json").read_text(encoding="utf-8"))

        self.assertEqual(metadata["version"], 1)
        self.assertEqual(metadata["anchor_offset"], [9.5, -6.0])

    def test_template_preview_keeps_aspect_ratio_and_flattens_alpha(self):
        from mbv.panel import template_preview_image

        image = np.zeros((40, 80, 4), dtype=np.uint8)
        image[10:30, 20:60] = (255, 80, 30, 255)
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "preview.png"
            ok, encoded = cv2.imencode(".png", image)
            self.assertTrue(ok)
            encoded.tofile(path)
            preview = template_preview_image(path, (40, 40))
        self.assertEqual(preview.size, (40, 20))
        self.assertEqual(preview.mode, "RGB")

    def test_template_manager_exposes_all_persisted_image_kinds(self):
        from mbv.panel import TEMPLATE_GROUPS

        self.assertEqual(
            TEMPLATE_GROUPS,
            (
                ("monster", "怪物模板"),
                ("filter", "过滤项"),
                ("player", "姓名板"),
                ("head", "头部"),
                ("title", "称号勋章"),
            ),
        )

    def test_strategy_numeric_stepper_changes_values_without_keyboard_focus(self):
        from mbv.panel import adjusted_numeric_text

        self.assertEqual(adjusted_numeric_text("0.2808", 0.01, 0.0, 1.0), "0.2908")
        self.assertEqual(adjusted_numeric_text("0.005", -0.01, 0.0, 1.0), "0")
        self.assertEqual(adjusted_numeric_text("0.5", 0.01, 0.0, 0.5), "0.5")

    def test_targeting_numeric_preview_is_persisted_immediately(self):
        from mbv.panel import ControlPanel
        from mbv.strategies import get_strategy

        strategy = get_strategy("bowman_dynamic")
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps(self.config), encoding="utf-8")
            instance = ControlPanel.__new__(ControlPanel)
            instance.config_path = path
            instance.bot = MagicMock()
            instance.bot.strategy = strategy
            instance._selected_strategy = lambda: strategy

            instance._preview_targeting_setting("box.forward", "0.35")

            loaded = bot.load_config(path)
        self.assertEqual(
            loaded["targeting"]["box"]["forward"],
            0.35,
        )
        instance.bot.preview_targeting_setting.assert_called_once_with("box.forward", 0.35)

    def test_running_panel_persists_common_and_strategy_settings_before_restart(self):
        from mbv.panel import ControlPanel

        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps(self.config), encoding="utf-8")
            with patch.object(ControlPanel, "_run_bot", lambda _self: None):
                panel = ControlPanel(path, enable_input=False)
            panel.bot.armed = True
            changes = {
                "behavior.attack_interval_seconds": "0.31",
                "targeting.box.forward": "0.41",
                "strategy.options.bowman_dynamic.platform_center_tolerance": "0.21",
            }
            for key, value in changes.items():
                entry = (
                    panel._entries.get(key)
                    or panel._targeting_entries.get(key)
                    or panel._strategy_entries.get(key)
                )
                entry.delete(0, "end")
                entry.insert(0, value)
            panel.hp_threshold_percent.set(42)
            panel.mp_threshold_percent.set(33)
            panel.fallback_patrol.set(True)
            panel.pickup_lost.set(True)
            panel.minimap_assist.set(False)

            self.assertTrue(
                panel._persist_settings(apply_runtime=False, notify=False, show_error=False)
            )
            self.assertTrue(panel.bot.armed)
            reloaded = bot.load_config(path)
            panel.overlay.close()
            panel.root.destroy()

        self.assertEqual(reloaded["behavior"]["attack_interval_seconds"], 0.31)
        self.assertEqual(reloaded["targeting"]["box"]["forward"], 0.41)
        self.assertEqual(
            reloaded["strategy"]["options"]["bowman_dynamic"]["platform_center_tolerance"],
            0.21,
        )
        self.assertEqual(reloaded["behavior"]["hp_threshold"], 0.42)
        self.assertEqual(reloaded["behavior"]["mp_threshold"], 0.33)
        self.assertTrue(reloaded["behavior"]["fallback_patrol"])
        self.assertTrue(reloaded["behavior"]["pickup_after_target_lost"])
        self.assertFalse(reloaded["vision"]["player_minimap_assist_enabled"])

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

    def test_missing_personal_config_is_created_once_from_example(self):
        with TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            example_path = temporary_path / "config.example.json"
            config_path = temporary_path / "config.json"
            example = {"version": 1, "calibrated": False, "input": {"delivery": "background"}}
            example_path.write_text(json.dumps(example, ensure_ascii=False), encoding="utf-8")

            self.assertTrue(bot.create_config_from_example(config_path, example_path))
            self.assertEqual(json.loads(config_path.read_text(encoding="utf-8")), example)

            config_path.write_text('{"version": 1, "preserved": true}\n', encoding="utf-8")
            self.assertFalse(bot.create_config_from_example(config_path, example_path))
            self.assertTrue(json.loads(config_path.read_text(encoding="utf-8"))["preserved"])

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
        self.assertEqual(set(counts), {"monster", "filter", "category", "player", "head", "title"})
        for value in counts.values():
            self.assertGreaterEqual(value, 0)

    def test_act_never_sends_input_without_authorization(self):
        instance = bot.BowmanBot.__new__(bot.BowmanBot)
        instance.armed = True
        instance.input_authorized = False
        instance.action_lock = __import__("threading").RLock()
        instance.keyboard = MagicMock()

        instance.act(None, 1.0, 1.0, None, None, None, None, 1, False, 0.0)

        self.assertEqual(instance.keyboard.method_calls, [])

    def test_start_bat_is_the_only_daily_startup(self):
        startup_files = sorted(path.name for path in ROOT.glob("Start*.bat"))
        self.assertEqual(startup_files, ["Start.bat"])
        script = (ROOT / "Start.bat").read_text(encoding="utf-8")
        self.assertIn("-Verb RunAs", script)
        self.assertIn("--enable-input", script)

    def test_apply_config_switches_delivery_without_sendinput(self):
        from mbv.player_tracking import PlayerTrackState

        instance = bot.BowmanBot.__new__(bot.BowmanBot)
        instance.armed = False
        instance.delivery = "foreground"
        instance.background_input = False
        instance.keyboard = bot.Keyboard("foreground")
        instance.keyboard.hwnd = 0
        instance.config_lock = __import__("threading").Lock()
        instance.action_lock = __import__("threading").RLock()
        instance.f8_requested = __import__("threading").Event()
        instance.player_track = PlayerTrackState(last_auxiliary_at=123.0)
        config = json.loads(json.dumps(self.config))
        config.setdefault("input", {})["delivery"] = "background"
        instance.apply_config(config)
        self.assertEqual(instance.delivery, "background")
        self.assertTrue(instance.background_input)
        self.assertEqual(instance.player_track.last_auxiliary_at, 0.0)
        self.assertEqual(instance.keyboard.delivery, "background")

    def test_apply_config_clears_pending_toggle_and_keeps_bot_paused(self):
        import threading

        instance = bot.BowmanBot.__new__(bot.BowmanBot)
        instance.armed = True
        instance.delivery = "background"
        instance.background_input = True
        instance.keyboard = MagicMock()
        instance.keyboard.root_hwnd = 0
        instance.keyboard.hwnd = 0
        instance.config_lock = threading.Lock()
        instance.action_lock = threading.RLock()
        instance.f8_requested = threading.Event()
        instance.f8_requested.set()
        instance.log = MagicMock()
        instance.notify = MagicMock()
        config = json.loads(json.dumps(self.config))

        instance.apply_config(config)

        self.assertFalse(instance.armed)
        self.assertFalse(instance.f8_requested.is_set())
        instance.keyboard.release_all.assert_called_once_with()

    def test_targeting_setting_preview_updates_runtime_without_disarming(self):
        import threading
        from mbv.strategies import active_strategy

        instance = bot.BowmanBot.__new__(bot.BowmanBot)
        instance.config = json.loads(json.dumps(self.config))
        instance.strategy = active_strategy(instance.config)
        instance.action_lock = threading.RLock()
        instance.config_lock = threading.Lock()
        instance.log = MagicMock()
        instance.armed = True

        instance.preview_targeting_setting("box.forward", 0.35)

        self.assertEqual(
            instance.config["targeting"]["box"]["forward"],
            0.35,
        )
        self.assertTrue(instance.armed)
        instance.log.write.assert_called_once()

    def test_suspend_vision_clears_pending_toggle_and_releases_input(self):
        import threading

        instance = bot.BowmanBot.__new__(bot.BowmanBot)
        instance.armed = True
        instance.state = "ATTACK_RIGHT"
        instance.action_lock = threading.RLock()
        instance.vision_suspended = threading.Event()
        instance.f8_requested = threading.Event()
        instance.f8_requested.set()
        instance.keyboard = MagicMock()
        instance.log = MagicMock()
        instance.notify = MagicMock()

        instance.suspend_vision()

        self.assertTrue(instance.vision_suspended.is_set())
        self.assertFalse(instance.f8_requested.is_set())
        self.assertFalse(instance.armed)
        instance.keyboard.release_all.assert_called()

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

    def test_attack_box_capture_can_share_smoothed_runtime_anchor(self):
        combat = (10, 20, 400, 300)
        player = (100, 180, 40, 1)
        rectangle = (110, 120, 200, 90)
        box = bot.attack_box_from_rectangle(
            rectangle,
            combat,
            player,
            raw_box=(100, 60, 40, 40),
            facing="right",
            player_anchor=(120.0, 180.0),
        )
        self.assertEqual(box["up"], round(80 / 300, 6))
        self.assertEqual(box["down"], round(10 / 300, 6))

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
        self.assertTrue(overlay.debug_item_enabled({}, "hp_bar"))
        self.assertTrue(overlay.debug_item_enabled({"debug_item": "hp_bar"}, "hp_bar"))
        self.assertFalse(overlay.debug_item_enabled({"debug_item": "hp_bar"}, "mp_bar"))

    def test_runtime_overlay_hide_blocks_queued_redraw_until_show(self):
        import game_overlay as overlay

        instance = overlay.RuntimeOverlay.__new__(overlay.RuntimeOverlay)
        instance._updates = queue.Queue(maxsize=1)
        instance._closed = False
        instance._visible = True
        instance._last_state = None
        instance._root = MagicMock()
        instance._exit_root = MagicMock()
        instance._canvas = MagicMock()
        instance._hwnd = 123
        state = {"left": 10, "top": 20, "width": 800, "height": 600}
        instance.update(state)

        with patch.object(instance, "_draw") as draw:
            instance.hide()
            instance._poll()
            draw.assert_not_called()
            self.assertEqual(instance._last_state, state)

            instance.show()
            draw.assert_called_once_with(instance._root, instance._canvas, 123, state)

    def test_closed_runtime_overlay_does_not_redraw_when_shown(self):
        import game_overlay as overlay

        instance = overlay.RuntimeOverlay.__new__(overlay.RuntimeOverlay)
        instance._closed = True
        instance._visible = False
        instance._last_state = {"width": 800}
        instance._root = MagicMock()
        instance._canvas = MagicMock()
        instance._hwnd = 123
        with patch.object(instance, "_draw") as draw:
            instance.show()
        draw.assert_not_called()
        self.assertFalse(instance._visible)

    def test_template_manager_cleanup_clears_busy_when_overlay_is_closed(self):
        from mbv.panel import ControlPanel

        instance = ControlPanel.__new__(ControlPanel)
        instance.busy = True
        instance.bot = MagicMock()
        instance.overlay = MagicMock()
        instance.overlay.show.side_effect = RuntimeError("HUD 已关闭")
        instance.root = MagicMock()
        instance.root.winfo_exists.return_value = False

        instance._close_template_manager(None)

        instance.bot.resume_vision.assert_called_once_with()
        self.assertFalse(instance.busy)

    def test_panel_debug_button_updates_bot_visibility(self):
        from mbv.panel import ControlPanel

        instance = ControlPanel.__new__(ControlPanel)
        instance.busy = False
        instance.debug_boxes = MagicMock()
        instance.debug_boxes.get.return_value = False
        instance.bot = MagicMock()

        instance._toggle_debug_boxes()

        instance.bot.set_calibration_overlay_visible.assert_called_once_with(False)

    def test_panel_capture_show_uses_single_debug_item_mode(self):
        from mbv.panel import ControlPanel

        instance = ControlPanel.__new__(ControlPanel)
        instance.debug_boxes = MagicMock()
        instance.bot = MagicMock()

        instance._show_capture_item("血条区域", "hp_bar")

        instance.debug_boxes.set.assert_called_once_with(True)
        instance.bot.set_calibration_overlay_item.assert_called_once_with("hp_bar")

    def test_control_panel_is_single_column_without_decoupling_info_stage(self):
        panel_source = (ROOT / "mbv" / "panel.py").read_text(encoding="utf-8")

        self.assertNotIn('text="游戏窗口已解耦"', panel_source)
        self.assertNotIn('text="实时识别状态"', panel_source)
        self.assertNotIn("validation_metrics", panel_source)
        self.assertIn("self.root.minsize(560, 720)", panel_source)

    def test_player_nameplate_is_grouped_under_player_templates(self):
        panel_source = (ROOT / "mbv" / "panel.py").read_text(encoding="utf-8")
        combat_start = panel_source.index('self._section("战斗识别")')
        templates_start = panel_source.index('self._section("模板采集")')
        strategy_start = panel_source.index('self._section("职业与策略")')

        self.assertNotIn("玩家姓名板", panel_source[combat_start:templates_start])
        self.assertIn("怪物模板", panel_source[templates_start:strategy_start])
        self.assertIn("人物模板", panel_source[templates_start:strategy_start])
        self.assertIn("玩家姓名板", panel_source[templates_start:strategy_start])

    def test_toggle_calibration_overlay_is_independent_of_hide(self):
        instance = bot.BowmanBot.__new__(bot.BowmanBot)
        instance.calibration_overlay_visible = True
        instance.calibration_overlay_item = "hp_bar"
        instance.log = type("Log", (), {"write": lambda *_args, **_kwargs: None})()
        instance.toggle_calibration_overlay()
        self.assertFalse(instance.calibration_overlay_visible)
        self.assertIsNone(instance.calibration_overlay_item)
        instance.toggle_calibration_overlay()
        self.assertTrue(instance.calibration_overlay_visible)
        instance.set_calibration_overlay_item("mp_bar")
        self.assertEqual(instance.calibration_overlay_item, "mp_bar")
        self.assertTrue(instance.calibration_overlay_visible)
        instance.set_calibration_overlay_visible(False)
        self.assertFalse(instance.calibration_overlay_visible)
        self.assertIsNone(instance.calibration_overlay_item)
        self.assertEqual(bot.vk_for("f7"), 0x76)
        self.assertEqual(bot.name_for_vk(0x76), "f7")


if __name__ == "__main__":
    unittest.main()
