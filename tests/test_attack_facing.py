import unittest
from unittest.mock import MagicMock, call, patch

from mbv.bot import BowmanBot


class AttackFacingTests(unittest.TestCase):
    def setUp(self):
        self.bot = BowmanBot.__new__(BowmanBot)
        self.bot.config = {
            "keys": {"left": "left", "right": "right", "attack": "shift"},
            "behavior": {"attack_interval_seconds": 0.22, "face_tap_seconds": 0.025,
                         "attack_dead_zone": 0.015},
        }
        self.bot.keyboard = MagicMock()
        self.bot.keyboard.hybrid.hwnd = 10
        self.bot.keyboard.hybrid.desktop.foreground.return_value = 10
        self.bot.log = MagicMock()
        self.bot.direction = "left"
        self.bot.last_attack = 0.0
        self.bot._reset_attack_facing()

    def attack(self, now, target=0.2, **kwargs):
        with patch("mbv.bot.time.monotonic", return_value=now):
            self.bot.face_and_attack(target, 0.5, now, face_each_attack=False, **kwargs)

    def test_route_turn_protection_survives_first_attack_and_opposite_target(self):
        self.bot.delivery = "foreground"
        self.bot.move("right")
        self.bot.keyboard.reset_mock()
        self.attack(10., face_tap_seconds=.08)
        self.attack(10.13, face_tap_seconds=.08)
        self.attack(10.26, face_tap_seconds=.08)
        self.attack(10.9, target=.8, face_tap_seconds=.08)
        self.assertEqual(self.bot.keyboard.tap.call_args_list, [call("left", .08), call("shift")])
        self.attack(11.03, target=.8, face_tap_seconds=.08)
        self.assertEqual(self.bot.keyboard.tap.call_args, call("right", .08))
        self.assertEqual(self.bot.log.write.call_args.kwargs["reason"], "foreground_route_combat")
        self.attack(11.10, target=.8, face_tap_seconds=.08)
        self.assertEqual(self.bot.keyboard.tap.call_args, call("right", .08))
        for tick in range(40):
            self.attack(11.16 + tick * .25, target=.8, face_tap_seconds=.08)
        self.assertEqual(sum(c.args[0] == "right" for c in self.bot.keyboard.tap.call_args_list), 1)
        self.assertEqual(sum(c.args[0] == "shift" for c in self.bot.keyboard.tap.call_args_list), 41)

    def test_route_invalidation_still_uses_protected_turn_and_longer_config(self):
        self.bot.config["behavior"]["face_tap_seconds"] = .15
        for now in (10., 11.):
            self.bot._reset_attack_facing()
            self.bot.keyboard.reset_mock()
            self.attack(now, face_tap_seconds=.08)
            self.bot.keyboard.tap.assert_not_called()
            self.attack(now + .13, face_tap_seconds=.08)
            self.bot.keyboard.tap.assert_called_once_with("left", .15)

    def test_route_override_does_not_change_other_modes(self):
        for delivery in ("background", "window_message", "hybrid"):
            with self.subTest(delivery=delivery):
                self.bot.delivery = delivery
                self.bot._reset_attack_facing()
                self.bot.keyboard.reset_mock()
                with patch.object(self.bot, "_prepare_hybrid_movement", return_value=True):
                    self.attack(10., face_tap_seconds=.08)
                channel = self.bot.keyboard.movement_tap if delivery == "hybrid" else self.bot.keyboard.tap
                channel.assert_called_once_with("left", .025)

    def test_cached_movement_direction_does_not_skip_first_turn(self):
        self.attack(10.0)
        self.bot.keyboard.tap.assert_called_once_with("left", 0.025)
        self.assertEqual(self.bot.state, "FACE_TARGET_LEFT")
        self.assertEqual(self.bot.last_attack, 0.0)
        self.assertFalse(self.bot.log.write.call_args.kwargs["facing_verified"])

    def test_turn_waits_for_previous_skill_then_settles_before_attack(self):
        self.bot.last_attack = 10.0
        self.attack(10.3)
        self.bot.keyboard.tap.assert_not_called()
        self.assertEqual(self.bot.state, "FACE_TARGET_LEFT")
        self.attack(10.61)
        self.attack(10.65)
        self.bot.keyboard.tap.assert_called_once_with("left", 0.025)
        self.attack(10.70)
        self.assertEqual(self.bot.keyboard.tap.call_args_list,
                         [call("left", 0.025), call("shift")])
        self.assertEqual(self.bot.state, "ATTACK_LEFT")

    def test_message_same_side_does_not_periodically_walk(self):
        self.bot.delivery = "window_message"
        self.attack(10.0)
        self.attack(10.1)
        self.attack(14.8)
        self.bot.keyboard.tap.reset_mock()
        self.attack(15.1)
        self.bot.keyboard.tap.assert_called_once_with("shift")
        self.assertEqual(self.bot.last_attack, 15.1)
        self.attack(15.41)
        self.assertEqual(self.bot.keyboard.tap.call_args_list, [call("shift"), call("shift")])
        self.attack(15.50)
        self.bot.keyboard.tap.assert_any_call("shift")
        self.bot.keyboard.down.assert_not_called()
        self.bot.keyboard.movement_down.assert_not_called()

    def test_side_change_waits_for_animation_instead_of_attacking_old_side(self):
        self.attack(10.0)
        self.attack(10.1)
        self.bot.keyboard.tap.reset_mock()
        self.attack(10.4, target=0.8)
        self.bot.keyboard.tap.assert_not_called()
        self.assertEqual(self.bot.state, "FACE_TARGET_RIGHT")
        self.attack(10.71, target=0.8)
        self.bot.keyboard.tap.assert_called_once_with("right", 0.025)
        self.attack(10.80, target=0.8)
        self.bot.keyboard.tap.assert_any_call("shift")

    def test_target_flipping_during_settle_never_sends_skill(self):
        self.attack(10.0)
        self.attack(10.02, target=0.8)
        self.attack(10.04)
        self.assertEqual(self.bot.keyboard.tap.call_args_list,
                         [call("left", 0.025), call("right", 0.025), call("left", 0.025)])
        self.assertEqual(self.bot.last_attack, 0.0)

    def test_move_invalidates_turn_even_if_returning_to_same_direction(self):
        self.attack(10.0)
        self.bot.move("left")
        self.assertIsNone(self.bot.attack_turn_direction)
        self.bot.keyboard.reset_mock()
        self.attack(10.2)
        self.bot.keyboard.tap.assert_not_called()
        self.attack(10.33)
        self.bot.keyboard.tap.assert_called_once_with("left", 0.08)
        self.attack(10.4)
        self.bot.keyboard.tap.assert_called_once_with("left", 0.08)
        self.attack(10.46)
        self.bot.keyboard.tap.assert_any_call("shift")

    def test_foreground_pickup_release_precedes_stable_return_turn(self):
        self.bot.delivery = "foreground"
        self.bot._pickup_held_key = "z"
        self.attack(10., target=.8)
        self.bot.keyboard.up.assert_any_call("z")
        self.bot.keyboard.tap.assert_not_called()
        self.assertEqual(self.bot.direction, "left")
        self.attack(10.1, target=.8)
        self.bot.keyboard.tap.assert_not_called()
        self.attack(10.13, target=.8)
        self.bot.keyboard.tap.assert_called_once_with("right", .08)
        self.assertEqual(self.bot.log.write.call_args.kwargs["reason"], "foreground_after_movement")
        self.attack(10.2, target=.8)
        self.bot.keyboard.tap.assert_called_once_with("right", .08)
        for tick in range(1, 41):
            self.attack(10.13 + tick * .25, target=.8)
        self.assertEqual(sum(c.args[0] == "right" for c in self.bot.keyboard.tap.call_args_list), 1)
        self.assertEqual(sum(c.args[0] == "shift" for c in self.bot.keyboard.tap.call_args_list), 40)

    def test_return_target_flip_restarts_stability_wait_without_turn_spam(self):
        self.bot.move("left")
        self.bot.keyboard.reset_mock()
        self.attack(10., target=.8)
        self.attack(10.1, target=.2)
        self.attack(10.2, target=.8)
        self.bot.keyboard.tap.assert_not_called()
        self.attack(10.33, target=.8)
        self.bot.keyboard.tap.assert_called_once_with("right", .08)

    def test_return_turn_does_not_skip_previous_skill_idle(self):
        self.bot.move("left")
        self.bot.last_attack = 10.
        self.attack(10.1)
        self.attack(10.3)
        self.bot.keyboard.tap.assert_not_called()
        self.attack(10.61)
        self.bot.keyboard.tap.assert_called_once_with("left", .08)

    def test_return_turn_send_failure_keeps_reorientation_required(self):
        self.bot.move("left")
        self.attack(10.)
        self.bot.keyboard.tap.side_effect = OSError("发送失败")
        with self.assertRaises(OSError):
            self.attack(10.13)
        self.assertTrue(self.bot._foreground_reorient_required)
        self.assertIsNone(self.bot.attack_turn_direction)

    def test_non_foreground_post_move_turn_timing_is_unchanged(self):
        for delivery in ("background", "window_message", "hybrid"):
            with self.subTest(delivery=delivery):
                self.bot.delivery = delivery
                self.bot.move("left")
                self.bot._pickup_held_key = "z"
                self.bot.keyboard.reset_mock()
                with patch.object(self.bot, "_prepare_hybrid_movement", return_value=True):
                    self.attack(10.)
                channel = self.bot.keyboard.movement_tap if delivery == "hybrid" else self.bot.keyboard.tap
                channel.assert_called_once_with("left", .025)

    def test_longer_foreground_return_tap_configuration_is_preserved(self):
        self.bot.config["behavior"]["face_tap_seconds"] = .15
        self.bot.move("left")
        self.attack(10.)
        self.attack(10.13)
        self.bot.keyboard.tap.assert_called_once_with("left", .15)

    def test_reset_cancels_pending_turn(self):
        self.attack(10.0)
        self.bot._reset_attack_facing()
        self.bot.keyboard.reset_mock()
        self.attack(10.2)
        self.bot.keyboard.tap.assert_called_once_with("left", 0.025)

    def test_key_failure_does_not_cache_success_or_send_attack(self):
        self.bot.keyboard.tap.side_effect = OSError("发送失败")
        with self.assertRaises(OSError):
            self.attack(10.0)
        self.assertIsNone(self.bot.attack_turn_direction)
        self.assertEqual(self.bot.last_attack, 0.0)

    def test_longer_user_turn_duration_is_preserved(self):
        self.bot.config["behavior"]["face_tap_seconds"] = 0.1
        self.attack(10.0)
        self.bot.keyboard.tap.assert_called_once_with("left", 0.1)

    def test_tiny_configured_turn_has_twenty_millisecond_floor(self):
        self.bot.config["behavior"]["face_tap_seconds"] = 0.001
        self.attack(10.)
        self.bot.keyboard.tap.assert_called_once_with("left", 0.02)

    def test_dynamic_all_modes_only_turn_once_for_continuous_same_side_attacks(self):
        for delivery in ("foreground", "background", "window_message", "hybrid"):
            with self.subTest(delivery=delivery):
                self.bot.delivery = delivery
                self.bot.last_attack = 0.
                self.bot._reset_attack_facing()
                self.bot.keyboard.reset_mock()
                with patch.object(self.bot, "_prepare_hybrid_movement", return_value=True):
                    for tick in range(81):
                        now = 10. + tick * .25
                        with patch("mbv.bot.time.monotonic", return_value=now):
                            self.bot.face_and_attack(.2, .5, now, face_each_attack=True)
                turns = self.bot.keyboard.movement_tap if delivery == "hybrid" else self.bot.keyboard.tap
                self.assertEqual([c for c in turns.call_args_list if c.args[0] == "left"],
                                 [call("left", .025)])
                self.assertEqual(sum(c.args[0] == "shift" for c in self.bot.keyboard.tap.call_args_list), 80)
                self.bot.keyboard.down.assert_not_called()
                self.bot.keyboard.movement_down.assert_not_called()

    def test_auxiliary_non_directional_key_keeps_facing(self):
        self.attack(10.)
        for key in ("end", "home", "a"):
            self.bot._invalidate_facing_for_auxiliary_key(key)
            self.assertEqual(self.bot.attack_turn_direction, "left")
        self.attack(10.2)
        self.assertEqual(self.bot.keyboard.tap.call_args_list, [call("left", .025), call("shift")])

    def test_auxiliary_movement_key_invalidates_facing(self):
        self.bot.config["keys"].update(left="a", right="d", jump="alt")
        for key in ("a", "d", "alt", "left", "right", "up", "down"):
            with self.subTest(key=key):
                self.bot.attack_turn_direction = "left"
                self.bot._invalidate_facing_for_auxiliary_key(key)
                self.assertIsNone(self.bot.attack_turn_direction)

    def test_short_visual_gap_does_not_restart_pending_turn_settle(self):
        self.attack(10.0)
        ready_at = self.bot.attack_facing_ready_at
        self.bot._observe_attack_facing_localization(10.02, False)
        self.bot._observe_attack_facing_localization(10.05, True)
        self.assertEqual(self.bot.attack_facing_ready_at, ready_at)
        self.attack(10.06)
        self.bot.keyboard.tap.assert_called_once_with("left", 0.025)
        self.attack(10.1)
        self.bot.keyboard.tap.assert_any_call("shift")

    def test_repeated_missing_frames_do_not_extend_visual_grace(self):
        self.attack(10.0)
        for now in (10.1, 10.2, 10.3, 10.4):
            self.bot._observe_attack_facing_localization(now, False)
            self.assertEqual(self.bot.attack_turn_direction, "left")
        self.bot._observe_attack_facing_localization(10.6, False)
        self.assertIsNone(self.bot.attack_turn_direction)
        self.assertEqual(self.bot.attack_facing_missing_since, 10.1)

    def test_short_visual_gap_after_five_seconds_does_not_repeat_turn(self):
        self.bot.delivery = "window_message"
        self.attack(10.0)
        self.bot._observe_attack_facing_localization(14.8, False)
        self.bot._observe_attack_facing_localization(15.1, True)
        self.assertEqual(self.bot.attack_turn_requested_at, 10.0)
        self.bot.keyboard.reset_mock()
        self.attack(15.1)
        self.bot.keyboard.tap.assert_called_once_with("shift")

    def test_foreground_same_side_has_no_five_second_refresh_pause(self):
        self.bot.delivery = "foreground"
        self.attack(10.)
        for tick in range(1, 81):
            self.attack(10. + tick * .25)
        direction_calls = [c for c in self.bot.keyboard.tap.call_args_list if c.args[0] == "left"]
        self.assertEqual(direction_calls, [call("left", .025)])
        self.assertEqual(sum(c.args[0] == "shift" for c in self.bot.keyboard.tap.call_args_list), 80)
        self.assertEqual(self.bot.last_attack, 30.)

    def test_foreground_still_reorients_when_cache_invalidated_after_long_attack(self):
        self.bot.delivery = "foreground"
        self.attack(10.)
        self.attack(10.1)
        self.bot._reset_attack_facing()
        self.bot.keyboard.reset_mock()
        self.attack(10.4)
        self.bot.keyboard.tap.assert_not_called()
        self.attack(10.71)
        self.bot.keyboard.tap.assert_called_once_with("left", .025)

    def test_action_diagnostics_record_no_target_and_are_rate_limited(self):
        self.bot.armed = True
        self.bot.last_attack = 9.
        self.bot.state = "TARGET_OUT_OF_RANGE"
        self.bot._record_action_state(10., (1, 2, 3, 4), None, (.5, .5), True)
        self.assertEqual(self.bot.log.write.call_args.args[0], "action_state")
        self.assertFalse(self.bot.log.write.call_args.kwargs["target_candidate"])
        self.assertEqual(self.bot.log.write.call_args.kwargs["since_attack_seconds"], 1.)
        for now in (10.1, 10.5, 11., 14.9):
            self.bot._record_action_state(now, (1, 2, 3, 4), None, (.5, .5), True)
        self.assertEqual(self.bot.log.write.call_count, 1)
        self.bot._record_action_state(15., None, None, None, False)
        self.assertEqual(self.bot.log.write.call_count, 2)

    def test_action_diagnostics_report_changed_gate_without_per_frame_spam(self):
        self.bot.armed = True
        self.bot.state = "FACE_TARGET_LEFT"
        self.bot._record_action_state(10., None, None, None, False)
        self.bot.state = "ATTACK_LEFT"
        self.bot._record_action_state(10.1, None, None, None, False)
        self.assertEqual(self.bot.log.write.call_count, 1)
        self.bot._record_action_state(11.1, None, None, None, False)
        self.assertEqual(self.bot.log.write.call_args.kwargs["state"], "ATTACK_LEFT")

    def test_non_hybrid_input_modes_keep_normal_turn_channel_without_foreground_fallback(self):
        for delivery in ("foreground", "background", "window_message"):
            with self.subTest(delivery=delivery):
                self.bot.delivery = delivery
                self.bot._reset_attack_facing()
                self.bot.last_attack = 0.0
                self.bot.keyboard.reset_mock()
                self.attack(10.0)
                self.attack(10.1, attack_key="m", attack_skill="melee")
                self.assertEqual(self.bot.keyboard.tap.call_args_list,
                                 [call("left", 0.025), call("m")])
                self.bot.keyboard.movement_down.assert_not_called()
                self.bot.keyboard.prepare_movement.assert_not_called()
                self.bot.keyboard.down.assert_not_called()


if __name__ == "__main__":
    unittest.main()
