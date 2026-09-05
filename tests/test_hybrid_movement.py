from __future__ import annotations

from pathlib import Path
import threading
import unittest
from unittest.mock import MagicMock, patch

from mbv.bot import BowmanBot
from mbv.config import load_config
from mbv.hybrid_movement import Desktop, HybridMovement, _focus_worker
from mbv.input import Keyboard, input_delivery, vk_for
from mbv.panel import DELIVERY_LABELS
from mbv.strategies import get_strategy
from mbv.window import WindowInfo


class FakeDesktop:
    def __init__(self):
        self.current = 20
        self.identities = {10: 100, 20: 200, 30: 300}
        self.position = (50, 50)
        self.pressed: set[int] = set()
        self.is_idle = True
        self.history = []
        self.fail_focus = False
        self.fail_restore = False

    def foreground(self):
        return self.current

    def pid(self, hwnd):
        return self.identities.get(hwnd, 0)

    def valid(self, hwnd, pid):
        return pid > 0 and self.pid(hwnd) == pid

    def cursor(self):
        return self.position

    def keys_down(self, excluding):
        return bool(self.pressed - excluding)

    def idle(self):
        return self.is_idle and not self.pressed

    def focus(self, hwnd, pid, expected, cancelled):
        self.history.append(("focus", hwnd))
        if self.current != expected or cancelled():
            raise OSError("changed")
        if self.fail_focus or (hwnd == 20 and self.fail_restore):
            raise OSError("activation denied")
        self.current = hwnd
        if cancelled():
            raise OSError("cancelled")


