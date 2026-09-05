from __future__ import annotations

import threading
import unittest
from unittest.mock import MagicMock, patch

from mbv.bot import BowmanBot
from mbv.input import Keyboard, vk_for
from mbv.strategies.base import StrategyActionContext
from mbv.strategies.common.stationary_attack import StationaryAttackStrategy


class MovementStrategyTests(unittest.TestCase):
    def context(self, marker, **changes):
        values = dict(marker=marker, player_box=(100, 100, 20, 1), player_anchor=None,
                      target_box=(200, 100, 20, 20), chase_box=None, combat_width=1000,
                      has_monster_candidates=True, now=50.0, last_target_seen=49.0,
                      last_pickup=0.0, direction="left", behavior={}, settings={},
                      recognition={"platform_center": {"x": 0.5, "y": 0.5}})
        values.update(changes)
        return StrategyActionContext(**values)

    def test_drift_preempts_attack_and_due_step_without_pending_return(self):
        for x, direction in ((0.54, "left"), (0.46, "right")):
            decision = StationaryAttackStrategy().decide(self.context((x, 0.5)))
            self.assertEqual((decision.action, decision.direction), ("move", direction))

    def test_vertical_drift_is_handled_without_pending_return(self):
        for y, action in ((0.4, "down_jump"), (0.6, "jump")):
            self.assertEqual(StationaryAttackStrategy().decide(self.context((0.5, y))).action, action)

    def test_missing_marker_never_moves(self):
        self.assertEqual(StationaryAttackStrategy().decide(self.context(None)).action, "stop")


class MovementInputTests(unittest.TestCase):
    def fixture(self, mode="window_message"):
        keyboard = Keyboard(mode)
        keyboard._dispatch = MagicMock()
        keyboard._ensure_repeat_thread = MagicMock()
        return keyboard

    def test_pulse_has_keyup_gap_and_fresh_keydown(self):
        keyboard = self.fixture()
        with patch("mbv.input.time.monotonic", return_value=0.0):
            keyboard.movement_down("left")
        with keyboard._lock:
            keyboard._repeat_key(37, 0.11)
            keyboard._repeat_key(37, 0.13)
            self.assertEqual(keyboard._dispatch.call_count, 2)
            keyboard._repeat_key(37, 0.17)
        self.assertEqual(keyboard._dispatch.call_args_list[-1].kwargs,
                         {"key_up": False, "was_down": False})
        keyboard.release_all()
        self.assertEqual(keyboard._dispatch.call_args_list[-1].args, (37, True))

    def test_timed_step_releases_without_next_vision_frame(self):
        for mode in ("foreground", "background", "window_message"):
            keyboard = self.fixture(mode)
            with patch("mbv.input.time.monotonic", return_value=0.0):
                keyboard.movement_down("right", seconds=0.12)
            with keyboard._lock:
                keyboard._repeat_key(39, 0.13)
            self.assertFalse(keyboard.held)
            self.assertFalse(keyboard._movement_deadlines)
            self.assertFalse(keyboard._movement_pulses)
            self.assertEqual(keyboard._dispatch.call_args_list[-1].args, (39, True))

    def test_release_cancels_pulse_and_next_press_starts_fresh(self):
        keyboard = self.fixture()
        keyboard.movement_down("right", seconds=0.12)
        keyboard.release_all()
        self.assertFalse(keyboard._movement_deadlines)
        self.assertFalse(keyboard._movement_pulses)
        keyboard.movement_down("right")
        self.assertEqual(keyboard._dispatch.call_args_list[-1].args, (39, False))

    def test_old_input_modes_do_not_pulse(self):
        for mode in ("foreground", "background"):
            keyboard = self.fixture(mode)
            keyboard.movement_down("left")
            self.assertFalse(keyboard._movement_pulses)
            keyboard.release_all()

    def test_failed_initial_press_is_not_recorded_as_held(self):
        for method in ("down", "movement_down"):
            keyboard = self.fixture()
            keyboard._dispatch.side_effect = OSError("denied")
            with self.assertRaises(OSError):
                getattr(keyboard, method)("right")
            self.assertFalse(keyboard.held)

    def test_repeat_failure_is_visible_to_main_loop(self):
        keyboard = self.fixture()
        keyboard.held.add(37)
        keyboard._dispatch.side_effect = OSError("denied")
        stop = MagicMock()
        stop.wait.side_effect = [False, True]
        stop.is_set.return_value = False
        keyboard._repeat_held_keys(stop)
        with self.assertRaisesRegex(OSError, "denied"):
            keyboard.check_health()
        keyboard.release_all()
        keyboard.check_health()

    def test_real_repeat_thread_releases_timed_move(self):
        keyboard = Keyboard("window_message")
        released = threading.Event()
        with patch.object(keyboard, "_dispatch", side_effect=lambda vk, key_up, **kw:
                          released.set() if key_up else None):
            try:
                keyboard.movement_down("right", seconds=0.03)
                self.assertTrue(released.wait(0.8))
                with keyboard._lock:
                    self.assertNotIn(vk_for("right"), keyboard.held)
            finally:
                keyboard.release_all()


