from copy import deepcopy
from dataclasses import replace
import unittest

from mbv.strategies import get_strategy, missing_recognition_data, normalize_strategy_config
from mbv.strategies.base import StrategyActionContext, TargetSelectionContext
from mbv.vision import Detection


class ArrowRainTests(unittest.TestCase):
    def setUp(self):
        self.strategy = get_strategy("bowman_arrow_rain")
        self.context = StrategyActionContext(
            marker=(.6, .5), player_box=(90, 100, 20, 1), player_anchor=(100., 100.),
            target_box=(130, 90, 20, 20), chase_box=None, combat_width=400, combat_height=200,
            has_monster_candidates=True, now=10., last_target_seen=9., last_pickup=0.,
            direction="left", behavior={"attack_interval_seconds": .2},
            settings=deepcopy(self.strategy.default_settings), default_attack_key="d",
            recognition={"platform_center": {"x": .5, "y": .5},
                         "platform_center_captured": True, "platform_center_space": "minimap",
                         "stationary_pickup_point": {"x": .8, "y": .5},
                         "stationary_pickup_point_captured": True,
                         "stationary_pickup_point_space": "minimap"},
        )

    def decide(self, **changes):
        return self.strategy.decide(replace(self.context, **changes))

    def test_config_is_independent_and_does_not_switch_existing_strategy(self):
        config = {"strategy": {"active": "stationary_attack", "options": {
            "stationary_attack": {"melee_skill_key": "f", "route_pickup_interval_seconds": 90.}}}}
        normalize_strategy_config(config)
        self.assertEqual(config["strategy"]["active"], "stationary_attack")
        settings = config["strategy"]["options"]
        self.assertEqual(settings["bowman_arrow_rain"]["melee_skill_key"], "")
        self.assertEqual(settings["stationary_attack"]["route_pickup_interval_seconds"], 90.)
        self.assertIsNot(settings["bowman_arrow_rain"], settings["stationary_attack"])
        self.assertEqual(missing_recognition_data(config, self.strategy), ("platform_center",))

    def continuity_context(self):
        return TargetSelectionContext(
            detections=[Detection((550, 90, 20, 20), .95, "monster")],
            player_box=(490, 100, 20, 1), player_raw_box=None, player_anchor=(500., 100.),
            scene_width=1200, scene_height=200, facing="right",
            target_area={"forward": .5, "back": .5, "up": .2, "down": .2},
            settings={"attack_range_px": 300.}, now=100., player_visual_fresh=True,
            marker_pixels=(50., 50.), marker_size=(100, 100),
        )

    def test_stationary_hold_uses_fixed_evidence_and_expires_without_stale_chase(self):
        context = self.continuity_context()
        fresh = self.strategy.select_targets(context)
        held_context = replace(context, now=100.2, detections=[], detections_fresh=False,
                               allow_target_hold=True, continuity_state=fresh.continuity_state)
        held = self.strategy.select_targets(held_context)
        self.assertEqual(held.target, fresh.target)
        self.assertIsNone(held.chase_target)
        self.assertIs(held.continuity_state, fresh.continuity_state)
        self.assertEqual(held.diagnostic["reason"], "bounded_target_hold")
        expired = self.strategy.select_targets(replace(
            held_context, now=100.251, continuity_state=held.continuity_state))
        self.assertIsNone(expired.target)
        self.assertIsNone(expired.chase_target)
        self.assertIsNone(expired.continuity_state)
        self.assertEqual(expired.diagnostic["reason"], "target_hold_expired")

    def test_hold_rejects_movement_localization_interruption_and_range_edits(self):
        context = self.continuity_context()
        evidence = self.strategy.select_targets(context).continuity_state
        held = replace(context, now=100.1, detections_fresh=False,
                       allow_target_hold=True, continuity_state=evidence)
        for changes in ({"allow_target_hold": False}, {"player_visual_fresh": False},
                        {"player_box": None}, {"player_box": (494, 100, 20, 1)},
                        {"marker_pixels": None}, {"marker_pixels": (50.51, 50.)},
                        {"marker_size": (101, 100)}, {"scene_width": 1201},
                        {"settings": {"attack_range_px": 50.}},
                        {"target_area": {"forward": .01, "back": .01, "up": .2, "down": .2}}):
            with self.subTest(changes=changes):
                result = self.strategy.select_targets(replace(held, **changes))
                self.assertIsNone(result.target)
                self.assertIsNone(result.chase_target)
                self.assertIsNone(result.continuity_state)

    def test_fresh_out_of_range_detection_replaces_evidence_not_resurrected(self):
        context = self.continuity_context()
        evidence = self.strategy.select_targets(context).continuity_state
        far = Detection((900, 90, 20, 20), .95, "far")
        result = self.strategy.select_targets(replace(
            context, detections=[far], continuity_state=evidence, allow_target_hold=True))
        self.assertIsNone(result.target)
        self.assertEqual(result.chase_target, far)
        self.assertIsNone(result.continuity_state)
        self.assertEqual(result.diagnostic["reason"], "outside_skill_range")

    def test_continuous_timing_is_opt_in_only_arrow_rain(self):
        self.assertTrue(self.decide().cast_interval_from_start)
        self.assertFalse(get_strategy("stationary_attack").decide(self.context).cast_interval_from_start)

    def test_off_point_and_step_return_clear_monsters_before_return(self):
        for marker in ((.6, .5), (.4, .5), (.5, .7), (.5, .3)):
            result = self.decide(marker=marker, periodic_step_pending_return=True)
            self.assertEqual(result.action, "cast")
            self.assertEqual(result.attack_key, "d")
            self.assertIsNone(result.direction)
            self.assertFalse(result.periodic_step_return_complete)
        original = get_strategy("stationary_attack").decide(self.context)
        self.assertEqual((original.action, original.direction), ("move", "left"))

    def test_no_target_confirmation_then_return_and_reappearing_target_stops_move(self):
        attack = self.decide()
        waiting = self.decide(target_box=None, now=11., runtime_state=attack.runtime_state)
        self.assertEqual(waiting.state, "ARROW_RAIN_CLEAR_WAIT")
        early = self.decide(target_box=None, now=11.39, runtime_state=waiting.runtime_state)
        self.assertEqual(early.action, "stop")
        returning = self.decide(target_box=None, now=11.41, runtime_state=early.runtime_state)
        self.assertEqual((returning.action, returning.direction), ("move", "left"))
        self.assertNotIn("combat_pending", returning.runtime_state)
        reappeared = self.decide(now=11.5, runtime_state=returning.runtime_state)
        self.assertEqual(reappeared.action, "cast")

    def test_interruption_and_lost_marker_restart_clear_confirmation(self):
        state = {"combat_pending": True, "combat_clear_since": 9.}
        result = self.decide(target_box=None, action_interrupted=True, runtime_state=state)
        self.assertEqual(result.action, "stop")
        self.assertEqual(result.runtime_state["combat_clear_since"], 10.)
        lost = self.decide(marker=None, runtime_state=state)
        self.assertEqual(lost.action, "stop")
        self.assertNotIn("combat_clear_since", lost.runtime_state)
        self.assertEqual(state["combat_clear_since"], 9.)

    def test_all_attack_sides_and_melee_use_cast_without_facing(self):
        for box in ((75, 90, 20, 20), (105, 90, 20, 20)):
            for melee in ("", "f"):
                result = self.decide(target_box=box, settings={**self.context.settings, "melee_skill_key": melee})
                self.assertEqual(result.action, "cast")
                self.assertEqual(result.attack_key, melee or "d")
                self.assertEqual(result.attack_interval_seconds, .2)
                self.assertIsNone(result.direction)
                self.assertIsNone(result.face_tap_seconds)
                self.assertIsNone(result.target_x)

    def test_home_keeps_periodic_step_and_no_monster_navigation(self):
        self.assertEqual(self.decide(marker=(.5, .5), now=46.).action, "step")
        self.assertEqual(self.decide(marker=(.5, .5)).action, "cast")
        self.assertEqual(self.decide(target_box=None).action, "move")
        self.assertEqual(self.decide(marker=(.5, .5), target_box=None,
                                     periodic_step_pending_return=True).state, "PERIODIC_STEP_RETURNED")

    def test_periodic_step_toggle_blocks_only_new_steps(self):
        disabled = {**self.context.settings, "periodic_step_enabled": False}
        self.assertEqual(self.decide(marker=(.5, .5), now=1000., settings=disabled).action, "cast")
        self.assertEqual(self.decide(marker=(.5, .5), now=1000., target_box=None,
                                     has_monster_candidates=False, settings=disabled).state, "SCANNING")
        self.assertEqual(self.decide(now=1000., target_box=None, settings=disabled).action, "move")
        self.assertEqual(self.decide(target_box=None, chase_box=(360, 90, 20, 20),
                                     settings=disabled).state, "ARROW_RAIN_APPROACH_RIGHT")
        returned = self.decide(marker=(.5, .5), target_box=None,
                              periodic_step_pending_return=True, settings=disabled)
        self.assertTrue(returned.periodic_step_return_complete)
        self.assertEqual(self.decide(marker=(.5, .5), now=1000.).action, "step")
        # 原地策略不读取箭雨开关，保持原行为。
        original = get_strategy("stationary_attack").decide(replace(
            self.context, marker=(.5, .5), now=1000., settings=disabled))
        self.assertEqual(original.action, "step")

    def test_disabled_periodic_step_does_not_disable_pickup(self):
        settings = {**self.context.settings, "periodic_step_enabled": False, "route_pickup_enabled": True}
        result = self.decide(marker=(.5, .5), now=181., target_box=None,
                             has_monster_candidates=False, settings=settings)
        self.assertEqual(result.state, "PICKUP_OUTBOUND_RIGHT")
        self.assertIsNotNone(result.pickup_interval_seconds)

    def test_periodic_step_defaults_and_invalid_values(self):
        for value, expected in ((False, False), (True, True), (None, True), ("false", True)):
            settings = {"periodic_step_enabled": value}
            self.strategy.normalize_settings(settings)
            self.assertIs(settings["periodic_step_enabled"], expected)
        settings = {}
        self.strategy.normalize_settings(settings)
        self.assertIs(settings["periodic_step_enabled"], True)

    def test_safety_missing_position_and_minimap_only(self):
        self.assertEqual(self.decide(recognition={}).action, "stop")
        self.assertEqual(self.decide(marker=None).action, "stop")
        self.assertEqual(self.decide(player_box=None).action, "stop")
        for marker, action in (((.6, .5), "move"), ((.5, .5), "stop")):
            result = self.decide(marker=marker, player_box=None, minimap_only=True)
            self.assertEqual(result.action, action)
            self.assertIsNone(result.attack_key)

    def test_pickup_outbound_collect_and_normal_return_do_not_face(self):
        for phase, marker in (("outbound", (.6, .5)), ("collect", (.8, .5)),
                              ("returning", (.6, .5)), ("returning", (.5, .5))):
            result = self.decide(marker=marker,
                settings={**self.context.settings, "route_pickup_enabled": True},
                runtime_state={"phase": phase, "route_started_at": 5., "return_reason": "collected",
                               "combat_side": "left"})
            self.assertEqual(result.action, "cast")
            self.assertIsNone(result.direction)
            self.assertIsNone(result.face_tap_seconds)
            self.assertIsNone(result.pickup_interval_seconds)
            self.assertNotIn("combat_side", result.runtime_state)

    def test_safety_pickup_return_still_preempts_combat(self):
        for reason in ("disabled", "off_platform", "point_invalid", "localization_timeout"):
            result = self.decide(settings={**self.context.settings, "route_pickup_enabled": True},
                runtime_state={"phase": "returning", "return_reason": reason})
            self.assertEqual(result.action, "move")

    def test_target_selection_respects_configured_front_back_and_facing(self):
        monsters = [Detection(name="left", score=.9, box=(60, 90, 20, 20)),
                    Detection(name="right", score=.9, box=(130, 90, 20, 20)),
                    Detection(name="outside", score=.9, box=(380, 90, 20, 20))]
        context = TargetSelectionContext(monsters, (90, 100, 20, 1), None, (100., 100.),
            400, 200, "left", {"forward": .2, "back": .05, "up": .2, "down": .2}, {})
        for direction in ("left", "right"):
            selected = self.strategy.select_targets(replace(context, facing=direction))
            self.assertEqual({d.name for d in selected.eligible_detections}, {direction})
            self.assertEqual(selected.target.name, direction)
            self.assertIsNone(selected.chase_target)
        self.assertEqual(self.context.runtime_state, {})
        self.assertEqual(self.strategy.__dict__, {})

    def select(self, centers, radius=300., width=1200, fresh=True, area=None, facing="right"):
        detections = [Detection(name=str(i), score=.9, box=(x - 10, y - 10, 20, 20))
                      for i, (x, y) in enumerate(centers)]
        return self.strategy.select_targets(TargetSelectionContext(
            detections=detections, player_box=(490, 100, 20, 1), player_raw_box=None,
            player_anchor=(500., 100.), scene_width=width, scene_height=200, facing=facing,
            target_area=area or {"forward": .5, "back": .5, "up": .2, "down": .2},
            settings={"attack_range_px": radius}, detections_fresh=fresh))

    def test_pixel_range_boundary_is_symmetric_and_independent_of_scene_width(self):
        for width in (1200, 1600):
            for x in (200, 800):
                result = self.select([(x, 100)], width=width)
                self.assertIsNotNone(result.target)
                self.assertIsNone(result.chase_target)
                self.assertAlmostEqual(result.skill_area_override["forward"] * width, 300.)
                self.assertAlmostEqual(result.skill_area_override["back"] * width, 300.)
            for x in (199, 801):
                result = self.select([(x, 100)], width=width)
                self.assertIsNone(result.target)
                self.assertIsNotNone(result.chase_target)

    def test_skill_range_changes_apply_inside_search_area(self):
        self.assertIsNotNone(self.select([(850, 100)], radius=350).target)
        self.assertIsNotNone(self.select([(850, 100)], radius=349).chase_target)
        field = next(f for f in self.strategy.setting_fields if f.path == "attack_range_px")
        self.assertTrue(field.direct_numeric_input)
        for value, expected in ((None, 300.), (float("nan"), 300.), (True, 300.),
                                (-1, 10.), (9999, 2000.), (250., 250.)):
            settings = {"attack_range_px": value}
            self.strategy.normalize_settings(settings)
            self.assertEqual(settings["attack_range_px"], expected)

    def test_all_four_search_bounds_change_eligibility_and_display_without_changing_skill_width(self):
        area = {"forward": .4, "back": .4, "up": .2, "down": .2}
        for field, value, point in (("forward", .2, (850, 100)), ("back", .2, (150, 100)),
                                    ("up", .05, (550, 80)), ("down", .05, (550, 120))):
            before = self.select([point], area=area)
            self.assertEqual(before.eligible_candidate_count, 1)
            changed = {**area, field: value}
            after = self.select([point], area=changed)
            self.assertEqual(after.eligible_candidate_count, 0)
            self.assertIsNone(after.target)
            self.assertIsNone(after.chase_target)
            self.assertEqual(after.attack_area_override, changed)
            self.assertEqual(after.skill_area_override["forward"] * 1200, 300.)
            self.assertEqual(after.skill_area_override["back"] * 1200, 300.)
        self.assertEqual(area, {"forward": .4, "back": .4, "up": .2, "down": .2})

    def test_search_and_skill_area_changes_are_independent_and_available_without_detections(self):
        first = self.select([(850, 100)], radius=300)
        expanded_skill = self.select([(850, 100)], radius=400)
        self.assertIsNotNone(first.chase_target)
        self.assertIsNotNone(expanded_skill.target)
        self.assertEqual(first.attack_area_override, expanded_skill.attack_area_override)
        stale = self.select([], radius=400, fresh=False)
        self.assertEqual(stale.attack_area_override, expanded_skill.attack_area_override)
        self.assertEqual(stale.skill_area_override, expanded_skill.skill_area_override)

    def test_out_of_height_or_frame_and_stale_detections_are_not_chased(self):
        result = self.select([(900, 10), (900, 180), (-50, 100), (1250, 100)])
        self.assertIsNone(result.target)
        self.assertIsNone(result.chase_target)
        self.assertEqual(result.eligible_candidate_count, 0)
        stale = self.select([(850, 100)], fresh=False)
        self.assertIsNone(stale.chase_target)
        self.assertFalse(self.strategy.allow_player_lost_recovery)

    def test_chase_moves_toward_target_even_at_home_then_stops_to_cast(self):
        for x, direction in ((30, "left"), (360, "right")):
            chasing = self.decide(marker=(.5, .5), target_box=None, chase_box=(x, 90, 20, 20))
            self.assertEqual((chasing.action, chasing.direction), ("move", direction))
            self.assertTrue(chasing.runtime_state["navigation_active"])
            self.assertIsNone(chasing.pickup_interval_seconds)
            in_range = self.decide(runtime_state=chasing.runtime_state)
            self.assertEqual(in_range.action, "cast")
            self.assertIsNone(in_range.direction)
            missing = self.decide(target_box=None, chase_box=None, runtime_state=chasing.runtime_state)
            self.assertEqual(missing.action, "stop")

    def test_pickup_chase_preserves_phase_and_suspends_collection(self):
        settings = {**self.context.settings, "route_pickup_enabled": True}
        for phase, marker in (("outbound", (.6, .5)), ("collect", (.8, .5)), ("returning", (.6, .5))):
            state = {"phase": phase, "route_started_at": 5., "return_reason": "collected",
                     "dwell_elapsed": .4, "dwell_tick_at": 9.9}
            chasing = self.decide(marker=marker, target_box=None, chase_box=(360, 90, 20, 20),
                                  settings=settings, runtime_state=state)
            self.assertEqual(chasing.action, "move")
            self.assertEqual(chasing.runtime_state["phase"], phase)
            self.assertEqual(chasing.runtime_state["dwell_elapsed"], .4)
            self.assertNotIn("dwell_tick_at", chasing.runtime_state)
            self.assertIsNone(chasing.pickup_interval_seconds)
            self.assertIn("dwell_tick_at", state)

    def test_chase_does_not_bypass_missing_visual_or_safety_route_return(self):
        for changes in ({"marker": None}, {"player_box": None}):
            self.assertEqual(self.decide(target_box=None, chase_box=(360, 90, 20, 20), **changes).action, "stop")
        result = self.decide(target_box=None, chase_box=(360, 90, 20, 20),
                            player_box=None, minimap_only=True)
        self.assertEqual(result.state, "RETURN_CENTER_LEFT")
        result = self.decide(target_box=None, chase_box=(360, 90, 20, 20),
                            runtime_state={"phase": "returning", "return_reason": "disabled"})
        self.assertEqual(result.state, "RETURN_CENTER_LEFT")
