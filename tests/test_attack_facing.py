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
        self.bot.log = MagicMock()
        self.bot.direction = "left"
        self.bot.last_attack = 0.0
        self.bot._reset_attack_facing()

    def attack(self, now, target=0.2, **kwargs):
        with patch("mbv.bot.time.monotonic", return_value=now):
            self.bot.face_and_attack(target, 0.5, now, face_each_attack=False, **kwargs)

    def test_cached_movement_direction_does_not_skip_first_turn(self):
        self.attack(10.0)
        self.bot.keyboard.tap.assert_called_once_with("left", 0.06)
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
        self.bot.keyboard.tap.assert_called_once_with("left", 0.06)
        self.attack(10.70)
        self.assertEqual(self.bot.keyboard.tap.call_args_list,
                         [call("left", 0.06), call("shift")])
        self.assertEqual(self.bot.state, "ATTACK_LEFT")

    def test_periodic_refresh_recovers_without_a_target_side_change(self):
        self.attack(10.0)
        self.attack(10.1)
        self.attack(14.8)
        self.bot.keyboard.tap.reset_mock()
        self.attack(15.1)
        self.bot.keyboard.tap.assert_not_called()
        self.assertEqual(self.bot.last_attack, 14.8)
        self.attack(15.41)
        self.bot.keyboard.tap.assert_called_once_with("left", 0.06)
        self.assertEqual(self.bot.log.write.call_args.kwargs["reason"], "refresh")
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
        self.bot.keyboard.tap.assert_called_once_with("right", 0.06)
        self.attack(10.80, target=0.8)
        self.bot.keyboard.tap.assert_any_call("shift")

    def test_target_flipping_during_settle_never_sends_skill(self):
        self.attack(10.0)
        self.attack(10.02, target=0.8)
        self.attack(10.04)
        self.assertEqual(self.bot.keyboard.tap.call_args_list,
                         [call("left", 0.06), call("right", 0.06), call("left", 0.06)])
        self.assertEqual(self.bot.last_attack, 0.0)

    def test_move_invalidates_turn_even_if_returning_to_same_direction(self):
        self.attack(10.0)
        self.bot.move("left")
        self.assertIsNone(self.bot.attack_turn_direction)
        self.bot.keyboard.reset_mock()
        self.attack(10.2)
        self.bot.keyboard.tap.assert_called_once_with("left", 0.06)

    def test_reset_cancels_pending_turn(self):
        self.attack(10.0)
        self.bot._reset_attack_facing()
        self.bot.keyboard.reset_mock()
        self.attack(10.2)
        self.bot.keyboard.tap.assert_called_once_with("left", 0.06)

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

    def test_all_input_modes_keep_normal_turn_channel_without_foreground_fallback(self):
        for delivery in ("foreground", "background", "window_message", "hybrid"):
            with self.subTest(delivery=delivery):
                self.bot.delivery = delivery
                self.bot._reset_attack_facing()
                self.bot.last_attack = 0.0
                self.bot.keyboard.reset_mock()
                self.attack(10.0)
                self.attack(10.1, attack_key="m", attack_skill="melee")
                self.assertEqual(self.bot.keyboard.tap.call_args_list,
                                 [call("left", 0.06), call("m")])
                self.bot.keyboard.movement_down.assert_not_called()
                self.bot.keyboard.prepare_movement.assert_not_called()
                self.bot.keyboard.down.assert_not_called()


if __name__ == "__main__":
    unittest.main()
