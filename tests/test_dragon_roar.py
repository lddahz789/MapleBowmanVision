from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import unittest

from mbv.strategies import get_strategy, missing_recognition_data, normalize_strategy_config
from mbv.strategies.base import StrategyActionContext, TargetSelectionContext
from mbv.strategies.warrior import DragonRoarStrategy
from mbv.vision import Detection, PLAYER_RELATIVE_REGION_SPACE


def region(**changes):
    return {"id": "roar", "name": "攻击范围", "enabled": True, "priority": 1,
            "space": PLAYER_RELATIVE_REGION_SPACE,
            "offset_x": -0.25, "offset_y": -0.5, "w": 0.5, "h": 1.0, **changes}


def monster(x, y=100, name="怪物"):
    return Detection((x - 10, y - 10, 20, 20), 0.9, name)


class DragonRoarStrategyTests(unittest.TestCase):
    def setUp(self):
        self.strategy = DragonRoarStrategy()
        self.monsters = (monster(140, 40), monster(200, 100), monster(260, 160))
        self.settings = {**deepcopy(self.strategy.default_settings),
                         "skill_key": "r", "attack_regions": [region()]}
        self.recognition = {"dragon_roar_point": {"x": 0.5, "y": 0.5},
                            "dragon_roar_point_space": "minimap",
                            "dragon_roar_point_captured": True}
        self.context = StrategyActionContext(
            marker=(0.5, 0.5), player_box=(190, 100, 20, 1), player_anchor=(200.0, 100.0),
            target_box=self.monsters[1].box, chase_box=None, combat_width=400, combat_height=200,
            has_monster_candidates=True, now=10.0, last_target_seen=9.0, last_pickup=0.0,
            direction="left", behavior={}, settings=self.settings, recognition=self.recognition,
            eligible_detections=self.monsters,
        )
        self.selection = TargetSelectionContext(
            detections=list(self.monsters), player_box=self.context.player_box,
            player_raw_box=(195, 60, 10, 4), player_anchor=self.context.player_anchor,
            scene_width=400, scene_height=200, facing="left",
            target_area={"forward": 0.0, "back": 0.0, "up": 0.0, "down": 0.0},
            settings=self.settings,
        )

    def decide(self, **changes):
        result = self.strategy.decide(replace(self.context, **changes))
        self.assertTrue(result.runtime_state["navigation_active"])
        return result

    def test_registered_metadata_required_captures_and_safe_defaults(self):
        registered = get_strategy("dragon_roar")
        self.assertIsInstance(registered, DragonRoarStrategy)
        self.assertEqual(registered.profession, "战士·龙骑士")
        self.assertEqual(registered.display_name, "龙咆哮·定点")
        self.assertIn("严格超过", registered.description)
        self.assertFalse(registered.allow_player_lost_recovery)
        fields = {field.recognition_key: field for field in registered.capture_fields}
        self.assertTrue(all(field.required for field in fields.values()))
        self.assertEqual(fields["dragon_roar_point"].coordinate_space, "minimap")
        self.assertEqual(fields["dragon_roar_point"].capture_kind, "point")
        self.assertTrue(fields["dragon_roar_attack_regions"].multiple)
        self.assertEqual(fields["dragon_roar_attack_regions"].settings_path, "attack_regions")
        self.assertEqual(registered.default_settings["skill_key"], "")
        self.assertEqual(registered.default_settings["attack_regions"], [])

    def test_numeric_fields_are_editable_with_explicit_bounds(self):
        fields = {field.path: field for field in self.strategy.setting_fields}
        self.assertTrue(fields["skill_key"].capture_key)
        self.assertEqual(fields["monster_count_threshold"].step, 1)
        self.assertEqual(fields["monster_count_threshold"].maximum, 23)
        for field in fields.values():
            if not field.capture_key:
                self.assertTrue(field.direct_numeric_input)
                self.assertIsNotNone(field.minimum)
                self.assertIsNotNone(field.maximum)

    def test_old_config_adds_defaults_without_changing_active_or_other_skills(self):
        config = {"strategy": {"active": "stationary_attack", "options": {
            "stationary_attack": {"melee_skill_key": "q"}}}}
        normalize_strategy_config(config)
        self.assertEqual(config["strategy"]["active"], "stationary_attack")
        self.assertEqual(config["strategy"]["options"]["stationary_attack"]["melee_skill_key"], "q")
        self.assertEqual(config["strategy"]["options"]["dragon_roar"], self.strategy.default_settings)
        config["strategy"]["options"]["dragon_roar"]["attack_regions"].append(region())
        self.assertEqual(self.strategy.default_settings["attack_regions"], [])

    def test_normalization_clamps_and_sanitizes_inputs(self):
        settings = {"skill_key": " R ", "monster_count_threshold": 99,
                    "cast_interval_seconds": -10, "return_tolerance_x": -1,
                    "return_tolerance_y": 3, "return_jump_interval_seconds": float("nan"),
                    "return_timeout_seconds": float("inf"), "attack_regions": [region()]}
        self.strategy.normalize_settings(settings)
        self.assertEqual(settings["skill_key"], "r")
        self.assertEqual(settings["monster_count_threshold"], 23)
        self.assertEqual(settings["cast_interval_seconds"], 0.1)
        self.assertEqual(settings["return_tolerance_x"], 0.005)
        self.assertEqual(settings["return_tolerance_y"], 0.5)
        self.assertEqual(settings["return_jump_interval_seconds"], 0.45)
        self.assertEqual(settings["return_timeout_seconds"], 15.0)
        self.assertEqual(len(settings["attack_regions"]), 1)

    def test_invalid_keys_booleans_and_nonfinite_counts_restore_safe_values(self):
        for value in (True, None, float("nan"), float("inf"), "invalid"):
            settings = {"skill_key": True, "monster_count_threshold": value}
            self.strategy.normalize_settings(settings)
            self.assertEqual(settings["skill_key"], "")
            self.assertEqual(settings["monster_count_threshold"], 2)
        settings = {"monster_count_threshold": -9}
        self.strategy.normalize_settings(settings)
        self.assertEqual(settings["monster_count_threshold"], 0)
        self.assertIsInstance(settings["monster_count_threshold"], int)

    def test_required_dependencies_are_reported_without_optional_toggle(self):
        config = {"strategy": {"active": "dragon_roar", "options": {}}}
        normalize_strategy_config(config)
        self.assertCountEqual(missing_recognition_data(config, self.strategy),
                              ("dragon_roar_point", "dragon_roar_attack_regions"))
        config["recognition"] = deepcopy(self.recognition)
        config["strategy"]["options"]["dragon_roar"]["attack_regions"] = [region()]
        self.assertEqual(missing_recognition_data(config, self.strategy), ())

    def test_selection_ignores_facing_common_box_and_layer(self):
        for facing in ("left", "right", None):
            selected = self.strategy.select_targets(replace(self.selection, facing=facing))
            self.assertEqual(selected.eligible_detections, self.monsters)
            self.assertEqual(selected.eligible_candidate_count, 3)
            self.assertIsNone(selected.chase_target)
            self.assertFalse(selected.uses_common_target_area)
            self.assertEqual(selected.target, self.monsters[1])

    def test_overlapping_regions_and_duplicate_frame_boxes_count_once(self):
        settings = {**self.settings, "attack_regions": [region(), region(id="overlap")]}
        selected = self.strategy.select_targets(replace(self.selection, settings=settings,
            detections=[*self.monsters, self.monsters[0], monster(140, 40, "重复模板")]))
        self.assertEqual(selected.eligible_candidate_count, 3)
        self.assertEqual(selected.eligible_detections, self.monsters)

    def test_disjoint_ranges_union_without_counting_gap(self):
        settings = {**self.settings, "attack_regions": [
            region(offset_x=-0.25, w=0.15), region(id="right", offset_x=0.10, w=0.15)]}
        selected = self.strategy.select_targets(replace(self.selection, settings=settings))
        self.assertEqual(selected.eligible_detections, (self.monsters[0], self.monsters[2]))

    def test_region_edges_are_inclusive_and_use_monster_center(self):
        detections = [monster(100), monster(300), monster(200, 0), monster(200, 200),
                      monster(99), monster(301), monster(200, -1), monster(200, 201)]
        selected = self.strategy.select_targets(replace(self.selection, detections=detections))
        self.assertEqual(selected.eligible_detections, tuple(detections[:4]))

    def test_regions_follow_stable_anchor_and_not_raw_nameplate_height(self):
        selected = self.strategy.select_targets(replace(self.selection,
            player_anchor=(300.0, 100.0), player_raw_box=(190, 999, 20, 50)))
        self.assertEqual(selected.eligible_detections, self.monsters[1:])

    def test_stale_hold_is_never_selected_or_counted(self):
        selected = self.strategy.select_targets(replace(self.selection, detections_fresh=False))
        self.assertIsNone(selected.target)
        self.assertEqual(selected.eligible_detections, ())
        self.assertEqual(selected.eligible_candidate_count, 0)
        decision = self.decide(eligible_detections=selected.eligible_detections)
        self.assertEqual(decision.state, "DRAGON_WAITING_MONSTERS")
        self.assertEqual(decision.runtime_state["monster_count"], 0)

    def test_selection_requires_player_stable_anchor_and_scene(self):
        for changes in ({"player_box": None}, {"player_anchor": None},
                        {"player_anchor": (float("nan"), 100)}, {"scene_width": 0},
                        {"scene_height": 0}):
            with self.subTest(changes=changes):
                selected = self.strategy.select_targets(replace(self.selection, **changes))
                self.assertEqual(selected.eligible_detections, ())

    def test_missing_disabled_or_obsolete_regions_stop_and_do_not_select(self):
        for regions in ([], [region(enabled=False)], [region(space="combat")],
                        [region(w=0)], [region(h=float("nan"))]):
            settings = {**self.settings, "attack_regions": regions}
            with self.subTest(regions=regions):
                selected = self.strategy.select_targets(replace(self.selection, settings=settings))
                self.assertEqual(selected.eligible_detections, ())
                decision = self.decide(settings=settings, marker=(0.7, 0.5))
                self.assertEqual(decision.action, "stop")
                self.assertEqual(decision.state, "DRAGON_RANGE_UNCALIBRATED")

    def test_strict_monster_threshold_requires_three_when_value_is_two(self):
        for count in range(4):
            result = self.decide(eligible_detections=self.monsters[:count])
            self.assertEqual(result.runtime_state["monster_count"], count)
            self.assertEqual(result.action, "cast" if count > 2 else "stop")

    def test_threshold_zero_still_requires_one_current_monster(self):
        settings = {**self.settings, "monster_count_threshold": 0}
        self.assertEqual(self.decide(settings=settings, eligible_detections=()).action, "stop")
        self.assertEqual(self.decide(settings=settings, eligible_detections=self.monsters[:1]).action,
                         "cast")

    def test_maximum_threshold_is_reachable_at_detection_limit(self):
        detections = tuple(monster(110 + i * 7) for i in range(24))
        settings = {**self.settings, "monster_count_threshold": 23}
        self.assertEqual(self.decide(settings=settings, eligible_detections=detections).action, "cast")
        self.assertEqual(self.decide(settings=settings, eligible_detections=detections[:23]).action,
                         "stop")

    def test_cast_has_only_requested_skill_and_interval_not_facing(self):
        result = self.decide(settings={**self.settings, "skill_key": " R ",
                                       "cast_interval_seconds": 1.7})
        self.assertEqual((result.action, result.state), ("cast", "DRAGON_ROAR"))
        self.assertEqual(result.attack_key, "r")
        self.assertEqual(result.attack_skill, "dragon_roar")
        self.assertEqual(result.attack_interval_seconds, 1.7)
        self.assertIsNone(result.direction)
        self.assertIsNone(result.move_seconds)
        self.assertIsNone(result.pickup_interval_seconds)
        self.assertFalse(result.face_each_attack)

    def test_empty_skill_never_falls_back_to_generic_attack(self):
        for key in ("", " ", None, True):
            result = self.decide(settings={**self.settings, "skill_key": key})
            self.assertEqual(result.state, "DRAGON_SKILL_UNBOUND")
            self.assertEqual(result.action, "stop")
            self.assertIsNone(result.attack_key)

    def test_decision_rechecks_range_and_does_not_trust_target_box_alone(self):
        result = self.decide(eligible_detections=())
        self.assertEqual(result.action, "stop")
        result = self.decide(eligible_detections=(monster(50), monster(350), monster(400)))
        self.assertEqual(result.runtime_state["monster_count"], 0)
        self.assertEqual(result.action, "stop")

    def test_point_requires_capture_flag_minimap_space_and_valid_coordinates(self):
        invalid = [{}, {"dragon_roar_point_captured": False},
                   {"dragon_roar_point_space": "combat"},
                   {"dragon_roar_point": {"x": float("nan"), "y": 0.5}},
                   {"dragon_roar_point": {"x": 0.5, "y": 1.1}},
                   {"dragon_roar_point": {"x": True, "y": 0.5}}]
        for changes in invalid:
            recognition = {} if not changes else {**self.recognition, **changes}
            result = self.decide(recognition=recognition)
            self.assertEqual(result.state, "DRAGON_POINT_UNCALIBRATED")
            self.assertEqual(result.action, "stop")

    def test_missing_invalid_marker_never_casts_or_moves(self):
        for marker in (None, (float("nan"), 0.5), (0.5, float("inf")),
                       (-0.1, 0.5), (0.5, 1.1), (True, 0.5)):
            result = self.decide(marker=marker)
            self.assertEqual(result.state, "MARKER_LOST")
            self.assertEqual(result.action, "stop")

    def test_missing_visual_without_minimap_only_authority_stops(self):
        for changes in ({"player_box": None}, {"player_anchor": None}):
            result = self.decide(marker=(0.8, 0.5), **changes)
            self.assertEqual(result.state, "PLAYER_SCREEN_LOST")
            self.assertEqual(result.action, "stop")

    def test_horizontal_return_preempts_skill_even_when_key_missing(self):
        for marker, direction in (((0.6, 0.5), "left"), ((0.4, 0.5), "right")):
            result = self.decide(marker=marker, settings={**self.settings, "skill_key": ""})
            self.assertEqual(result.action, "move")
            self.assertEqual(result.direction, direction)
            self.assertEqual(result.runtime_state["phase"], "returning")
            self.assertEqual(result.runtime_state["return_started_at"], 10.0)
            self.assertEqual(result.target_x, 0.5)
            self.assertIsNone(result.attack_key)

    def test_outer_tolerance_boundary_does_not_start_return(self):
        for marker in ((0.515, 0.5), (0.485, 0.5), (0.5, 0.56), (0.5, 0.44)):
            result = self.decide(marker=marker)
            self.assertEqual(result.action, "cast")
            self.assertEqual(result.runtime_state["phase"], "idle")

    def test_return_hysteresis_keeps_moving_inside_outer_radius_until_arrival(self):
        initial = self.decide(marker=(0.52, 0.5))
        inside_outer = self.decide(marker=(0.512, 0.5), now=11,
                                   runtime_state=initial.runtime_state)
        self.assertEqual(inside_outer.action, "move")
        self.assertEqual(inside_outer.runtime_state["return_started_at"], 10)
        arrived = self.decide(marker=(0.509, 0.5), now=12,
                              runtime_state=inside_outer.runtime_state)
        self.assertEqual(arrived.action, "cast")
        self.assertEqual(arrived.runtime_state["phase"], "idle")
        self.assertNotIn("return_started_at", arrived.runtime_state)

    def test_vertical_return_and_cooldown(self):
        for marker, action in (((0.5, 0.6), "jump"), ((0.5, 0.4), "down_jump")):
            result = self.decide(marker=marker)
            self.assertEqual(result.action, action)
            waiting = self.decide(marker=marker, last_jump=9.8,
                                  runtime_state=result.runtime_state)
            self.assertEqual(waiting.action, "stop")
            self.assertEqual(waiting.state, "DRAGON_WAITING_RETURN_JUMP")
            self.assertEqual(waiting.runtime_state["phase"], "returning")

    def test_horizontal_alignment_precedes_vertical_jump(self):
        result = self.decide(marker=(0.6, 0.7))
        self.assertEqual(result.action, "move")
        self.assertEqual(result.direction, "left")

    def test_minimap_only_can_return_but_never_casts_or_counts_targets(self):
        changes = {"minimap_only": True, "player_box": None, "player_anchor": None}
        for marker, action in (((0.6, 0.5), "move"), ((0.5, 0.6), "jump"),
                               ((0.5, 0.4), "down_jump"), ((0.5, 0.5), "stop")):
            result = self.decide(marker=marker, **changes)
            self.assertEqual(result.action, action)
            self.assertEqual(result.runtime_state["monster_count"], 0)
            self.assertIsNone(result.attack_key)
        self.assertEqual(self.decide(**changes).state, "MINIMAP_WAITING_VISUAL")

    def test_missing_minimap_only_marker_stops(self):
        result = self.decide(marker=None, minimap_only=True, player_box=None, player_anchor=None)
        self.assertEqual(result.action, "stop")
        self.assertEqual(result.state, "MARKER_LOST")

    def test_timeout_latches_for_horizontal_or_vertical_navigation(self):
        for marker in ((0.6, 0.5), (0.5, 0.7)):
            initial = self.decide(marker=marker)
            near_timeout = self.decide(marker=marker, now=24.9, runtime_state=initial.runtime_state)
            self.assertNotEqual(near_timeout.state, "DRAGON_RETURN_BLOCKED")
            expired = self.decide(marker=marker, now=25.0, runtime_state=near_timeout.runtime_state)
            self.assertEqual(expired.action, "stop")
            self.assertEqual(expired.state, "DRAGON_RETURN_BLOCKED")
            self.assertEqual(expired.runtime_state["phase"], "blocked")
            self.assertEqual(expired.runtime_state["return_reason"], "timeout")
            at_home = self.decide(now=26.0, runtime_state=expired.runtime_state)
            self.assertEqual(at_home.state, "DRAGON_RETURN_BLOCKED")
            self.assertEqual(self.decide(now=26.0, runtime_state={}).action, "cast")

    def test_missing_position_and_interruptions_do_not_extend_return_timeout(self):
        initial = self.decide(marker=(0.6, 0.5))
        lost = self.decide(marker=None, now=20.0, runtime_state=initial.runtime_state)
        self.assertEqual(lost.state, "MARKER_LOST")
        self.assertEqual(lost.runtime_state["return_started_at"], 10.0)
        restored = self.decide(marker=(0.6, 0.5), now=26.0, runtime_state=lost.runtime_state,
                               action_interrupted=True, localization_lost_seconds=6)
        self.assertEqual(restored.state, "DRAGON_RETURN_BLOCKED")

    def test_timeout_from_zero_timestamp_is_not_reset(self):
        initial = self.decide(marker=(0.6, 0.5), now=0.0)
        self.assertEqual(initial.runtime_state["return_started_at"], 0.0)
        expired = self.decide(marker=(0.6, 0.5), now=15.0, runtime_state=initial.runtime_state)
        self.assertEqual(expired.state, "DRAGON_RETURN_BLOCKED")

    def test_new_return_gets_new_timeout_after_successful_arrival(self):
        first = self.decide(marker=(0.6, 0.5))
        arrival = self.decide(now=11.0, runtime_state=first.runtime_state)
        second = self.decide(marker=(0.6, 0.5), now=30.0, runtime_state=arrival.runtime_state)
        self.assertEqual(second.action, "move")
        self.assertEqual(second.runtime_state["return_started_at"], 30.0)

    def test_no_patrol_pickup_or_chase_even_when_common_behavior_requests_it(self):
        result = self.decide(eligible_detections=(), chase_box=(500, 100, 20, 20),
                            has_monster_candidates=False, last_target_seen=0,
                            behavior={"fallback_patrol": True, "pickup_after_target_lost": True})
        self.assertEqual(result.action, "stop")
        self.assertEqual(result.state, "DRAGON_WAITING_MONSTERS")

    def test_strategy_has_no_mutable_session_state_and_does_not_modify_inputs(self):
        previous = {"phase": "idle", "navigation_active": True, "monster_count": 0}
        before_settings = deepcopy(self.settings)
        result = self.decide(marker=(0.6, 0.5), runtime_state=previous)
        self.assertEqual(previous, {"phase": "idle", "navigation_active": True, "monster_count": 0})
        self.assertEqual(self.settings, before_settings)
        self.assertIsNot(result.runtime_state, previous)
        self.assertEqual(self.strategy.__dict__, {})
        self.assertEqual(self.decide().action, "cast")


if __name__ == "__main__":
    unittest.main()