class HybridGuardTests(unittest.TestCase):
    def setUp(self):
        self.desktop = FakeDesktop()
        self.now = 10.0
        self.sent = []

        def send(vk, up):
            self.sent.append((vk, up))
            self.desktop.history.append(("up" if up else "down", vk))
            if up:
                self.desktop.pressed.discard(vk)
            else:
                self.desktop.pressed.add(vk)

        self.send = send
        self.guard = HybridMovement(send, desktop=self.desktop, clock=lambda: self.now, start_watchdog=False)
        self.guard.bind(10)

    def test_begin_verifies_focus_then_release_keeps_game_foreground(self):
        self.assertTrue(self.guard.begin())
        self.guard.down(39, 0.2)
        self.guard.finish()
        self.assertEqual(self.desktop.history, [("focus", 10), ("down", 39), ("up", 39)])
        self.assertFalse(self.guard.held)
        self.assertEqual(self.desktop.current, 10)

    def test_busy_user_defers_without_focus_or_input(self):
        self.desktop.is_idle = False
        self.assertFalse(self.guard.begin())
        self.assertFalse(self.desktop.history)

    def test_cooperative_route_yields_then_reacquires_without_bypassing_guards(self):
        self.guard.begin()
        for elapsed in (0.5, 1., 1.5, 2., 2.4):
            self.now = 10. + elapsed
            self.guard.heartbeat()
            self.guard.down(39)
            self.assertFalse(self.guard.yield_if_due())
        self.now = 12.5
        self.assertTrue(self.guard.yield_if_due())
        self.assertFalse(self.guard.active)
        self.assertEqual(self.desktop.history[-1], ("up", 39))
        self.assertEqual(self.desktop.current, 10)
        self.assertNotIn(("focus", 20), self.desktop.history)
        self.assertFalse(self.guard.begin())
        self.now = 13.3
        self.desktop.is_idle = False
        self.assertFalse(self.guard.begin())
        self.desktop.is_idle = True
        self.assertTrue(self.guard.begin())
        self.assertEqual(self.desktop.current, 10)
        self.guard.finish()

    def test_cooperative_yield_does_not_restore_but_still_detects_user_intervention(self):
        for intervention in (False, True):
            self.setUp()
            self.guard.begin()
            self.now = 12.5
            self.guard.heartbeat_at = self.now
            if intervention:
                self.desktop.current = 30
            else:
                self.desktop.fail_restore = True
            if intervention:
                with self.assertRaises(OSError):
                    self.guard.yield_if_due()
            else:
                self.assertTrue(self.guard.yield_if_due())
                self.guard.check()
            self.assertFalse(self.guard.active)
            if intervention:
                self.assertEqual(self.desktop.current, 30)

    def test_lease_is_reused_for_right_step_and_left_return(self):
        self.guard.begin()
        self.guard.down(39, 0.12)
        self.now += 0.13
        self.guard.poll()
        self.guard.begin()
        self.guard.down(37)
        self.guard.finish()
        self.assertEqual([e for e in self.desktop.history if e[0] == "focus"], [("focus", 10)])

    def test_game_already_foreground_does_not_change_window(self):
        self.desktop.current = 10
        self.guard.begin()
        self.guard.down(39)
        self.guard.finish()
        self.assertEqual([e for e in self.desktop.history if e[0] == "focus"], [])

    def test_focus_denied_never_sends_movement(self):
        self.desktop.fail_focus = True
        with self.assertRaises(OSError):
            self.guard.begin()
        self.assertFalse(self.sent)

    def test_direction_key_cannot_be_sent_without_lease(self):
        with self.assertRaises(OSError):
            self.guard.down(39)
        self.assertFalse(self.sent)

    def test_deadline_releases_without_new_visual_frame(self):
        self.guard.begin()
        self.guard.down(39, 0.12)
        self.now += 0.13
        self.guard.poll()
        self.assertEqual(self.sent, [(39, False), (39, True)])
        self.assertTrue(self.guard.active)

    def test_unbounded_move_has_half_second_key_deadline(self):
        self.guard.begin()
        self.guard.down(39)
        self.now += 0.51
        self.guard.poll()
        self.assertFalse(self.guard.held)

    def test_watchdog_runs_without_main_loop(self):
        self.guard.begin()
        self.guard.down(39, 0.12)
        self.now += 0.13
        stop = MagicMock()
        stop.wait.side_effect = [False, True]
        stop.is_set.return_value = False
        self.guard._watch(stop)
        self.assertFalse(self.guard.held)

    def test_no_heartbeat_aborts_and_keeps_game_foreground(self):
        self.guard.begin()
        self.guard.down(39)
        self.now += 0.81
        self.guard.poll()
        self.assertFalse(self.guard.held)
        self.assertEqual(self.desktop.current, 10)
        with self.assertRaisesRegex(OSError, "心跳"):
            self.guard.check()

    def test_total_lease_cannot_be_extended_by_heartbeats(self):
        self.guard.begin()
        for delta in range(1, 9):
            self.now = 10 + delta * 0.5
            self.guard.heartbeat()
        self.now = 14.1
        self.guard.poll()
        with self.assertRaisesRegex(OSError, "4 秒"):
            self.guard.check()

    def test_user_changes_window_releases_without_stealing_it_back(self):
        self.guard.begin()
        self.guard.down(39)
        self.desktop.current = 30
        self.guard.poll()
        self.assertFalse(self.guard.held)
        self.assertEqual(self.desktop.current, 30)
        self.assertNotIn(("focus", 20), self.desktop.history)
        with self.assertRaises(OSError):
            self.guard.check()

    def test_mouse_input_aborts_without_restoring_over_user_choice(self):
        self.guard.begin()
        self.guard.down(39)
        self.desktop.position = (51, 50)
        self.guard.poll()
        self.assertFalse(self.guard.held)
        self.assertNotIn(("focus", 20), self.desktop.history)

    def test_keyboard_input_aborts_without_sending_more_keydown(self):
        self.guard.begin()
        self.guard.down(39)
        self.desktop.pressed.add(65)
        with self.assertRaises(OSError):
            self.guard.down(37)
        self.assertEqual(self.sent, [(39, False), (39, True)])
        self.assertEqual(self.desktop.current, 10)

    def test_unrestorable_previous_window_does_not_fail_movement(self):
        self.guard.begin()
        self.guard.down(39)
        self.desktop.fail_restore = True
        self.guard.finish()
        self.assertFalse(self.guard.held)
        self.guard.check()
        self.assertEqual(self.desktop.current, 10)
        self.assertNotIn(("focus", 20), self.desktop.history)
        self.assertTrue(self.guard.begin())
        self.guard.finish()

    def test_closed_original_is_not_reactivated(self):
        self.guard.begin()
        self.desktop.identities[20] = 201
        self.guard.finish()
        self.assertNotIn(("focus", 20), self.desktop.history)
        self.guard.check()

    def test_replaced_game_releases_and_does_not_send_new_input(self):
        self.guard.begin()
        self.guard.down(39)
        self.desktop.identities[10] = 101
        with self.assertRaises(OSError):
            self.guard.down(37)
        self.assertEqual(self.sent, [(39, False), (39, True)])

    def test_failed_keydown_gets_compensating_keyup(self):
        self.guard.begin()
        sent = []
        def fail(vk, up):
            sent.append((vk, up))
            if not up:
                raise OSError("failed")
        self.guard.send = fail
        with self.assertRaises(OSError):
            self.guard.down(39)
        self.assertEqual(sent, [(39, False), (39, True)])

    def test_failed_keyup_prevents_restoring_while_key_may_be_held(self):
        self.guard.begin()
        self.guard.down(39)
        self.guard.send = MagicMock(side_effect=OSError("failed"))
        self.guard.finish()
        self.assertNotIn(("focus", 20), self.desktop.history)
        self.assertIn(39, self.guard.held)
        self.guard.send = self.send
        self.guard.finish()
        self.assertFalse(self.guard.held)

    def test_rebind_cannot_clear_error_while_movement_key_release_fails(self):
        self.guard.begin()
        self.guard.down(39)
        self.guard.send = MagicMock(side_effect=OSError("keyup failed"))
        with self.assertRaisesRegex(OSError, "抬键失败"):
            self.guard.bind(10)
        self.assertIn(39, self.guard.held)
        self.assertIsNotNone(self.guard.error)
        self.guard.send = self.send
        self.guard.bind(10)
        self.assertFalse(self.guard.held)
        self.guard.check()

    def test_pause_signal_releases_without_switching_window_or_waiting_for_visual_loop(self):
        self.guard.begin()
        self.guard.down(39)
        self.guard.cancelled = lambda: True
        self.guard.poll()
        self.assertFalse(self.guard.held)
        self.assertEqual(self.desktop.current, 10)
        with self.assertRaisesRegex(OSError, "暂停或退出"):
            self.guard.check()

    def test_generic_modifier_alias_is_not_mistaken_for_manual_input(self):
        self.guard.begin()
        self.guard.down(0x11)
        self.desktop.pressed.add(0xA2)
        self.guard.poll()
        self.assertTrue(self.guard.active)
        self.assertIsNone(self.guard.error)


