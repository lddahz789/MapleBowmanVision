import json
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from mbv.config import load_config, save_config
from mbv.strategies import get_strategy, normalize_strategy_config
from mbv.strategies.base import StrategyActionContext, TargetSelectionContext
from mbv.vision import Detection


ROOT = Path(__file__).resolve().parents[1]


class StationaryMeleeTests(unittest.TestCase):
    def setUp(self):
        self.strategy = get_strategy("stationary_attack")
        self.context = StrategyActionContext(
            marker=(0.5, 0.5),
            player_box=(95, 100, 10, 1),
            player_anchor=(100.0, 100.0),
            target_box=(105, 90, 20, 20),
            chase_box=None,
            combat_width=400,
            combat_height=240,
            has_monster_candidates=True,
            now=10.0,
            last_target_seen=9.0,
            last_pickup=0.0,
            direction="right",
            behavior={},
            settings={**self.strategy.default_settings, "melee_skill_key": "M"},
            recognition={"platform_center": {"x": 0.5, "y": 0.5}},
        )

    def decide(self, **changes):
        return self.strategy.decide(replace(self.context, **changes))

    def test_fields_support_capture_and_direct_distance_input(self):
        fields = {field.path: field for field in self.strategy.setting_fields}
        self.assertTrue(fields["melee_skill_key"].capture_key)
        for name in ("melee_enter_distance_multiplier", "melee_exit_distance_multiplier"):
            self.assertTrue(fields[name].direct_numeric_input)

    def test_old_config_defaults_are_disabled_and_independent_of_dynamic(self):
        config = {"strategy": {"active": "stationary_attack", "options": {
            "bowman_dynamic": {"melee_skill_key": "q"},
            "stationary_attack": {"periodic_step_seconds": 0.2},
        }}}
        normalize_strategy_config(config)
        settings = config["strategy"]["options"]["stationary_attack"]
        self.assertEqual(settings["melee_skill_key"], "")
        self.assertEqual(settings["melee_enter_distance_multiplier"], 0.35)
        self.assertEqual(settings["melee_exit_distance_multiplier"], 0.9)
        self.assertEqual(settings["periodic_step_seconds"], 0.2)
        self.assertEqual(config["strategy"]["options"]["bowman_dynamic"]["melee_skill_key"], "q")

    def test_settings_sanitize_invalid_values_and_order_thresholds(self):
        cases = [
            (None, float("nan"), float("inf"), "", 0.35, 0.9),
            (True, True, "bad", "", 0.35, 0.9),
            (" M ", 2, 0.1, "m", 2.0, 2.0),
            ("q", -1, 100, "q", 0.0, 5.0),
            ("q", 100, -1, "q", 3.0, 3.0),
        ]
        for key, enter, leave, expected_key, expected_enter, expected_leave in cases:
            with self.subTest(key=key, enter=enter, leave=leave):
                settings = {"melee_skill_key": key, "melee_enter_distance_multiplier": enter,
                            "melee_exit_distance_multiplier": leave}
                self.strategy.normalize_settings(settings)
                self.assertEqual(settings, {"melee_skill_key": expected_key,
                    "melee_enter_distance_multiplier": expected_enter,
                    "melee_exit_distance_multiplier": expected_leave})

    def test_capture_and_clear_persist_without_changing_dynamic_skill(self):
        from mbv.panel import ControlPanel

        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            config = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
            config["strategy"]["options"]["bowman_dynamic"]["melee_skill_key"] = "q"
            save_config(path, config)
            panel = ControlPanel.__new__(ControlPanel)
            panel.config_path = path
            panel.root = MagicMock()
            panel._run_tool = lambda _title, action: action()
            for captured, expected in (("M", "m"), ("", "")):
                with patch("mbv.panel.capture_key_name", return_value=captured):
                    panel._capture_key("strategy.options.stationary_attack.melee_skill_key")
                loaded = load_config(path)
                settings = loaded["strategy"]["options"]["stationary_attack"]
                self.assertEqual(settings["melee_skill_key"], expected)
                self.assertEqual(loaded["strategy"]["options"]["bowman_dynamic"]["melee_skill_key"], "q")
            settings["melee_enter_distance_multiplier"] = 0.5
            settings["melee_exit_distance_multiplier"] = 1.2
            save_config(path, loaded)
            self.assertEqual(load_config(path)["strategy"]["options"]["stationary_attack"], settings)

    def test_close_target_on_either_side_uses_melee_without_repeated_facing(self):
        for box in ((105, 90, 20, 20), (75, 90, 20, 20), (90, 90, 20, 20)):
            with self.subTest(box=box):
                decision = self.decide(target_box=box)
                self.assertEqual((decision.action, decision.state, decision.attack_skill, decision.attack_key),
                                 ("attack", "ATTACK_MELEE", "melee", "m"))
                self.assertFalse(decision.face_each_attack)
                self.assertTrue(decision.target_seen)

    def test_hysteresis_enter_exit_and_exact_boundaries(self):
        for x, previous, expected in (
            (107, None, "melee"), (108, None, "single"),
            (114, None, "single"), (114, "melee", "melee"),
            (118, "melee", "melee"), (119, "melee", "single"),
        ):
            with self.subTest(x=x, previous=previous):
                decision = self.decide(target_box=(x, 90, 20, 20), previous_attack_skill=previous)
                self.assertEqual(decision.attack_skill, expected)
                self.assertEqual(decision.attack_key, "m" if expected == "melee" else None)

    def test_distance_scales_with_monster_width_and_uses_stable_anchor(self):
        for width in (20, 40, 80):
            with self.subTest(width=width):
                decision = self.decide(target_box=(100 + width // 4, 90, width, 20),
                                       player_box=(0, 0, 10, 1))
                self.assertEqual(decision.attack_skill, "melee")
        self.assertEqual(self.decide(player_anchor=None).attack_skill, "melee")

    def test_cleared_key_immediately_disables_active_melee(self):
        for key in ("", "  ", None):
            with self.subTest(key=key):
                decision = self.decide(settings={**self.context.settings, "melee_skill_key": key},
                                       previous_attack_skill="melee")
                self.assertEqual((decision.attack_skill, decision.attack_key), ("single", None))

    def test_custom_thresholds_change_trigger(self):
        settings = {**self.context.settings, "melee_enter_distance_multiplier": 0.1,
                    "melee_exit_distance_multiplier": 0.2}
        self.assertEqual(self.decide(settings=settings).attack_skill, "single")
        self.assertEqual(self.decide(settings=settings, previous_attack_skill="melee").attack_skill, "single")
        settings["melee_enter_distance_multiplier"] = 0.5
        self.assertEqual(self.decide(settings=settings).attack_skill, "melee")

    def test_safety_return_and_periodic_step_take_priority_over_melee(self):
        cases = [
            ({"player_box": None}, "stop", "PLAYER_SCREEN_LOST"),
            ({"marker": None}, "stop", "MARKER_LOST"),
            ({"marker": (0.6, 0.5)}, "move", "RETURN_CENTER_LEFT"),
            ({"marker": (0.4, 0.5)}, "move", "RETURN_CENTER_RIGHT"),
            ({"marker": (0.5, 0.7)}, "jump", "RETURN_CENTER_JUMP_UP"),
            ({"marker": (0.5, 0.3)}, "down_jump", "RETURN_CENTER_DOWN_JUMP"),
            ({"periodic_step_pending_return": True}, "stop", "PERIODIC_STEP_RETURNED"),
            ({"now": 45.0}, "step", "PERIODIC_STEP_RIGHT"),
        ]
        for changes, action, state in cases:
            with self.subTest(changes=changes):
                decision = self.decide(**changes)
                self.assertEqual((decision.action, decision.state), (action, state))
                self.assertIsNone(decision.attack_key)

    def test_missing_target_never_blindly_casts_or_chases(self):
        for candidates, state in ((True, "TARGET_OUT_OF_RANGE"), (False, "SCANNING")):
            decision = self.decide(target_box=None, chase_box=(105, 90, 20, 20),
                                   has_monster_candidates=candidates, previous_attack_skill="melee")
            self.assertEqual((decision.action, decision.state), ("stop", state))
            self.assertIsNone(decision.attack_key)

    def test_target_selection_keeps_bidirectional_range_and_height_filter(self):
        for close in (Detection((105, 90, 20, 20), 0.9, "right"),
                      Detection((75, 90, 20, 20), 0.9, "left")):
            with self.subTest(target=close.name):
                selected = self.strategy.select_targets(TargetSelectionContext(
                    detections=[Detection((95, 10, 10, 10), 0.99, "upper"),
                                Detection((300, 90, 20, 20), 0.99, "far"), close],
                    player_box=self.context.player_box,
                    player_raw_box=(95, 0, 10, 10),
                    player_anchor=self.context.player_anchor,
                    scene_width=400, scene_height=240, facing="right",
                    target_area={"forward": 0.2, "back": 0.05, "up": 0.1, "down": 0.1},
                    settings=self.context.settings,
                ))
                self.assertIs(selected.target, close)
                self.assertIsNone(selected.chase_target)
                self.assertEqual(self.decide(target_box=selected.target.box).attack_skill, "melee")

    def test_executor_uses_melee_then_common_attack_key_without_repeated_turns(self):
        from mbv.bot import BowmanBot

        instance = BowmanBot.__new__(BowmanBot)
        instance.config = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
        instance.keyboard = MagicMock()
        instance.log = MagicMock()
        instance.direction = None
        instance.last_attack = 0.0
        with patch("mbv.bot.time.monotonic", return_value=9.0):
            instance.face_and_attack(0.8, 0.5, 9.0, face_each_attack=False)
        for now, box, previous in ((10.0, (105, 90, 20, 20), None),
                                   (11.0, (114, 90, 20, 20), "melee"),
                                   (12.0, (130, 90, 20, 20), "melee")):
            decision = self.decide(target_box=box, previous_attack_skill=previous)
            with patch("mbv.bot.time.monotonic", return_value=now):
                instance.face_and_attack(decision.target_x, decision.player_x, now,
                                         face_each_attack=decision.face_each_attack,
                                         attack_key=decision.attack_key, attack_skill=decision.attack_skill)
        keys = [call.args[0] for call in instance.keyboard.tap.call_args_list]
        self.assertEqual(keys, ["right", "m", "m", instance.config["keys"]["attack"]])
        instance.keyboard.down.assert_not_called()


if __name__ == "__main__":
    unittest.main()
