from pathlib import Path
import unittest

from mbv.config import load_config, refresh_calibrated


class OptionalBarCalibrationTests(unittest.TestCase):
    def test_bars_do_not_block_base_calibration_in_either_profile(self):
        root = Path(__file__).resolve().parents[1]
        for path in (root / "config.example.json", root / "profiles/newmaple/config.example.json"):
            with self.subTest(path=path):
                config = load_config(path)
                items = config["calibration"]["items"]
                for key in ("minimap", "player_marker", "combat_region"):
                    items[key] = {"complete": True}
                for key in ("hp_bar", "mp_bar"):
                    items[key] = {"complete": False}
                self.assertTrue(refresh_calibrated(config))
                self.assertFalse(config["calibration"]["status_regions_complete"])
                for key in ("minimap", "player_marker", "combat_region"):
                    items[key]["complete"] = False
                    self.assertFalse(refresh_calibrated(config))
                    items[key]["complete"] = True