class BoundedFocusTests(unittest.TestCase):
    def test_timeout_and_cancellation_kill_helper(self):
        for cancelled in (False, True):
            with self.subTest(cancelled=cancelled):
                context = MagicMock()
                parent, child, process = MagicMock(), MagicMock(), MagicMock()
                context.Pipe.return_value = parent, child
                context.Process.return_value = process
                parent.poll.return_value = False
                process.is_alive.return_value = True
                with patch("mbv.hybrid_movement.multiprocessing.get_context", return_value=context), \
                     patch("mbv.hybrid_movement.time.monotonic", side_effect=[10.0, 13.0]):
                    with self.assertRaises(OSError):
                        Desktop().focus(10, 100, 20, lambda: cancelled)
                process.terminate.assert_called_once()
                parent.close.assert_called_once()
                child.close.assert_called_once()

    def test_worker_success_is_not_enough_without_foreground_verification(self):
        context = MagicMock()
        parent, child, process = MagicMock(), MagicMock(), MagicMock()
        context.Pipe.return_value = parent, child
        context.Process.return_value = process
        parent.poll.return_value = True
        parent.recv.return_value = None
        desktop = Desktop()
        desktop.valid = MagicMock(return_value=True)
        desktop.foreground = MagicMock(return_value=30)
        with patch("mbv.hybrid_movement.multiprocessing.get_context", return_value=context):
            with self.assertRaisesRegex(OSError, "身份复核"):
                desktop.focus(10, 100, 20, lambda: False)
        process.terminate.assert_called_once()

    def test_worker_checks_original_foreground_before_touching_windows(self):
        desktop = FakeDesktop()
        connection = MagicMock()
        with patch("mbv.hybrid_movement.Desktop", return_value=desktop), \
             patch("mbv.window.focus_game_window") as activate:
            _focus_worker(connection, 10, 100, 30)
        activate.assert_not_called()
        self.assertIsInstance(connection.send.call_args.args[0], str)
        connection.close.assert_called_once()


