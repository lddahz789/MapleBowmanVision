from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import MagicMock, call, patch

import numpy as np

from mbv import calibrate
from mbv.bot import BowmanBot
from mbv.config import load_config, save_config
from mbv.strategies import get_strategy, missing_recognition_data, normalize_strategy_config
from mbv.strategies.base import StrategyActionContext, TargetSelectionContext
from mbv.vision import Detection
from mbv.window import WindowInfo


ROOT = Path(__file__).resolve().parents[1]
POINT = "stationary_pickup_point"


def recognition():
    return {"platform_center": {"x": .5, "y": .5}, "platform_center_captured": True,
            "platform_center_space": "minimap", POINT: {"x": .8, "y": .5},
            POINT + "_captured": True, POINT + "_space": "minimap"}


class RouteDecisionTests(unittest.TestCase):
    def setUp(self):
        self.strategy = get_strategy("stationary_attack")
        self.context = StrategyActionContext(
            marker=(.5, .5), player_box=(90, 100, 20, 1), player_anchor=(100., 100.),
            target_box=None, chase_box=None, combat_width=400, has_monster_candidates=False,
            now=181., started_at=1., last_target_seen=0., last_pickup=0., direction="right",
            behavior={}, settings={**self.strategy.default_settings, "route_pickup_enabled": True},
            recognition=recognition(), last_periodic_step=156.,
        )

    def decide(self, **changes):
        return self.strategy.decide(replace(self.context, **changes))

    def advance(self, **changes):
        self.context = replace(self.context, **changes)
        result = self.strategy.decide(self.context)
        if result.runtime_state is not None:
            self.context = replace(self.context, runtime_state=result.runtime_state)
        return result

    def finish_collection(self):
        for _ in range(120):
            previous_time = self.context.now
            result = self.advance(now=previous_time + .1, last_pickup=previous_time,
                                  target_box=None, action_interrupted=False)
            if result.state == "PICKUP_START_RETURN":
                return result
        self.fail("持续有效拾取后没有进入返程")

    def test_disabled_does_not_require_point_or_change_old_behavior(self):
        settings = {**self.context.settings, "route_pickup_enabled": False}
        self.assertEqual(self.decide(settings=settings, recognition={"platform_center": {"x": .5, "y": .5}}).state, "SCANNING")
        self.assertEqual(self.decide(settings=settings, now=201.).action, "step")

    def test_timer_begins_at_arming_and_route_wins_when_both_due(self):
        self.assertEqual(self.decide(now=180.9).action, "stop")
        route = self.decide(last_periodic_step=0.)
        self.assertEqual((route.action, route.direction), ("move", "right"))
        self.assertEqual(route.runtime_state["phase"], "outbound")
        self.assertFalse(route.reset_periodic_step)
        self.assertEqual(self.context.runtime_state, {})

    def test_route_must_start_at_home_and_after_previous_step_return(self):
        self.assertEqual(self.decide(marker=(.6, .5)).state, "RETURN_CENTER_LEFT")
        self.assertEqual(self.decide(periodic_step_pending_return=True).state, "PERIODIC_STEP_RETURNED")

    def test_outbound_bypasses_return_and_supports_both_directions(self):
        self.advance()
        result = self.advance(marker=(.65, .5), now=182.)
        self.assertEqual(result.state, "PICKUP_OUTBOUND_RIGHT")
        self.assertEqual(result.pickup_interval_seconds, .15)
        config = recognition()
        config[POINT]["x"] = .2
        result = self.decide(recognition=config, runtime_state={})
        self.assertEqual(result.direction, "left")

    def test_outbound_attacks_then_continues_without_step(self):
        self.advance()
        result = self.advance(marker=(.65, .5), target_box=(105, 90, 20, 20), now=210.,
                              settings={**self.context.settings, "melee_skill_key": "m"})
        self.assertEqual((result.action, result.attack_key), ("attack", "m"))
        self.assertIsNone(result.pickup_interval_seconds)
        result = self.advance(target_box=None, now=211.)
        self.assertEqual(result.state, "PICKUP_CLEAR_WAIT")
        result = self.advance(now=211.5)
        self.assertEqual(result.state, "PICKUP_OUTBOUND_RIGHT")

    def test_dwell_requires_pickup_and_excludes_combat(self):
        self.advance()
        self.assertEqual(self.advance(marker=(.8, .5), now=182.).action, "pickup")
        self.assertEqual(self.advance(now=184.).action, "pickup")
        self.assertEqual(self.advance(now=185., target_box=(120, 90, 20, 20)).action, "attack")
        self.assertEqual(self.advance(now=186., target_box=None, last_pickup=183.).state, "PICKUP_CLEAR_WAIT")
        self.assertEqual(self.advance(now=186.5).action, "pickup")
        self.assertEqual(self.advance(now=188., last_pickup=187.).state, "PICKUP_COLLECT")
        self.assertEqual(self.finish_collection().state, "PICKUP_START_RETURN")

    def test_return_clears_monsters_before_moving_and_completion_resets_timers(self):
        self.advance()
        self.advance(marker=(.8, .5), now=182.)
        self.finish_collection()
        result = self.advance(now=185., target_box=(120, 90, 20, 20))
        self.assertEqual(result.action, "attack")
        self.assertEqual(result.runtime_state["phase"], "returning")
        self.assertIsNone(result.pickup_interval_seconds)
        self.assertEqual(self.advance(now=185.1, target_box=None).state, "PICKUP_CLEAR_WAIT")
        result = self.advance(now=185.6)
        self.assertEqual(result.state, "RETURN_CENTER_LEFT")
        self.assertIsNone(result.attack_key)
        result = self.advance(marker=(.5, .5), now=188.)
        self.assertEqual(result.state, "PICKUP_RETURNED")
        self.assertTrue(result.reset_periodic_step)
        self.assertEqual(result.runtime_state["last_completed_at"], 188.)
        self.context = replace(self.context, target_box=(120, 90, 20, 20))
        self.assertEqual(self.advance(now=232.9, last_periodic_step=188.).action, "attack")
        self.assertEqual(self.advance(now=233.).action, "step")
        self.assertEqual(self.decide(now=367.9, last_periodic_step=367.).action, "attack")
        self.assertEqual(self.decide(now=368., target_box=None).state, "PICKUP_OUTBOUND_RIGHT")

    def test_disabling_midroute_returns_without_pickup_then_stays_disabled(self):
        self.advance()
        result = self.advance(marker=(.65, .5), settings={**self.context.settings, "route_pickup_enabled": False})
        self.assertEqual(result.state, "RETURN_CENTER_LEFT")
        self.assertIsNone(result.pickup_interval_seconds)
        self.assertEqual(result.runtime_state["phase"], "returning")
        self.assertEqual(self.advance(marker=(.5, .5)).state, "PICKUP_RETURNED")
        self.assertEqual(self.decide(now=1000., last_periodic_step=999.).state, "SCANNING")

    def test_timeout_selects_return_phase_but_still_clears_monsters_first(self):
        self.advance()
        result = self.advance(marker=(.65, .5), now=271., target_box=(120, 90, 20, 20))
        self.assertEqual(result.action, "attack")
        self.assertEqual(result.runtime_state["phase"], "returning")
        self.assertEqual(result.runtime_state["return_reason"], "outbound_timeout")

    def test_no_displacement_does_not_postpone_periodic_step(self):
        self.advance(target_box=(120, 90, 20, 20))
        self.advance(now=270., target_box=None)
        result = self.advance(now=271.)
        self.assertEqual(result.state, "PICKUP_RETURNED")
        self.assertFalse(result.reset_periodic_step)

    def test_minimap_only_waits_then_returns_after_grace_without_pickup_or_attack(self):
        self.advance()
        result = self.advance(marker=(.65, .5), player_box=None, player_anchor=None,
                              target_box=(120, 90, 20, 20), minimap_only=True)
        self.assertEqual(result.state, "PICKUP_WAIT_LOCALIZATION")
        self.assertIsNone(result.pickup_interval_seconds)
        result = self.advance(now=182., localization_lost_seconds=1.)
        self.assertEqual(result.state, "RETURN_CENTER_LEFT")
        self.assertEqual(result.runtime_state["return_reason"], "localization_timeout")
        self.assertEqual(self.decide(marker=(.5, .5)).state, "PICKUP_RETURNED")

    def test_short_visual_loss_resumes_same_outbound_route(self):
        self.advance()
        started = self.context.runtime_state["route_started_at"]
        self.advance(now=182., marker=(.65, .5), player_box=None, minimap_only=True)
        result = self.advance(now=182.25, player_box=(90, 100, 20, 1), minimap_only=False,
                              localization_lost_seconds=.25)
        self.assertEqual(result.state, "PICKUP_OUTBOUND_RIGHT")
        self.assertEqual(result.runtime_state["route_started_at"], started)
        self.assertNotIn("return_reason", result.runtime_state)

    def test_separate_outbound_timeout_does_not_consume_destination_dwell(self):
        self.advance(settings={**self.context.settings, "route_pickup_timeout_seconds": 15.,
                               "route_pickup_dwell_seconds": 7.})
        self.advance(now=195., marker=(.8, .5))  # 出发 14 秒后到达。
        result = self.advance(now=196., last_pickup=195.)
        self.assertEqual(result.state, "PICKUP_COLLECT")
        self.assertEqual(self.finish_collection().runtime_state["return_reason"], "collected")
        self.assertGreaterEqual(self.context.now, 202.)

    def test_collection_pauses_accumulator_for_combat_and_short_loss(self):
        self.advance()
        self.advance(now=182., marker=(.8, .5))
        self.advance(now=182.4, last_pickup=182.)
        accrued = self.context.runtime_state["dwell_elapsed"]
        self.assertAlmostEqual(accrued, .4)
        self.advance(now=182.5, target_box=(120, 90, 20, 20))
        self.advance(now=190., target_box=None, last_pickup=182.4)
        self.assertAlmostEqual(self.context.runtime_state["dwell_elapsed"], accrued)
        self.advance(now=190.1, player_box=None, minimap_only=True)
        self.advance(now=190.35, player_box=(90, 100, 20, 1), minimap_only=False,
                     localization_lost_seconds=.25, last_pickup=190.)
        self.assertAlmostEqual(self.context.runtime_state["dwell_elapsed"], accrued)
        self.assertEqual(self.finish_collection().state, "PICKUP_START_RETURN")

    def test_upstream_interruptions_and_missing_input_do_not_count_as_pickup(self):
        self.advance()
        self.advance(now=182., marker=(.8, .5))
        self.advance(now=182.4, last_pickup=182., action_interrupted=True)
        self.assertEqual(self.context.runtime_state["dwell_elapsed"], 0.)
        self.advance(now=182.8, action_interrupted=False, last_pickup=0.)
        self.assertEqual(self.context.runtime_state["dwell_elapsed"], 0.)

    def test_destination_stage_has_independent_timeout_for_persistent_monsters(self):
        self.advance()
        self.advance(now=182., marker=(.8, .5))
        result = self.advance(now=242., target_box=(120, 90, 20, 20))
        self.assertEqual(result.action, "attack")
        self.assertEqual(result.runtime_state["phase"], "returning")
        self.assertEqual(result.runtime_state["return_reason"], "collection_timeout")

    def test_disabling_and_falling_take_priority_over_visual_grace(self):
        self.advance()
        result = self.decide(marker=(.65, .5), player_box=None, minimap_only=True,
                             settings={**self.context.settings, "route_pickup_enabled": False})
        self.assertEqual(result.runtime_state["return_reason"], "disabled")
        result = self.decide(marker=(.65, .7), player_box=None, minimap_only=True)
        self.assertEqual(result.runtime_state["return_reason"], "off_platform")

    def test_lost_marker_or_visual_cannot_continue_route(self):
        self.advance()
        self.assertEqual(self.decide(marker=None).state, "MARKER_LOST")
        self.assertEqual(self.decide(player_box=None).state, "PLAYER_SCREEN_LOST")
        self.assertEqual(self.context.runtime_state["phase"], "outbound")

    def test_off_platform_aborts_route(self):
        self.advance()
        result = self.advance(marker=(.65, .7))
        self.assertEqual(result.runtime_state["phase"], "returning")
        self.assertEqual(result.state, "RETURN_CENTER_LEFT")

    def test_invalid_points_stop_departure_but_never_block_safety_return(self):
        for point in (None, {}, {"x": float("nan"), "y": .5}, {"x": 1.1, "y": .5},
                      {"x": .8, "y": .9}, {"x": .5, "y": .5}):
            with self.subTest(point=point):
                config = {**recognition(), POINT: point}
                self.assertEqual(self.decide(recognition=config).state, "PICKUP_POINT_INVALID")
                self.assertEqual(self.decide(recognition=config, marker=(.6, .5)).state, "RETURN_CENTER_LEFT")

    def test_instances_do_not_share_state(self):
        before = deepcopy(vars(self.strategy))
        self.advance()
        self.assertEqual(vars(self.strategy), before)
        self.assertEqual(self.decide(runtime_state={}, now=2.).state, "SCANNING")

    def test_both_route_directions_wait_through_one_empty_frame_and_resume(self):
        for phase in ("outbound", "returning"):
            with self.subTest(phase=phase):
                self.context = replace(self.context, now=182., marker=(.65, .5),
                    runtime_state={"phase": phase, "route_started_at": 181.,
                                   "return_reason": "collected", "navigation_active": True})
                self.assertEqual(self.advance(target_box=(120, 90, 20, 20)).action, "attack")
                self.assertEqual(self.advance(now=182.1, target_box=None).state, "PICKUP_CLEAR_WAIT")
                self.assertEqual(self.advance(now=182.2, target_box=(120, 90, 20, 20)).action, "attack")
                self.assertEqual(self.advance(now=182.3, target_box=None).state, "PICKUP_CLEAR_WAIT")
                self.assertEqual(self.advance(now=182.6).action, "stop")
                resumed = self.advance(now=182.8)
                self.assertEqual((resumed.action, resumed.direction),
                                 ("move", "right" if phase == "outbound" else "left"))

    def test_return_melee_and_home_completion_wait_for_clear(self):
        state = {"phase": "returning", "return_reason": "collected", "departed": True}
        self.context = replace(self.context, runtime_state=state,
                               settings={**self.context.settings, "melee_skill_key": "m"})
        result = self.advance(target_box=(105, 90, 20, 20))
        self.assertEqual((result.action, result.attack_key), ("attack", "m"))
        self.assertFalse(result.reset_periodic_step)
        self.assertEqual(self.advance(now=181.1, target_box=None).state, "PICKUP_CLEAR_WAIT")
        done = self.advance(now=181.6)
        self.assertEqual(done.state, "PICKUP_RETURNED")
        self.assertTrue(done.reset_periodic_step)

    def test_safety_returns_do_not_stop_for_monsters(self):
        for reason in ("disabled", "point_invalid", "off_platform", "localization_timeout"):
            result = self.decide(marker=(.65, .5), target_box=(120, 90, 20, 20),
                runtime_state={"phase": "returning", "return_reason": reason})
            self.assertEqual(result.state, "RETURN_CENTER_LEFT")
            self.assertIsNone(result.attack_key)

    def test_map_only_return_never_attacks_even_with_target_in_context(self):
        result = self.decide(marker=(.65, .5), player_box=None, minimap_only=True,
            target_box=(120, 90, 20, 20),
            runtime_state={"phase": "returning", "return_reason": "collected"})
        self.assertEqual(result.state, "RETURN_CENTER_LEFT")
        self.assertIsNone(result.pickup_interval_seconds)

    def test_route_clears_current_side_even_when_other_side_becomes_nearer(self):
        left = Detection((50, 90, 20, 20), .9, "left")
        right = Detection((110, 90, 20, 20), .9, "right")
        for phase in ("outbound", "collect", "returning"):
            with self.subTest(phase=phase):
                state = {"phase": phase, "route_started_at": 181., "return_reason": "collected"}
                first = self.decide(target_box=left.box, runtime_state=state)
                result = self.decide(target_box=right.box, eligible_detections=(left, right),
                                     now=181.2, runtime_state=first.runtime_state)
                self.assertEqual(result.target_x, .15)
                self.assertEqual(result.runtime_state["combat_side"], "left")
                self.assertEqual(result.face_tap_seconds, .08)
                self.assertIsNone(result.pickup_interval_seconds)

    def test_side_loss_waits_then_switches_using_fresh_target_not_stale_box(self):
        first = self.advance(target_box=(50, 90, 20, 20))
        self.assertEqual(first.runtime_state["combat_side"], "left")
        self.assertEqual(self.advance(now=181.1, target_box=(110, 90, 20, 20)).state, "PICKUP_CLEAR_WAIT")
        self.assertEqual(self.advance(now=181.3).state, "PICKUP_CLEAR_WAIT")
        result = self.advance(now=181.51, target_box=(130, 90, 20, 20))
        self.assertEqual(result.target_x, .35)
        self.assertEqual(result.runtime_state["combat_side"], "right")

    def test_reappearing_side_and_interruption_reset_side_loss_confirmation(self):
        left = Detection((50, 90, 20, 20), .9, "left")
        self.advance(target_box=left.box)
        self.advance(now=181.1, target_box=(110, 90, 20, 20))
        result = self.advance(now=181.3, eligible_detections=(left,))
        self.assertEqual(result.runtime_state["combat_side"], "left")
        self.assertNotIn("combat_side_missing_since", result.runtime_state)
        self.advance(now=181.4, eligible_detections=())
        self.assertEqual(self.advance(now=182., action_interrupted=True).action, "stop")
        self.assertEqual(self.advance(now=182.2, action_interrupted=False).action, "stop")
        self.assertEqual(self.advance(now=182.5).runtime_state["combat_side"], "right")

    def test_clear_episode_discards_side_lock_before_next_move(self):
        self.advance(target_box=(50, 90, 20, 20))
        self.advance(now=181.1, target_box=None)
        result = self.advance(now=181.6)
        self.assertEqual(result.action, "move")
        self.assertNotIn("combat_side", result.runtime_state)

    def test_return_combat_short_visual_loss_does_not_insert_opposite_move(self):
        state = {"phase": "returning", "return_reason": "collected", "combat_pending": True}
        result = self.decide(marker=(.65, .5), player_box=None, minimap_only=True,
                             runtime_state=state, localization_lost_seconds=.2)
        self.assertEqual(result.state, "PICKUP_WAIT_LOCALIZATION")
        self.assertEqual(result.action, "stop")
        result = self.decide(marker=(.65, .5), player_box=None, minimap_only=True,
                             runtime_state=state, localization_lost_seconds=1.1)
        self.assertEqual(result.state, "RETURN_CENTER_LEFT")
        result = self.decide(marker=(.65, .5), player_box=None, minimap_only=True,
                             runtime_state={**state, "return_reason": "disabled"})
        self.assertEqual(result.state, "RETURN_CENTER_LEFT")

    def test_selection_exports_only_current_bidirectional_eligible_detections(self):
        left = Detection((50, 90, 20, 20), .9, "left")
        right = Detection((110, 90, 20, 20), .9, "right")
        outside = Detection((390, 90, 20, 20), .9, "outside")
        upstairs = Detection((110, 0, 20, 20), .9, "upstairs")
        context = TargetSelectionContext(
            detections=[left, right, outside, upstairs], player_box=(90, 100, 20, 1),
            player_raw_box=None, player_anchor=(100., 100.), scene_width=400, scene_height=200,
            facing="right", target_area={"forward": .2, "back": .01, "up": .1, "down": .1},
            settings=self.context.settings)
        selection = self.strategy.select_targets(context)
        self.assertEqual(selection.eligible_detections, (left, right))
        self.assertEqual(selection.target, right)
        self.assertEqual(self.strategy.select_targets(replace(context, player_box=None)).eligible_detections, ())
        # 没进入路线的普通原地攻击不锁侧，也不请求路线转向时序。
        result = self.decide(now=10., target_box=right.box, eligible_detections=(left, right),
                             runtime_state={"combat_side": "left"})
        self.assertEqual(result.target_x, .3)
        self.assertIsNone(result.face_tap_seconds)

    def test_buff_interruption_does_not_count_as_clear_confirmation(self):
        self.advance(target_box=(120, 90, 20, 20))
        self.advance(now=181.1, target_box=None)
        self.assertEqual(self.advance(now=183., action_interrupted=True).state, "PICKUP_CLEAR_WAIT")
        self.assertEqual(self.advance(now=183.2, action_interrupted=False).state, "PICKUP_CLEAR_WAIT")
        self.assertEqual(self.advance(now=183.5).state, "PICKUP_OUTBOUND_RIGHT")


class RouteCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(ROOT / "config.example.json")
        self.config["recognition"].update(recognition())
        self.strategy = get_strategy("stationary_attack")
        self.config["strategy"]["active"] = self.strategy.key

    def test_defaults_and_direct_input_metadata(self):
        settings = self.config["strategy"]["options"][self.strategy.key]
        self.assertFalse(settings["route_pickup_enabled"])
        self.assertEqual(settings["route_pickup_interval_seconds"], 180.)
        fields = {f.path: f for f in self.strategy.setting_fields}
        self.assertTrue(fields["route_pickup_interval_seconds"].direct_numeric_input)
        settings.update(route_pickup_enabled="false", route_pickup_interval_seconds=float("nan"),
                        route_pickup_dwell_seconds=-10, route_pickup_timeout_seconds=10000)
        normalize_strategy_config(self.config)
        self.assertFalse(settings["route_pickup_enabled"])
        self.assertEqual(settings["route_pickup_interval_seconds"], 180.)
        self.assertEqual(settings["route_pickup_dwell_seconds"], .3)
        self.assertEqual(settings["route_pickup_timeout_seconds"], 600.)

    def test_capture_required_only_when_enabled_and_valid(self):
        settings = self.config["strategy"]["options"][self.strategy.key]
        self.config["recognition"][POINT + "_captured"] = False
        self.assertNotIn(POINT, missing_recognition_data(self.config, self.strategy))
        settings["route_pickup_enabled"] = True
        self.assertIn(POINT, missing_recognition_data(self.config, self.strategy))
        self.config["recognition"].update(recognition())
        self.assertNotIn(POINT, missing_recognition_data(self.config, self.strategy))
        self.config["recognition"][POINT + "_space"] = "combat"
        self.assertIn(POINT, missing_recognition_data(self.config, self.strategy))

    def test_new_timers_are_bounded_and_existing_values_survive_roundtrip(self):
        settings = self.config["strategy"]["options"][self.strategy.key]
        settings.update(route_pickup_dwell_seconds=7., route_pickup_timeout_seconds=15.,
                        route_pickup_visual_grace_seconds=float("nan"),
                        route_pickup_collect_timeout_seconds=-1.)
        self.strategy.normalize_settings(settings)
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            save_config(path, self.config)
            saved = load_config(path)["strategy"]["options"][self.strategy.key]
        self.assertEqual(saved["route_pickup_dwell_seconds"], 7.)
        self.assertEqual(saved["route_pickup_timeout_seconds"], 15.)
        self.assertEqual(saved["route_pickup_visual_grace_seconds"], 1.)
        self.assertEqual(saved["route_pickup_collect_timeout_seconds"], 10.)
        settings.update(route_pickup_dwell_seconds=10., route_pickup_collect_timeout_seconds=10.,
                        route_pickup_visual_grace_seconds=100.)
        self.strategy.normalize_settings(settings)
        self.assertEqual(settings["route_pickup_collect_timeout_seconds"], 11.)
        self.assertEqual(settings["route_pickup_visual_grace_seconds"], 3.)

    def test_magnified_point_is_independent_and_saved_in_current_profile(self):
        selected = MagicMock(cancelled=False, point=(500, 200))
        window = WindowInfo(123, "NewMaple", 0, 0, 1000, 500)
        frozen = np.zeros((500, 1000, 3), np.uint8)
        preview = np.ones_like(frozen)
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            save_config(path, self.config)
            with patch.object(calibrate, "find_game_window", return_value=window), \
                 patch.object(calibrate, "focus_game_window"), patch.object(calibrate.mss, "MSS"), \
                 patch.object(calibrate, "capture_client", return_value=frozen), \
                 patch.object(calibrate, "magnified_roi_preview", return_value=(preview, (300, 100, 400, 200), 4.)), \
                 patch.object(calibrate, "interactive_overlay", return_value=selected) as overlay:
                result = calibrate.capture_minimap_point(path, POINT, "目标拾取点")
            saved = load_config(path)
        self.assertEqual(result, {"x": .5, "y": .5})
        self.assertIs(overlay.call_args.kwargs["frozen_frame"], preview)
        self.assertEqual(overlay.call_args.args[2], "point")
        self.assertEqual(saved["recognition"][POINT + "_space"], "minimap")
        self.assertTrue(saved["calibration"]["items"][POINT]["complete"])
        self.assertEqual(saved["recognition"]["platform_center"], recognition()["platform_center"])

    def test_minimap_recapture_and_aspect_change_invalidate_point_combat_does_not(self):
        calibrate._invalidate_combat_dependents(self.config)
        self.assertTrue(self.config["recognition"][POINT + "_captured"])
        calibrate._invalidate_strategy_minimap_points(self.config)
        self.assertFalse(self.config["recognition"][POINT + "_captured"])
        self.config["recognition"].update(recognition())
        self.config["calibration"]["window_size"] = [1000, 500]
        calibrate._prepare_window_calibration(self.config, WindowInfo(123, "NewMaple", 0, 0, 1000, 800))
        self.assertFalse(self.config["recognition"][POINT + "_captured"])

    def test_minimap_recapture_entrypoint_invalidates_pickup_sample(self):
        selected = MagicMock(cancelled=False, rectangle=(10, 10, 200, 100))
        window = WindowInfo(123, "NewMaple", 0, 0, 1000, 500)
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            save_config(path, self.config)
            with patch.object(calibrate, "find_game_window", return_value=window), \
                 patch.object(calibrate, "focus_game_window"), patch.object(calibrate.mss, "MSS"), \
                 patch.object(calibrate, "capture_client", return_value=np.zeros((500, 1000, 3), np.uint8)), \
                 patch.object(calibrate, "interactive_overlay", return_value=selected):
                calibrate.capture_status_region(path, "minimap", "小地图")
            saved = load_config(path)
        self.assertFalse(saved["recognition"][POINT + "_captured"])
        self.assertFalse(saved["calibration"]["items"][POINT]["complete"])

    def test_cancel_point_sampling_keeps_existing_point(self):
        selected = MagicMock(cancelled=True, point=None)
        window = WindowInfo(123, "NewMaple", 0, 0, 1000, 500)
        frozen = np.zeros((500, 1000, 3), np.uint8)
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            save_config(path, self.config)
            before = path.read_bytes()
            with patch.object(calibrate, "find_game_window", return_value=window), \
                 patch.object(calibrate, "focus_game_window"), patch.object(calibrate.mss, "MSS"), \
                 patch.object(calibrate, "capture_client", return_value=frozen), \
                 patch.object(calibrate, "interactive_overlay", return_value=selected):
                with self.assertRaisesRegex(RuntimeError, "取消"):
                    calibrate.capture_minimap_point(path, POINT, "拾取点")
            self.assertEqual(path.read_bytes(), before)

    def test_toggle_persists_without_disarming_midroute(self):
        from mbv.panel import ControlPanel
        panel = ControlPanel.__new__(ControlPanel)
        panel.bot = MagicMock()
        panel.bot.strategy = self.strategy
        panel._selected_strategy = MagicMock(return_value=self.strategy)
        variable = MagicMock()
        variable.get.return_value = False
        with TemporaryDirectory() as tmp:
            panel.config_path = Path(tmp) / "config.json"
            self.config["strategy"]["options"][self.strategy.key]["route_pickup_enabled"] = True
            save_config(panel.config_path, self.config)
            panel._preview_strategy_toggle("route_pickup_enabled", variable)
            saved = load_config(panel.config_path)
        self.assertFalse(saved["strategy"]["options"][self.strategy.key]["route_pickup_enabled"])
        panel.bot.preview_strategy_setting.assert_called_once_with("route_pickup_enabled", False)
        panel.bot.apply_config.assert_not_called()