class MovementRuntimeTests(unittest.TestCase):
    def fixture(self):
        bot = BowmanBot.__new__(BowmanBot)
        bot.config = {"keys": {"left": "left", "right": "right"}}
        bot.delivery = "window_message"
        bot.keyboard = MagicMock()
        bot.log = MagicMock()
        bot.disarm = MagicMock()
        bot.live_minimap_width = 100
        bot.last_periodic_step = 0.0
        bot.periodic_step_pending_return = False
        return bot

    def attempt(self, bot, start, result_x):
        bot._advance_periodic_step((0.5, 0.5), start)
        hold_deadline = bot.step_motion.deadline
        bot._advance_periodic_step((0.5, 0.5), hold_deadline + 0.001)
        bot._advance_periodic_step((result_x, 0.5), bot.step_motion.deadline + 0.001)

    def test_step_only_completes_after_measured_displacement(self):
        bot = self.fixture()
        bot.periodic_step("right", 0.12, 1.0, "PERIODIC_STEP_RIGHT")
        bot._advance_periodic_step((0.5, 0.5), 1.3)
        bot.keyboard.movement_down.assert_not_called()
        self.attempt(bot, 1.61, 0.51)
        bot.keyboard.movement_down.assert_called_once_with("right", seconds=0.12)
        self.assertTrue(bot.periodic_step_pending_return)
        self.assertIsNone(bot.step_motion)
        self.assertGreater(bot.last_periodic_step, 1.0)

    def test_no_displacement_retries_three_times_then_stops(self):
        bot = self.fixture()
        bot.periodic_step("right", 0.12, 1.0, "PERIODIC_STEP_RIGHT")
        for _ in range(3):
            self.attempt(bot, bot.step_motion.deadline + 0.001, 0.5)
        self.assertFalse(bot.periodic_step_pending_return)
        self.assertEqual(bot.last_periodic_step, 0.0)
        self.assertEqual(bot.keyboard.movement_down.call_count, 3)
        bot.disarm.assert_called_once()

    def test_wrong_direction_does_not_count_as_success(self):
        bot = self.fixture()
        bot.periodic_step("right", 0.12, 1.0, "PERIODIC_STEP_RIGHT")
        self.attempt(bot, 1.61, 0.49)
        bot.disarm.assert_called_once()
        self.assertFalse(bot.periodic_step_pending_return)

    def test_interruption_requires_new_preparation_and_baseline(self):
        bot = self.fixture()
        bot.periodic_step("right", 0.12, 1.0, "PERIODIC_STEP_RIGHT")
        bot._advance_periodic_step((0.5, 0.5), 1.61)
        with patch("mbv.bot.time.monotonic", return_value=1.65):
            bot._interrupt_step()
        bot.stop_move()
        self.assertEqual(bot.step_motion.phase, "prepare")
        self.assertIsNone(bot.step_motion.baseline)
        bot._advance_periodic_step((0.6, 0.5), 2.0)
        self.assertEqual(bot.keyboard.movement_down.call_count, 1)

    def test_return_prepares_retries_and_stops_if_stalled(self):
        bot = self.fixture()
        for now in (1.0, 1.3):
            bot._move_with_feedback("left", (0.6, 0.5), now, "RETURN_CENTER_LEFT")
        bot.keyboard.movement_down.assert_not_called()
        for now in (1.7, 3.7, 3.9, 5.7):
            bot._move_with_feedback("left", (0.6, 0.5), now, "RETURN_CENTER_LEFT")
        bot.disarm.assert_called_once()
        self.assertTrue(any(c.args[0] == "movement_retry" for c in bot.log.write.call_args_list))

    def test_only_correct_direction_progress_refreshes_watchdog(self):
        bot = self.fixture()
        bot._move_with_feedback("left", (0.6, 0.5), 1.0, "RETURN_CENTER_LEFT")
        bot._move_with_feedback("left", (0.59, 0.5), 3.0, "RETURN_CENTER_LEFT")
        bot._move_with_feedback("left", (0.60, 0.5), 4.0, "RETURN_CENTER_LEFT")
        self.assertEqual(bot.move_progress.progress_at, 3.0)
        bot._move_with_feedback("left", (0.60, 0.5), 7.1, "RETURN_CENTER_LEFT")
        bot.disarm.assert_called_once()

    def test_stop_resets_return_watchdog(self):
        bot = self.fixture()
        bot._move_with_feedback("left", (0.6, 0.5), 1.0, "RETURN_CENTER_LEFT")
        bot.stop_move()
        self.assertIsNone(bot.move_progress)

    def test_return_does_not_repeat_attack_wait_after_potion_interruption(self):
        bot = self.fixture()
        bot.last_attack = 1.0
        bot._move_with_feedback("left", (0.6, 0.5), 10.0, "RETURN_CENTER_LEFT")
        self.assertEqual(bot.move_progress.ready_at, 10.0)

    def test_tiny_marker_jitter_does_not_verify_step(self):
        bot = self.fixture()
        bot.periodic_step("right", 0.12, 1.0, "PERIODIC_STEP_RIGHT")
        self.attempt(bot, 1.61, 0.501)
        self.assertFalse(bot.periodic_step_pending_return)
        self.assertEqual(bot.step_motion.phase, "prepare")