class HybridKeyboardTests(unittest.TestCase):
    def setUp(self):
        self.keyboard = Keyboard("hybrid")
        self.desktop = FakeDesktop()
        self.keyboard._send_input = MagicMock()
        self.keyboard._ensure_repeat_thread = MagicMock()
        self.keyboard.hwnd = self.keyboard.root_hwnd = 10
        self.keyboard.hybrid = HybridMovement(self.keyboard._send_input, desktop=self.desktop,
                                              start_watchdog=False)
        self.keyboard.hybrid.bind(10)

    def test_mode_parser_and_panel_registration(self):
        self.assertEqual(input_delivery({"input": {"delivery": "hybrid"}}), "hybrid")
        self.assertIn("hybrid", DELIVERY_LABELS)
        self.assertEqual(input_delivery({"input": {"delivery": "postmessage"}}), "background")

    def test_skills_always_postmessage_even_with_active_foreground_movement(self):
        self.keyboard.prepare_movement()
        self.keyboard.movement_down("left")
        with patch("mbv.input.user32.IsWindow", return_value=True), \
             patch("mbv.input.user32.PostMessageW", return_value=True) as post, \
             patch("mbv.input.time.sleep"):
            for key in ("d", "f", "end", "home", "shift"):
                self.keyboard.tap(key)
            self.keyboard.down("a")
            self.keyboard._repeat_key(vk_for("a"), 0.0)
            self.keyboard.up("a")
        self.assertEqual(post.call_count, 13)
        self.keyboard._send_input.assert_called_once_with(vk_for("left"), False)
        self.keyboard.release_all()

    def test_message_failure_never_falls_back_to_sendinput(self):
        with patch("mbv.input.user32.IsWindow", return_value=True), \
             patch("mbv.input.user32.PostMessageW", return_value=False):
            with self.assertRaises(OSError):
                self.keyboard.tap("d")
        self.keyboard._send_input.assert_not_called()

    def test_jump_movement_uses_scancode_but_skill_on_same_key_still_posts(self):
        self.keyboard.prepare_movement()
        with patch("mbv.input.time.sleep"), patch("mbv.input.user32.IsWindow", return_value=True), \
             patch("mbv.input.user32.PostMessageW", return_value=True) as post:
            self.keyboard.movement_tap("alt")
            self.keyboard.tap("alt")
        self.assertEqual(self.keyboard._send_input.call_args_list[0].args, (vk_for("alt"), False))
        self.assertEqual(self.keyboard._send_input.call_args_list[1].args, (vk_for("alt"), True))
        self.assertEqual(post.call_count, 2)

    def test_release_all_also_releases_foreground_movement_and_keeps_game(self):
        self.keyboard.prepare_movement()
        self.keyboard.movement_down("right")
        self.keyboard.release_all()
        self.assertFalse(self.keyboard.hybrid.held)
        self.assertEqual(self.desktop.current, 10)
        self.keyboard._send_input.assert_any_call(vk_for("right"), True)


class HybridRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.bot = BowmanBot.__new__(BowmanBot)
        bot = self.bot
        bot.config = load_config(Path(__file__).resolve().parents[1] / "config.example.json")
        bot.config["input"]["delivery"] = "hybrid"
        bot.config["strategy"]["active"] = "stationary_attack"
        bot.strategy = get_strategy("stationary_attack")
        bot.config["recognition"]["platform_center"] = {"x": 0.5, "y": 0.5}
        bot.delivery = "hybrid"
        bot.background_input = bot.input_authorized = bot.armed = True
        bot.action_lock = threading.RLock()
        bot.started_at = 1.0
        bot.last_nameplate_seen_at = bot.marker_last_seen = 10.0
        bot.last_attack = bot.last_pickup = bot.last_target_seen = bot.last_periodic_step = 0.0
        bot.last_attack_anchor = (100.0, 100.0)
        bot.direction = None
        bot.keyboard = MagicMock()
        bot.keyboard.prepare_movement.return_value = True
        bot.keyboard.movement_events.return_value = []
        bot.log = MagicMock()
        bot._try_auto_potion = MagicMock(return_value=False)
        bot._try_auto_buff = MagicMock(return_value=False)

    def act(self, marker=(0.7, 0.5), now=10.0):
        with patch("mbv.bot.user32.IsWindow", return_value=True), \
             patch("mbv.bot.user32.IsIconic", return_value=False), \
             patch("mbv.bot.time.monotonic", return_value=now):
            self.bot.act(WindowInfo(10, "NewMaple", 0, 0, 800, 600), 1.0, 1.0,
                         marker, (90, 100, 20, 1), (200, 90, 20, 20), None, 400, True, now, 200)

    def test_return_uses_foreground_movement_then_separate_turn_before_attack(self):
        self.act()
        self.act(now=10.1)
        self.bot.keyboard.movement_down.assert_called_with("left")
        self.bot.keyboard.down.assert_not_called()
        self.act(marker=(0.5, 0.5), now=10.2)
        self.bot.keyboard.finish_movement.assert_called()
        self.assertEqual(self.bot.state, "FACE_TARGET_RIGHT")
        self.bot.keyboard.tap.assert_called_once_with("right", 0.06)
        self.act(marker=(0.5, 0.5), now=10.3)
        self.bot.keyboard.tap.assert_any_call(self.bot.config["keys"]["attack"])

    def test_busy_user_does_not_start_movement_or_consume_step(self):
        self.bot.keyboard.prepare_movement.return_value = False
        self.act()
        self.assertEqual(self.bot.state, "HYBRID_WAIT_IDLE")
        self.bot.keyboard.movement_down.assert_not_called()
        self.assertIsNone(self.bot.move_progress)

    def test_buff_or_potion_interrupt_restores_focus(self):
        for method in ("_try_auto_buff", "_try_auto_potion"):
            self.bot.attack_turn_direction = "left"
            self.bot.attack_turn_requested_at = 9.0
            getattr(self.bot, method).return_value = True
            self.act()
            self.bot.keyboard.finish_movement.assert_called()
            self.bot.keyboard.movement_down.assert_not_called()
            self.assertIsNone(self.bot.attack_turn_direction)
            getattr(self.bot, method).return_value = False

    def test_visual_or_marker_loss_releases_focus(self):
        self.act(marker=None)
        self.bot.keyboard.finish_movement.assert_called()
        self.bot.keyboard.movement_down.assert_not_called()

    def test_down_jump_routes_both_keys_to_movement_only(self):
        with patch("mbv.bot.time.sleep"):
            self.bot.down_jump_to_safe(10.0, "DOWN_JUMP")
        self.bot.keyboard.movement_down.assert_called_once_with(self.bot.config["keys"]["down"])
        self.bot.keyboard.movement_tap.assert_called_once_with(self.bot.config["keys"]["jump"])
        self.bot.keyboard.tap.assert_not_called()

    def test_disarm_releases_both_input_lanes(self):
        self.bot.notify = MagicMock()
        self.bot.disarm("test")
        self.bot.keyboard.release_all.assert_called_once()
        self.assertFalse(self.bot.armed)

    def test_latched_health_error_pauses_instead_of_crashing_and_blocks_paused_potions(self):
        self.bot.notify = MagicMock()
        self.bot.keyboard.check_health.side_effect = OSError("混合后台停止：视觉或行动心跳中断")
        with patch("mbv.bot.user32.MessageBeep"):
            self.act()
        self.assertFalse(self.bot.armed)
        self.assertTrue(self.bot._hybrid_input_fault)
        self.assertEqual(self.bot.state, "PAUSED")
        self.bot.keyboard.release_all.assert_called_once()
        self.bot.notify.assert_called_once()
        self.bot._try_auto_potion.reset_mock()
        self.act(now=11.)
        self.act(now=12.)
        self.bot._try_auto_potion.assert_not_called()
        self.bot.keyboard.movement_down.assert_not_called()
        self.assertEqual(self.bot.notify.call_count, 1)

    def test_heartbeat_exception_in_finally_also_pauses_cleanly(self):
        self.bot.notify = MagicMock()
        self.bot.keyboard.movement_heartbeat.side_effect = OSError("混合后台停止：检测到用户操作")
        with patch("mbv.bot.user32.MessageBeep"):
            self.act()
        self.assertFalse(self.bot.armed)
        self.bot.keyboard.release_all.assert_called_once()
        self.bot.notify.assert_called_once()

    def test_release_failure_is_reported_without_restarting_worker_or_input(self):
        self.bot.notify = MagicMock()
        self.bot.keyboard.check_health.side_effect = OSError("input failed")
        self.bot.keyboard.release_all.side_effect = OSError("release failed")
        self.act()
        self.assertFalse(self.bot.armed)
        self.assertTrue(self.bot._hybrid_input_fault)
        self.assertIn("抬键失败", self.bot.notify.call_args.args[0])

    def test_non_hybrid_or_programming_errors_are_not_swallowed(self):
        self.bot._act = MagicMock(side_effect=ValueError("bug"))
        with self.assertRaisesRegex(ValueError, "bug"):
            self.act()
        self.bot.delivery = "foreground"
        self.bot._act.side_effect = OSError("foreground failed")
        with self.assertRaisesRegex(OSError, "foreground failed"):
            self.act()

    def test_new_focus_requires_a_new_visual_frame_before_moving(self):
        self.bot.keyboard.hybrid.active = False
        def activate():
            self.bot.keyboard.hybrid.active = True
            return True
        self.bot.keyboard.prepare_movement.side_effect = activate
        self.act()
        self.assertEqual(self.bot.state, "HYBRID_WAIT_FRAME")
        self.bot.keyboard.movement_down.assert_not_called()
        self.act(now=10.1)
        self.act(now=10.2)
        self.bot.keyboard.movement_down.assert_called_with("left")

    def test_toggle_hybrid_does_not_focus_or_make_game_topmost(self):
        bot = self.bot
        bot._hybrid_input_fault = True
        bot.armed = False
        bot.integrity_ok = True
        bot.config["calibrated"] = True
        bot.config["window"]["topmost_while_armed"] = True
        bot.player_templates = [MagicMock()]
        bot.templates = []
        bot.notify = MagicMock()
        window = WindowInfo(10, "NewMaple", 0, 0, 800, 600)
        with patch("mbv.bot.missing_recognition_data", return_value=[]), \
             patch("mbv.bot.user32.IsWindow", return_value=True), \
             patch("mbv.bot.user32.IsIconic", return_value=False), \
             patch("mbv.bot.user32.GetForegroundWindow", return_value=20), \
             patch("mbv.bot.client_window", return_value=window), \
             patch("mbv.bot.focus_game_window") as focus, \
             patch("mbv.bot.set_window_topmost") as topmost, \
             patch("mbv.bot.user32.MessageBeep"):
            bot._toggle(window)
        self.assertTrue(bot.armed)
        self.assertFalse(bot._hybrid_input_fault)
        focus.assert_not_called()
        topmost.assert_not_called()


if __name__ == "__main__":
    unittest.main()