class RouteRuntimeTests(unittest.TestCase):
    def setUp(self):
        bot = self.bot = BowmanBot.__new__(BowmanBot)
        bot.config = load_config(ROOT / "config.example.json")
        bot.config["strategy"]["active"] = "stationary_attack"
        bot.config["strategy"]["options"]["stationary_attack"]["route_pickup_enabled"] = True
        bot.config["recognition"].update(recognition())
        bot.strategy = get_strategy("stationary_attack")
        bot.delivery = "hybrid"
        bot.background_input = bot.input_authorized = bot.armed = True
        bot.action_lock = threading.RLock()
        bot.started_at = 1.
        bot.last_nameplate_seen_at = bot.marker_last_seen = 181.
        bot.last_attack = bot.last_pickup = bot.last_target_seen = 0.
        bot.last_periodic_step = 156.
        bot.last_attack_anchor = (100., 100.)
        bot.direction = None
        bot.keyboard = MagicMock()
        bot.keyboard.hybrid.hwnd = 10
        bot.keyboard.hybrid.desktop.foreground.return_value = 10
        bot.keyboard.prepare_movement.return_value = True
        bot.keyboard.movement_events.return_value = []
        bot.keyboard.hybrid.yield_if_due.return_value = False
        bot.log = MagicMock()
        bot._try_auto_potion = MagicMock(return_value=False)
        bot._try_auto_buff = MagicMock(return_value=False)

    def act(self, marker=(.5, .5), now=181., target=None, player=(90, 100, 20, 1), fresh=True):
        if fresh and player is not None:
            self.bot.last_nameplate_seen_at = now
        with patch("mbv.bot.user32.IsWindow", return_value=True), \
             patch("mbv.bot.user32.IsIconic", return_value=False), \
             patch("mbv.bot.user32.GetForegroundWindow", return_value=10), \
             patch("mbv.bot.time.monotonic", return_value=now):
            self.bot.act(WindowInfo(10, "NewMaple", 0, 0, 800, 600), 1., 1., marker,
                         player, target, None, 400, target is not None, now, 200)

    def test_movement_and_pickup_use_separate_channels_with_continuous_hold(self):
        self.act()
        self.bot.keyboard.tap.assert_not_called()  # 移动准备期不拾取。
        self.act(now=181.1)
        self.bot.keyboard.movement_down.assert_called_with("right")
        self.bot.keyboard.hold.assert_called_once_with(self.bot.config["keys"]["pickup"])
        self.bot.keyboard.down.assert_not_called()
        self.act(now=181.2, marker=(.52, .5))
        self.assertEqual(self.bot.keyboard.hold.call_count, 2)
        self.act(now=181.3, marker=(.54, .5))
        self.assertEqual(self.bot.keyboard.hold.call_count, 3)
        self.bot.keyboard.tap.assert_not_called()
        self.assertNotIn(call(self.bot.config["keys"]["pickup"]), self.bot.keyboard.up.call_args_list)

    def test_runtime_completion_resets_45_seconds_only_at_home(self):
        self.act()
        self.act(marker=(.8, .5), now=182.)
        for tick in range(1, 18):
            self.act(marker=(.8, .5), now=182. + tick * .1)
        self.assertEqual(self.bot.last_periodic_step, 156.)
        self.act(marker=(.5, .5), now=188.)
        self.assertEqual(self.bot.last_periodic_step, 188.)
        self.assertEqual(self.bot.state, "PICKUP_RETURNED")
        self.assertEqual(self.bot.strategy_runtime_state["last_completed_at"], 188.)
        self.assertIsNone(self.bot._pickup_held_key)

    def test_route_hold_releases_on_each_interruption(self):
        for interruption in ("monster", "visual", "marker", "buff", "potion", "yield"):
            with self.subTest(interruption=interruption):
                self.setUp()
                self.act()
                self.act(now=181.1)
                key = self.bot.config["keys"]["pickup"]
                self.assertEqual(self.bot._pickup_held_key, key)
                self.bot.keyboard.reset_mock()
                kwargs = {}
                if interruption == "monster":
                    kwargs["target"] = (200, 90, 20, 20)
                elif interruption == "visual":
                    kwargs["player"] = None
                elif interruption == "marker":
                    kwargs["marker"] = None
                elif interruption in {"buff", "potion"}:
                    getattr(self.bot, "_try_auto_" + interruption).return_value = True
                else:
                    self.bot.keyboard.hybrid.yield_if_due.return_value = True
                self.act(now=181.2, **kwargs)
                self.bot.keyboard.up.assert_any_call(key)
                self.assertIsNone(self.bot._pickup_held_key)
                self.bot.keyboard.hold.assert_not_called()

    def test_hold_failure_attempts_release_and_pauses_hybrid(self):
        self.act()
        self.bot.notify = MagicMock()
        self.bot.keyboard.hold.side_effect = OSError("拾取按下失败")
        self.act(now=181.1)
        self.bot.keyboard.up.assert_any_call(self.bot.config["keys"]["pickup"])
        self.assertFalse(self.bot.armed)

    def test_disarm_clears_pickup_ownership(self):
        self.act()
        self.act(now=181.1)
        self.bot.notify = MagicMock()
        self.bot.disarm("test")
        self.bot.keyboard.release_all.assert_called_once()
        self.assertIsNone(self.bot._pickup_held_key)

    def test_return_combat_releases_movement_and_suspends_pickup(self):
        self.bot.strategy_runtime_state = {"phase": "returning", "return_reason": "collected",
                                          "navigation_active": True, "departed": True}
        self.act(marker=(.65, .5))
        self.act(marker=(.65, .5), now=181.1)
        self.bot.keyboard.reset_mock()
        self.act(marker=(.65, .5), now=181.2, target=(20, 90, 20, 20))
        self.bot.keyboard.up.assert_any_call("left")
        self.bot.keyboard.movement_down.assert_not_called()
        self.bot.keyboard.tap.assert_not_called()
        self.act(marker=(.65, .5), now=181.4, target=(20, 90, 20, 20))
        self.bot.keyboard.tap.assert_called_once_with(self.bot.config["keys"]["attack"])
        self.bot.keyboard.reset_mock()
        self.act(marker=(.65, .5), now=181.5)
        self.assertEqual(self.bot.state, "PICKUP_CLEAR_WAIT")
        self.bot.keyboard.tap.assert_not_called()
        self.bot.keyboard.movement_down.assert_not_called()

    def test_runtime_records_loss_across_full_visual_gate_and_resumes_short_loss(self):
        self.act()
        self.act(marker=(.65, .5), now=182., player=None)
        self.assertEqual(self.bot.state, "PLAYER_SCREEN_LOST")
        self.act(marker=(.65, .5), now=182.25)
        self.assertEqual(self.bot.strategy_runtime_state["phase"], "outbound")
        self.assertNotIn("return_reason", self.bot.strategy_runtime_state)

    def test_long_full_loss_returns_on_first_recovered_frame(self):
        self.act()
        self.act(marker=(.65, .5), now=182., player=None)
        self.act(marker=(.65, .5), now=183.1)
        self.assertEqual(self.bot.strategy_runtime_state["phase"], "returning")
        self.assertEqual(self.bot.strategy_runtime_state["return_reason"], "localization_timeout")

    def test_loss_history_survives_buff_gate_and_recovered_frames(self):
        self.act()
        self.bot._try_auto_buff.return_value = True
        self.act(marker=(.65, .5), now=182., player=None)
        self.act(marker=(.65, .5), now=183.2)
        self.bot._try_auto_buff.return_value = False
        self.act(marker=(.65, .5), now=183.3)
        self.assertEqual(self.bot.strategy_runtime_state["return_reason"], "localization_timeout")

    def test_missing_minimap_stops_without_input_then_short_recovery_resumes(self):
        self.act()
        self.act(marker=None, now=182.)
        self.assertEqual(self.bot.state, "MARKER_LOST")
        self.bot.keyboard.movement_down.assert_not_called()
        self.bot.keyboard.tap.assert_not_called()
        self.act(marker=(.65, .5), now=182.25)
        self.assertEqual(self.bot.strategy_runtime_state["phase"], "outbound")

    def test_minimap_only_wait_releases_movement_and_respects_navigation_timeout(self):
        self.act()
        self.bot.player_track = MagicMock()
        self.bot.player_track.last_seen_at = 0.
        self.bot.player_track.minimap_stationary_evidence = None
        self.bot.player_track.minimap_navigation_seen_at = 181.
        self.bot.live_marker_unambiguous = True
        self.act(marker=(.65, .5), now=182., player=None)
        self.assertEqual(self.bot.state, "PICKUP_WAIT_LOCALIZATION")
        self.bot.keyboard.movement_down.assert_not_called()
        self.bot.keyboard.tap.assert_not_called()
        self.act(marker=(.65, .5), now=183.1, player=None)
        self.assertEqual(self.bot.strategy_runtime_state["phase"], "returning")
        self.act(marker=(.65, .5), now=192., player=None)
        self.assertEqual(self.bot.state, "MINIMAP_VISUAL_TIMEOUT")

    def test_buff_pause_does_not_consume_destination_dwell(self):
        self.act()
        self.act(marker=(.8, .5), now=182.)
        self.act(marker=(.8, .5), now=182.2)
        accrued = self.bot.strategy_runtime_state["dwell_elapsed"]
        self.bot._try_auto_buff.return_value = True
        self.act(marker=(.8, .5), now=182.3)
        self.bot._try_auto_buff.return_value = False
        self.act(marker=(.8, .5), now=182.5)
        self.assertAlmostEqual(self.bot.strategy_runtime_state["dwell_elapsed"], accrued)

    def test_segment_yield_preserves_no_progress_watch_and_sends_no_pickup(self):
        self.act()
        progress = self.bot.move_progress
        self.bot.keyboard.hybrid.yield_if_due.return_value = True
        self.act(now=183.5)
        self.assertIs(self.bot.move_progress, progress)
        self.assertEqual(self.bot.state, "HYBRID_ROUTE_YIELD")
        self.bot.keyboard.tap.assert_not_called()
        self.bot.keyboard.movement_down.assert_not_called()
        self.assertEqual(self.bot.strategy_runtime_state["phase"], "outbound")

    def test_busy_user_does_not_consume_route_or_move(self):
        self.bot.keyboard.prepare_movement.return_value = False
        self.act()
        self.assertEqual(self.bot.state, "HYBRID_WAIT_IDLE_FOREGROUND")
        self.bot.keyboard.tap.assert_not_called()
        self.bot.keyboard.movement_down.assert_not_called()
        self.assertEqual(self.bot.last_periodic_step, 156.)

    def test_lost_visual_during_navigation_never_blindly_walks_to_find_player(self):
        self.act()
        self.bot.last_nameplate_seen_at = 0.
        self.bot.recover_player_nameplate = MagicMock()
        self.act(now=182., player=None)
        self.assertEqual(self.bot.state, "PLAYER_SCREEN_LOST")
        self.bot.recover_player_nameplate.assert_not_called()
        self.bot.keyboard.finish_movement.assert_called()

    def test_expired_screen_box_during_navigation_cannot_attack(self):
        self.act()
        self.bot.last_nameplate_seen_at = 0.
        self.bot.recover_player_nameplate = MagicMock()
        self.act(now=182., target=(120, 90, 20, 20), fresh=False)
        self.assertEqual(self.bot.state, "PLAYER_SCREEN_LOST")
        self.bot.recover_player_nameplate.assert_not_called()
        self.bot.keyboard.tap.assert_not_called()

    def test_buff_interrupt_preserves_route_but_releases_movement(self):
        self.act()
        state = deepcopy(self.bot.strategy_runtime_state)
        self.bot._try_auto_buff.return_value = True
        self.act(now=182.)
        self.assertEqual(self.bot.strategy_runtime_state, state)
        self.bot.keyboard.finish_movement.assert_called()
        self.bot.keyboard.tap.assert_not_called()

    def test_pause_clears_route_and_releases_keys_without_returning(self):
        self.act()
        self.bot.notify = MagicMock()
        with patch("mbv.bot.user32.MessageBeep"):
            self.bot.disarm("测试暂停")
        self.assertEqual(self.bot.strategy_runtime_state, {})
        self.assertFalse(self.bot.armed)
        self.bot.keyboard.release_all.assert_called_once()
        self.bot.keyboard.movement_down.assert_not_called()

    def test_conflicting_pickup_key_is_rejected(self):
        self.bot.config["keys"]["pickup"] = "left"
        with self.assertRaisesRegex(RuntimeError, "拾取键"):
            self.bot._hold_route_pickup(181.)
        self.bot.keyboard.tap.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, "拾取键"):
            self.act()
        self.bot.keyboard.movement_down.assert_not_called()
        self.bot.keyboard.prepare_movement.assert_not_called()


if __name__ == "__main__":
    unittest.main()
