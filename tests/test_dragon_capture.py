from __future__ import annotations

from contextlib import ExitStack, contextmanager
from copy import deepcopy
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mbv import calibrate
from mbv.config import load_config, save_config
from mbv.strategies import get_strategy
from mbv.vision import player_relative_region_rect
from mbv.window import WindowInfo


STRATEGY = "dragon_roar"
POINT = "dragon_roar_point"


class DragonRoarCaptureTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(ROOT / "config.example.json")
        self.strategy = get_strategy(STRATEGY)
        self.range_field = next(
            field for field in self.strategy.capture_fields if field.settings_path == "attack_regions"
        )
        self.region_key = self.range_field.recognition_key
        self.window = WindowInfo(123, "MockMaple", 0, 0, 1000, 500)
        self.frozen = np.zeros((500, 1000, 3), dtype=np.uint8)
        self.config["regions"]["minimap"] = {"x": .02, "y": .04, "w": .2, "h": .2}
        self.config["regions"]["combat"] = {"x": .05, "y": .05, "w": .9, "h": .8}
        self.config["calibration"]["window_size"] = [1000, 500]
        self.config["recognition"].update({
            POINT: {"x": .4, "y": .5},
            POINT + "_space": "minimap",
            POINT + "_captured": True,
        })
        self.config["strategy"]["options"][STRATEGY]["attack_regions"] = [{
            "id": "region_1", "name": "龙咆哮范围", "priority": 1, "enabled": True,
            "space": "player_anchor_v1", "offset_x": -.2, "offset_y": -.3,
            "w": .4, "h": .4,
        }]
        for key in (POINT, self.region_key):
            self.config["calibration"]["items"][key] = {"complete": True}

    @contextmanager
    def capture_environment(self, selected):
        """All window, image acquisition and overlay operations are mocked."""
        with ExitStack() as stack:
            stack.enter_context(patch.object(calibrate, "find_game_window", return_value=self.window))
            focus = stack.enter_context(patch.object(calibrate, "focus_game_window"))
            stack.enter_context(patch.object(calibrate.mss, "MSS"))
            capture = stack.enter_context(
                patch.object(calibrate, "capture_client", return_value=self.frozen)
            )
            overlay = stack.enter_context(
                patch.object(calibrate, "interactive_overlay", return_value=selected)
            )
            yield capture, overlay, focus

    def test_point_capture_maps_frozen_magnified_preview_and_roundtrips(self):
        selected = MagicMock(cancelled=False, point=(300, 230))
        preview = np.ones_like(self.frozen)
        original_center = deepcopy(self.config["recognition"]["platform_center"])
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            save_config(path, self.config)
            with self.capture_environment(selected) as (capture, overlay, _focus), patch.object(
                calibrate, "magnified_roi_preview",
                return_value=(preview, (200, 80, 400, 200), 2.0),
            ) as magnify:
                result = calibrate.capture_minimap_point(path, POINT, "点击龙咆哮定点")
            saved = load_config(path)

        capture.assert_called_once()
        self.assertIs(magnify.call_args.args[0], self.frozen)
        self.assertEqual(magnify.call_args.args[1], (20, 20, 200, 100))
        self.assertIs(overlay.call_args.kwargs["frozen_frame"], preview)
        self.assertEqual(overlay.call_args.kwargs["guide_rect"], (200, 80, 400, 200))
        self.assertEqual(overlay.call_args.args[2], "point")
        self.assertIn("点击龙咆哮定点", overlay.call_args.args[1])
        self.assertEqual(result, {"x": .25, "y": .75})
        self.assertEqual(saved["recognition"][POINT], result)
        self.assertEqual(saved["recognition"][POINT + "_space"], "minimap")
        self.assertTrue(saved["recognition"][POINT + "_captured"])
        self.assertTrue(saved["calibration"]["items"][POINT]["complete"])
        self.assertEqual(saved["recognition"]["platform_center"], original_center)

    def test_cancelled_or_outside_point_does_not_replace_saved_capture(self):
        preview = np.ones_like(self.frozen)
        for selected in (
            MagicMock(cancelled=True, point=None),
            MagicMock(cancelled=False, point=(100, 100)),
        ):
            with self.subTest(cancelled=selected.cancelled), TemporaryDirectory() as temporary:
                path = Path(temporary) / "config.json"
                save_config(path, self.config)
                before = path.read_bytes()
                with self.capture_environment(selected), patch.object(
                    calibrate, "magnified_roi_preview",
                    return_value=(preview, (200, 80, 400, 200), 2.0),
                ):
                    with self.assertRaises(RuntimeError):
                        calibrate.capture_minimap_point(path, POINT, "点击龙咆哮定点")
                self.assertEqual(path.read_bytes(), before)

    def test_minimap_recapture_invalidates_point_but_preserves_attack_regions(self):
        selected = MagicMock(cancelled=False, rectangle=(10, 10, 180, 90))
        original_regions = deepcopy(self.config["strategy"]["options"][STRATEGY]["attack_regions"])
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            save_config(path, self.config)
            with self.capture_environment(selected):
                calibrate.capture_status_region(path, "minimap", "小地图")
            saved = load_config(path)

        self.assertFalse(saved["recognition"][POINT + "_captured"])
        self.assertFalse(saved["calibration"]["items"][POINT]["complete"])
        self.assertEqual(saved["strategy"]["options"][STRATEGY]["attack_regions"], original_regions)

    def test_combat_recapture_clears_attack_regions_but_preserves_minimap_point(self):
        selected = MagicMock(cancelled=False, rectangle=(20, 30, 900, 400))
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            save_config(path, self.config)
            with self.capture_environment(selected):
                calibrate.capture_combat_region(path)
            saved = load_config(path)

        self.assertEqual(saved["recognition"][POINT], {"x": .4, "y": .5})
        self.assertTrue(saved["recognition"][POINT + "_captured"])
        self.assertTrue(saved["calibration"]["items"][POINT]["complete"])
        self.assertEqual(saved["recognition"][POINT + "_space"], "minimap")
        self.assertEqual(saved["strategy"]["options"][STRATEGY]["attack_regions"], [])
        self.assertFalse(saved["calibration"]["items"][self.region_key]["complete"])

    def test_aspect_ratio_change_invalidates_point_and_ranges(self):
        calibrate._prepare_window_calibration(
            self.config, WindowInfo(123, "MockMaple", 0, 0, 1000, 800)
        )
        self.assertFalse(self.config["recognition"][POINT + "_captured"])
        self.assertFalse(self.config["calibration"]["items"][POINT]["complete"])
        self.assertEqual(self.config["strategy"]["options"][STRATEGY]["attack_regions"], [])

    def test_proportional_resize_preserves_point_and_ranges(self):
        calibrate._prepare_window_calibration(
            self.config, WindowInfo(123, "MockMaple", 0, 0, 1200, 600)
        )
        self.assertTrue(self.config["recognition"][POINT + "_captured"])
        self.assertEqual(len(self.config["strategy"]["options"][STRATEGY]["attack_regions"]), 1)

    def test_attack_region_capture_and_reframe_roundtrip_without_changing_point(self):
        selected = MagicMock(cancelled=False, rectangle=(120, 100, 300, 100))
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            save_config(path, self.config)
            with self.capture_environment(selected) as (capture, overlay, _focus):
                result = calibrate.capture_strategy_region(
                    path, STRATEGY, "attack_regions", self.region_key, "框选龙咆哮攻击范围",
                    region_id="region_1", player_anchor=(450., 200.),
                )
            saved = load_config(path)

        capture.assert_called_once()
        self.assertIs(overlay.call_args.kwargs["frozen_frame"], self.frozen)
        self.assertEqual(overlay.call_args.kwargs["guide_rect"], (50, 25, 900, 400))
        self.assertEqual(overlay.call_args.args[2], "rectangle")
        self.assertEqual(result["id"], "region_1")
        self.assertEqual(result["name"], "龙咆哮范围")
        self.assertEqual(result["space"], "player_anchor_v1")
        self.assertEqual(len(saved["strategy"]["options"][STRATEGY]["attack_regions"]), 1)
        self.assertEqual(saved["strategy"]["options"][STRATEGY]["attack_regions"][0], result)
        restored = player_relative_region_rect((450., 200.), 900, 400, result)
        for actual, expected in zip(restored, (70., 75., 370., 175.)):
            self.assertAlmostEqual(actual, expected, places=3)
        self.assertEqual(saved["recognition"][POINT], {"x": .4, "y": .5})
        self.assertTrue(saved["calibration"]["items"][self.region_key]["complete"])

    def test_capturing_current_profile_does_not_modify_other_profile(self):
        selected = MagicMock(cancelled=False, point=(400, 180))
        for profile in ("classic", "newmaple"):
            with self.subTest(profile=profile), TemporaryDirectory() as temporary:
                paths = {
                    "classic": Path(temporary) / "config.json",
                    "newmaple": Path(temporary) / "profiles" / "newmaple" / "config.json",
                }
                for name, path in paths.items():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    config = deepcopy(self.config)
                    config["profile"] = name
                    config["recognition"][POINT] = {"x": .2 if name == "classic" else .8, "y": .4}
                    save_config(path, config)
                other = paths["newmaple" if profile == "classic" else "classic"]
                untouched = other.read_bytes()
                with self.capture_environment(selected), patch.object(
                    calibrate, "magnified_roi_preview",
                    return_value=(np.ones_like(self.frozen), (200, 80, 400, 200), 2.0),
                ):
                    calibrate.capture_minimap_point(paths[profile], POINT, "点击龙咆哮定点")
                saved = load_config(paths[profile])
                self.assertEqual(saved["profile"], profile)
                self.assertEqual(saved["recognition"][POINT], {"x": .5, "y": .5})
                self.assertEqual(other.read_bytes(), untouched)

    def test_region_capture_errors_are_strategy_neutral_and_leave_config_unchanged(self):
        cases = (
            (MagicMock(cancelled=False, rectangle=(120, 100, 100, 100)), None, "尚未识别"),
            (MagicMock(cancelled=True, rectangle=None), (450., 200.), "取消"),
            (MagicMock(cancelled=False, rectangle=(0, 0, 100, 100)), (450., 200.), "完整位于"),
        )
        for selected, anchor, expected in cases:
            with self.subTest(error=expected), TemporaryDirectory() as temporary:
                path = Path(temporary) / "config.json"
                save_config(path, self.config)
                before = path.read_bytes()
                with self.capture_environment(selected):
                    with self.assertRaisesRegex(RuntimeError, expected) as caught:
                        calibrate.capture_strategy_region(
                            path, STRATEGY, "attack_regions", self.region_key,
                            "框选龙咆哮攻击范围", player_anchor=anchor,
                        )
                self.assertNotIn("标飞", str(caught.exception))
                self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
