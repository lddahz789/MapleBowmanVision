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

    def test_turn_lease_never_activates_background_game(self):
        self.assertFalse(self.guard.begin(allow_focus=False))
        self.assertEqual(self.desktop.current, 20)
        self.assertFalse(self.guard.active)
        self.assertFalse(self.desktop.history)
        self.assertFalse(self.sent)

    def test_turn_lease_accepts_existing_foreground_without_focus_call(self):
        self.desktop.current = 10
        self.assertTrue(self.guard.begin(allow_focus=False))
        self.guard.down(39, .025)
        self.guard.finish()
        self.assertEqual(self.desktop.history, [("down", 39), ("up", 39)])

    def test_focus_loss_during_turn_lease_acquisition_does_not_reactivate(self):
        self.desktop.current = 10
        def lose_focus_during_idle():
            self.desktop.current = 20
            return True
        self.desktop.idle = lose_focus_during_idle
        self.assertFalse(self.guard.begin(allow_focus=False))
        self.assertFalse(self.desktop.history)
        self.assertFalse(self.sent)

    def test_actual_movement_can_still_activate_after_deferred_turn(self):
        self.assertFalse(self.guard.begin(allow_focus=False))
        self.assertTrue(self.guard.begin())
        self.guard.down(39, .12)
        self.guard.finish()
        self.assertEqual(self.desktop.history, [("focus", 10), ("down", 39), ("up", 39)])

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

    def test_mouse_displacement_does_not_interrupt_movement(self):
        self.guard.begin()
        self.guard.down(39)
        self.desktop.position = (51, 50)
        self.guard.poll()
        self.assertIn(39, self.guard.held)
        self.assertTrue(self.guard.active)
        self.assertIsNone(self.guard.error)
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

    def test_delayed_async_keyup_does_not_mistake_own_turn_for_user(self):
        self.guard.begin()
        self.guard.down(37)
        self.guard.up(37)
        self.desktop.pressed.add(37)  # 模拟 keyup 已发送，异步键状态稍晚更新。
        self.guard.heartbeat()
        self.assertTrue(self.guard.active)
        self.assertIsNone(self.guard.error)
        self.desktop.pressed.clear()
        self.now += 0.1
        self.guard.heartbeat()
        self.assertFalse(self.guard.releasing)

    def test_keyup_grace_is_bounded_and_does_not_mask_other_keys(self):
        for vk, elapsed in ((37, 0.1), (65, 0.01)):
            with self.subTest(vk=vk):
                self.setUp()
                self.guard.begin()
                self.guard.down(37)
                self.guard.up(37)
                self.desktop.pressed.add(vk)
                self.now += elapsed
                self.guard.poll()
                self.assertFalse(self.guard.active)
                self.assertIn("键盘/鼠标按键", self.guard.error)

    def test_keyup_grace_does_not_mask_click_or_focus_change(self):
        for kind in ("click", "focus"):
            with self.subTest(kind=kind):
                self.setUp()
                self.guard.begin()
                self.guard.down(37)
                self.guard.up(37)
                if kind == "click":
                    self.desktop.pressed.add(1)
                else:
                    self.desktop.current = 30
                self.guard.poll()
                self.assertFalse(self.guard.active)
                self.assertIn("键盘/鼠标按键" if kind == "click" else "前台焦点变化", self.guard.error)

    def test_cursor_displacement_during_focus_acquisition_does_not_abort(self):
        focus = self.desktop.focus
        def move_cursor(*args):
            self.desktop.position = (500, 400)
            focus(*args)
        self.desktop.focus = move_cursor
        self.assertTrue(self.guard.begin())
        self.guard.down(39)
        self.guard.check()

    def test_idle_ignores_mouse_activity_timestamp_but_waits_after_buttons(self):
        desktop = Desktop()
        with patch.object(desktop, "keys_down", return_value=False) as pressed, \
             patch("mbv.hybrid_movement.time.monotonic", return_value=10.) as clock, \
             patch("mbv.hybrid_movement.user32.GetLastInputInfo") as last_input:
            self.assertFalse(desktop.idle())
            clock.return_value = 10.71
            self.assertTrue(desktop.idle())
            last_input.assert_not_called()
            pressed.return_value = True
            self.assertFalse(desktop.idle())
            pressed.return_value = False
            clock.return_value = 11.
            self.assertFalse(desktop.idle())
            clock.return_value = 11.71
            self.assertTrue(desktop.idle())

    def test_keyup_grace_applies_to_injected_modifier_alias(self):
        self.guard.begin()
        self.guard.down(0x11)
        self.guard.up(0x11)
        self.desktop.pressed.add(0xA2)
        self.guard.heartbeat()
        self.assertTrue(self.guard.active)

    def test_game_foreground_still_requires_idle_before_new_lease(self):
        self.desktop.current = 10
        self.desktop.is_idle = False
        self.assertFalse(self.guard.begin())
        self.assertFalse(self.sent)
        self.assertFalse(self.desktop.history)


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

    def test_foreground_skills_use_hardware_even_with_active_movement(self):
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
        post.assert_not_called()
        self.assertEqual(self.keyboard._send_input.call_count, 13)
        self.assertIn(vk_for("left"), self.keyboard.hybrid.held)
        self.assertFalse(self.keyboard.hybrid.skill_held)
        self.keyboard.release_all()

    def test_message_failure_never_falls_back_to_sendinput(self):
        with patch("mbv.input.user32.IsWindow", return_value=True), \
             patch("mbv.input.user32.PostMessageW", return_value=False):
            with self.assertRaises(OSError):
                self.keyboard.tap("d")
        self.keyboard._send_input.assert_not_called()

    def test_jump_movement_and_foreground_skill_on_same_key_release_separately(self):
        self.keyboard.prepare_movement()
        with patch("mbv.input.time.sleep"), patch("mbv.input.user32.IsWindow", return_value=True), \
             patch("mbv.input.user32.PostMessageW", return_value=True) as post:
            self.keyboard.movement_tap("alt")
            self.keyboard.tap("alt")
        self.assertEqual(self.keyboard._send_input.call_args_list[0].args, (vk_for("alt"), False))
        self.assertEqual(self.keyboard._send_input.call_args_list[1].args, (vk_for("alt"), True))
        self.assertEqual(self.keyboard._send_input.call_count, 4)
        post.assert_not_called()

    def test_background_skills_stay_messages_without_focusing(self):
        with patch("mbv.input.user32.IsWindow", return_value=True), \
             patch("mbv.input.user32.PostMessageW", return_value=True) as post, \
             patch("mbv.input.time.sleep"):
            for key in ("d", "f", "ctrl", "home", "z"):
                self.keyboard.tap(key)
        self.assertEqual(post.call_count, 10)
        self.keyboard._send_input.assert_not_called()
        self.assertFalse(self.desktop.history)

    def test_focus_lost_during_tap_releases_original_hardware_channel(self):
        self.desktop.current = 10
        with patch("mbv.input.time.sleep", side_effect=lambda _: setattr(self.desktop, "current", 20)), \
             patch("mbv.input.user32.PostMessageW") as post:
            self.keyboard.tap("d")
        self.assertEqual([c.args for c in self.keyboard._send_input.call_args_list], [(68, False), (68, True)])
        post.assert_not_called()
        self.assertFalse(self.keyboard._hybrid_presses)
        self.assertFalse(self.desktop.history)

    def test_focus_gained_during_tap_keeps_original_message_channel(self):
        with patch("mbv.input.time.sleep", side_effect=lambda _: setattr(self.desktop, "current", 10)), \
             patch("mbv.input.user32.IsWindow", return_value=True), \
             patch("mbv.input.user32.PostMessageW", return_value=True) as post:
            self.keyboard.tap("d")
        self.assertEqual(post.call_count, 2)
        self.keyboard._send_input.assert_not_called()

    def test_next_press_reselects_channel_after_focus_transition(self):
        with patch("mbv.input.time.sleep"), patch("mbv.input.user32.IsWindow", return_value=True), \
             patch("mbv.input.user32.PostMessageW", return_value=True) as post:
            for foreground in (10, 20, 10):
                self.desktop.current = foreground
                self.keyboard.tap("d")
        self.assertEqual(self.keyboard._send_input.call_count, 4)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(self.desktop.history, [])

    def test_foreground_held_skill_does_not_repeat_global_keydown_after_focus_loss(self):
        self.desktop.current = 10
        self.keyboard.down("d")
        self.desktop.current = 20
        self.keyboard._poll_hybrid_presses()
        if 68 in self.keyboard.held:
            self.keyboard._repeat_key(68, 10.0)
        self.keyboard.up("d")
        self.assertEqual([c.args for c in self.keyboard._send_input.call_args_list], [(68, False), (68, True)])
        self.assertFalse(self.keyboard.held)

    def test_foreground_skills_are_not_reported_as_user_input_during_pickup(self):
        self.keyboard.prepare_movement()
        self.keyboard.movement_down("left")
        def during_hold(_):
            self.desktop.pressed.add(vk_for("z"))
            self.keyboard.hybrid.heartbeat()
        with patch("mbv.input.time.sleep", side_effect=during_hold):
            self.keyboard.tap("z")
        self.assertTrue(self.keyboard.hybrid.active)
        self.assertIsNone(self.keyboard.hybrid.error)
        self.keyboard.hybrid.heartbeat()  # 包含抬键后的同步宽限。
        self.keyboard.release_all()

    def test_pause_releases_tap_and_late_cleanup_cannot_release_new_press(self):
        self.desktop.current = 10
        def during_hold(_):
            self.keyboard.release_all()
            self.keyboard.down("d")
        with patch("mbv.input.time.sleep", side_effect=during_hold):
            self.keyboard.tap("d")
        self.assertEqual([c.args for c in self.keyboard._send_input.call_args_list],
                         [(68, False), (68, True), (68, False)])
        self.assertIn(68, self.keyboard.hybrid.skill_held)
        self.keyboard.release_all()

    def test_hardware_failure_does_not_fall_back_to_messages(self):
        self.desktop.current = 10
        def fail_down(vk, up):
            if not up:
                raise OSError("send failed")
        self.keyboard._send_input.side_effect = fail_down
        with patch("mbv.input.user32.PostMessageW") as post, self.assertRaises(OSError):
            self.keyboard.tap("d")
        post.assert_not_called()
        self.assertFalse(self.keyboard.hybrid.skill_held)
        with self.assertRaises(OSError):
            self.keyboard.check_health()

    def test_unexpected_partial_keydown_failure_still_releases_hardware(self):
        self.desktop.current = 10
        self.keyboard._send_input.side_effect = [RuntimeError("partial"), None]
        with self.assertRaisesRegex(RuntimeError, "partial"):
            self.keyboard.tap("d")
        self.assertEqual([c.args for c in self.keyboard._send_input.call_args_list], [(68, False), (68, True)])
        self.assertFalse(self.keyboard.hybrid.skill_held)

    def test_keyup_failure_is_retained_until_released_before_rebind(self):
        self.desktop.current = 10
        self.keyboard.down("d")
        self.keyboard._send_input.side_effect = OSError("keyup failed")
        self.keyboard.release_all()
        self.assertIn(68, self.keyboard._hybrid_presses)
        with self.assertRaises(OSError):
            self.keyboard.bind_window(10)
        self.keyboard._send_input.side_effect = None
        with patch("mbv.input.resolve_input_hwnd", return_value=10):
            self.keyboard.bind_window(10)
        self.keyboard.check_health()
        self.assertFalse(self.keyboard.hybrid.skill_held)

    def test_second_focus_check_prevents_hardware_down_after_switch(self):
        self.desktop.foreground = MagicMock(side_effect=[10, 20])
        with self.assertRaises(OSError):
            self.keyboard.tap("d")
        self.keyboard._send_input.assert_not_called()

    def test_background_keyup_does_not_target_reused_root(self):
        with patch("mbv.input.user32.IsWindow", return_value=True), \
             patch("mbv.input.user32.PostMessageW", return_value=True) as post, \
             patch("mbv.input.time.sleep", side_effect=lambda _: self.desktop.identities.update({10: 999})):
            self.keyboard.tap("d")
        self.assertEqual(post.call_count, 1)

    def test_child_input_window_does_not_determine_foreground_channel(self):
        self.keyboard.hwnd = 11
        self.desktop.identities[11] = 100
        self.desktop.current = 10
        with patch("mbv.input.time.sleep"), patch("mbv.input.user32.PostMessageW") as post:
            self.keyboard.tap("d")
        self.assertEqual(self.keyboard._send_input.call_count, 2)
        post.assert_not_called()

    def test_reused_child_is_not_sent_keyup_even_while_root_is_valid(self):
        self.keyboard.hwnd = 11
        self.desktop.identities[11] = 100
        with patch("mbv.input.user32.IsWindow", return_value=True), \
             patch("mbv.input.user32.PostMessageW", return_value=True) as post, \
             patch("mbv.input.time.sleep", side_effect=lambda _: self.desktop.identities.update({11: 999})):
            self.keyboard.tap("d")
        self.assertEqual(post.call_count, 1)

    def test_message_keyup_still_reaches_same_process_after_minimize(self):
        def minimize(_):
            self.desktop.valid = MagicMock(return_value=False)
        with patch("mbv.input.user32.IsWindow", return_value=True), \
             patch("mbv.input.user32.PostMessageW", return_value=True) as post, \
             patch("mbv.input.time.sleep", side_effect=minimize):
            self.keyboard.tap("d")
        self.assertEqual(post.call_count, 2)

    def test_skill_watchdog_releases_even_without_visual_frames_or_move_lease(self):
        self.desktop.current = 10
        self.keyboard.down("d")
        self.desktop.current = 20
        stop = MagicMock()
        stop.wait.side_effect = [False, True]
        stop.is_set.return_value = False
        self.keyboard._repeat_held_keys(stop)
        self.assertFalse(self.keyboard.hybrid.skill_held)
        self.assertFalse(self.keyboard.held)
        self.assertEqual(self.keyboard._send_input.call_count, 2)

    def test_message_pause_releases_exactly_once_without_hardware_fallback(self):
        with patch("mbv.input.user32.IsWindow", return_value=True), \
             patch("mbv.input.user32.PostMessageW", return_value=True) as post, \
             patch("mbv.input.time.sleep", side_effect=lambda _: self.keyboard.release_all()):
            self.keyboard.tap("ctrl", 0.18)
        self.assertEqual(post.call_count, 2)
        self.keyboard._send_input.assert_not_called()
        self.assertFalse(self.keyboard._hybrid_presses)

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
        bot.keyboard.hybrid.hwnd = 10
        bot.keyboard.hybrid.desktop.foreground.return_value = 10
        bot.keyboard.prepare_movement.return_value = True
        bot.keyboard.movement_events.return_value = []
        bot.log = MagicMock()
        bot._try_auto_potion = MagicMock(return_value=False)
        bot._try_auto_buff = MagicMock(return_value=False)

    def act(self, marker=(0.7, 0.5), now=10.0,
            player_box=(90, 100, 20, 1), target_box=(200, 90, 20, 20), foreground=10):
        with patch("mbv.bot.user32.IsWindow", return_value=True), \
             patch("mbv.bot.user32.IsIconic", return_value=False), \
             patch("mbv.bot.user32.GetForegroundWindow", return_value=foreground), \
             patch("mbv.bot.time.monotonic", return_value=now):
            self.bot.act(WindowInfo(10, "NewMaple", 0, 0, 800, 600), 1.0, 1.0,
                         marker, player_box, target_box, None, 400, True, now, 200)

    def test_target_gap_does_not_restart_turn_or_delay_same_side_attack(self):
        self.act(marker=(0.5, 0.5))
        self.act(marker=(0.5, 0.5), now=10.1)
        self.bot.keyboard.reset_mock()
        self.act(marker=(0.5, 0.5), now=10.2, target_box=None)
        self.bot.keyboard.tap.assert_not_called()
        self.act(marker=(0.5, 0.5), now=10.4)
        self.bot.keyboard.tap.assert_called_once_with(self.bot.config["keys"]["attack"])
        self.bot.keyboard.prepare_movement.assert_not_called()

    def test_short_visual_loss_stops_input_but_resumes_without_repeated_turn(self):
        self.act(marker=(0.5, 0.5))
        self.act(marker=(0.5, 0.5), now=10.1)
        self.bot.keyboard.reset_mock()
        self.act(marker=(0.5, 0.5), now=10.2, player_box=None)
        self.assertEqual(self.bot.state, "PLAYER_SCREEN_LOST")
        self.bot.keyboard.tap.assert_not_called()
        self.act(marker=(0.5, 0.5), now=10.5)
        self.bot.keyboard.tap.assert_called_once_with(self.bot.config["keys"]["attack"])

    def test_long_visual_loss_requires_turn_on_first_recovered_frame(self):
        self.act(marker=(0.5, 0.5))
        self.act(marker=(0.5, 0.5), now=10.1)
        self.bot.keyboard.reset_mock()
        self.act(marker=(0.5, 0.5), now=10.2, player_box=None)
        self.bot.last_nameplate_seen_at = 10.9
        self.act(marker=(0.5, 0.5), now=10.9)
        self.bot.keyboard.movement_tap.assert_called_with("right", 0.025)
        self.bot.keyboard.tap.assert_not_called()
        self.assertEqual(self.bot.state, "FACE_TARGET_RIGHT")

    def test_ambiguous_marker_immediately_invalidates_facing_and_blocks_attack(self):
        self.act(marker=(0.5, 0.5))
        self.bot.keyboard.reset_mock()
        self.bot.live_marker_unambiguous = False
        self.act(marker=(0.5, 0.5), now=10.1)
        self.assertIsNone(self.bot.attack_turn_direction)
        self.bot.keyboard.tap.assert_not_called()

    def test_frequent_target_gaps_keep_attack_cadence_without_periodic_turn(self):
        attacks = []
        for tick in range(100):
            now = 10.0 + tick / 10
            self.bot.last_nameplate_seen_at = now
            self.act(marker=(0.5, 0.5), now=now,
                     target_box=None if tick % 5 == 2 else (200, 90, 20, 20))
            if self.bot.last_attack == now:
                attacks.append(now)
        turns = [c for c in self.bot.keyboard.movement_tap.call_args_list if c.args[0] == "right"]
        self.assertEqual(len(turns), 1)  # 仅首次转向，不再每五秒点方向。
        self.assertGreaterEqual(len(attacks), 24)
        self.bot.keyboard.movement_down.assert_not_called()

    def test_return_uses_foreground_movement_then_separate_turn_before_attack(self):
        self.act()
        self.act(now=10.1)
        self.bot.keyboard.movement_down.assert_called_with("left")
        self.bot.keyboard.down.assert_not_called()
        self.act(marker=(0.5, 0.5), now=10.2)
        self.bot.keyboard.finish_movement.assert_called()
        self.assertEqual(self.bot.state, "FACE_TARGET_RIGHT")
        self.bot.keyboard.movement_tap.assert_called_once_with("right", 0.025)
        self.bot.keyboard.tap.assert_not_called()
        self.act(marker=(0.5, 0.5), now=10.3)
        self.bot.keyboard.tap.assert_any_call(self.bot.config["keys"]["attack"])

    def test_busy_user_does_not_start_movement_or_consume_step(self):
        self.bot.keyboard.prepare_movement.return_value = False
        self.act()
        self.assertEqual(self.bot.state, "HYBRID_WAIT_IDLE_FOREGROUND")
        self.bot.keyboard.movement_down.assert_not_called()
        self.assertIsNone(self.bot.move_progress)

    def test_buff_or_potion_releases_lease_but_preserves_stationary_facing(self):
        for method in ("_try_auto_buff", "_try_auto_potion"):
            self.bot.attack_turn_direction = "left"
            self.bot.attack_turn_requested_at = 9.0
            getattr(self.bot, method).return_value = True
            self.act()
            self.bot.keyboard.finish_movement.assert_called()
            self.bot.keyboard.movement_down.assert_not_called()
            self.assertEqual(self.bot.attack_turn_direction, "left")
            getattr(self.bot, method).return_value = False

    def test_visual_or_marker_loss_releases_focus(self):
        self.act(marker=None)
        self.bot.keyboard.finish_movement.assert_called()
        self.bot.keyboard.movement_down.assert_not_called()

    def test_focus_change_during_potion_gate_still_invalidates_turn(self):
        self.bot._hybrid_attack_foreground = False
        self.bot.attack_turn_direction = "left"
        self.bot._try_auto_potion.return_value = True
        self.act(marker=(0.5, 0.5), foreground=10)
        self.assertIsNone(self.bot.attack_turn_direction)
        self.bot.keyboard.movement_tap.assert_not_called()

    def test_untrusted_marker_during_buff_gate_still_invalidates_turn(self):
        self.bot.attack_turn_direction = "left"
        self.bot.live_marker_unambiguous = False
        self.bot._try_auto_buff.return_value = True
        self.act(marker=(0.5, 0.5))
        self.assertIsNone(self.bot.attack_turn_direction)
        self.bot.keyboard.movement_tap.assert_not_called()

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


class HybridFacingIntegrationTests(unittest.TestCase):
    """真实 Keyboard + HybridMovement 状态机，只有桌面和发键 API 使用替身。"""
    def setUp(self):
        self.runtime = HybridRuntimeTests()
        self.runtime.setUp()
        self.bot = self.runtime.bot
        self.bot.notify = MagicMock()
        self.now = 10.0
        self.desktop = FakeDesktop()
        self.desktop.current = 10  # 本组原有用例验证已经前台时的转向保护。
        keyboard = Keyboard("hybrid")
        keyboard.hwnd = keyboard.root_hwnd = 10
        keyboard._send_input = MagicMock()
        keyboard._ensure_repeat_thread = MagicMock()
        keyboard.hybrid = HybridMovement(keyboard._send_input, desktop=self.desktop,
                                         clock=lambda: self.now, start_watchdog=False)
        keyboard.hybrid.bind(10)
        self.bot.keyboard = keyboard
        self.keyboard = keyboard
        self.post = self.enterContext(patch("mbv.input.user32.PostMessageW", return_value=True))
        self.enterContext(patch("mbv.input.time.sleep"))

    def act(self, now, **kwargs):
        self.now = now
        self.bot.last_nameplate_seen_at = now
        self.runtime.act(now=now, marker=(0.5, 0.5), foreground=self.desktop.current, **kwargs)

    def test_focus_new_frame_hardware_turn_release_then_foreground_attack(self):
        self.act(10.0)
        self.assertEqual(self.bot.state, "HYBRID_WAIT_FRAME")
        self.assertTrue(self.keyboard.hybrid.active)
        self.keyboard._send_input.assert_not_called()
        self.post.assert_not_called()
        self.act(10.1)
        self.assertEqual([c.args for c in self.keyboard._send_input.call_args_list],
                         [(vk_for("right"), False), (vk_for("right"), True)])
        self.assertFalse(self.keyboard.hybrid.active)
        self.assertFalse(self.keyboard.hybrid.held)
        self.post.assert_not_called()
        self.act(10.15)
        self.post.assert_not_called()  # 转向后的等待没有提前跳过。
        self.act(10.2)
        self.post.assert_not_called()
        self.assertEqual([c.args for c in self.keyboard._send_input.call_args_list][-2:],
                         [(vk_for(self.bot.config["keys"]["attack"]), False),
                          (vk_for(self.bot.config["keys"]["attack"]), True)])
        self.assertEqual(self.desktop.current, 10)

    def test_busy_desktop_does_not_send_direction_or_attack(self):
        self.desktop.is_idle = False
        self.act(10.0)
        self.assertEqual(self.bot.state, "HYBRID_WAIT_IDLE_FOREGROUND")
        self.keyboard._send_input.assert_not_called()
        self.post.assert_not_called()
        self.assertEqual(self.desktop.current, 10)

    def test_busy_foreground_has_distinct_wait_without_disarming(self):
        self.desktop.current = 10
        self.desktop.is_idle = False
        self.act(10.0)
        self.assertEqual(self.bot.state, "HYBRID_WAIT_IDLE_FOREGROUND")
        self.assertTrue(self.bot.armed)
        self.assertFalse(self.desktop.history)
        self.keyboard._send_input.assert_not_called()
        self.post.assert_not_called()

    def test_target_changes_after_focus_uses_fresh_direction(self):
        self.act(10.0)
        self.act(10.1, target_box=(20, 90, 20, 20))
        self.assertEqual(self.keyboard._send_input.call_args_list[0].args,
                         (vk_for("left"), False))
        self.post.assert_not_called()

    def test_manual_foreground_transition_invalidates_cached_facing(self):
        self.bot._hybrid_attack_foreground = False
        self.bot.direction = self.bot.attack_turn_direction = "right"
        self.bot.attack_turn_requested_at = 9.0
        self.bot.attack_facing_ready_at = 9.1
        self.desktop.current = 10
        self.act(10.0)
        self.post.assert_not_called()
        self.assertEqual(self.bot.state, "HYBRID_WAIT_FRAME")
        self.act(10.1)
        self.keyboard._send_input.assert_any_call(vk_for("right"), False)

    def test_target_lost_after_focus_cancels_turn_lease(self):
        self.act(10.0)
        self.act(10.1, target_box=None)
        self.assertFalse(self.keyboard.hybrid.active)
        self.keyboard._send_input.assert_not_called()
        self.post.assert_not_called()

    def test_user_switch_before_turn_key_sends_background_skill_without_refocusing(self):
        self.act(10.0)
        self.desktop.current = 30
        self.act(10.1)
        self.assertTrue(self.bot.armed)
        self.assertFalse(self.keyboard.hybrid.active)
        self.keyboard._send_input.assert_not_called()
        self.assertEqual(self.post.call_count, 2)
        self.assertFalse(self.desktop.history)
        self.assertEqual(self.bot.state, "HYBRID_ATTACK_FIXED")

    def test_dynamic_attack_does_not_hold_hardware_direction_on_every_skill(self):
        self.bot.config["strategy"]["active"] = "bowman_dynamic"
        self.bot.strategy = get_strategy("bowman_dynamic")
        for now in (10.0, 10.1, 10.2, 10.5, 10.8):
            self.act(now)
        self.assertEqual(self.keyboard._send_input.call_count, 8)
        self.assertEqual(sum(c.args[0] in {37, 39} for c in self.keyboard._send_input.call_args_list), 2)
        self.post.assert_not_called()

    def test_face_and_jump_attack_share_guard_without_sending_direction_messages(self):
        from mbv.strategies.base import StrategyDecision
        for action in ("face", "jump_attack"):
            with self.subTest(action=action):
                self.bot._reset_attack_facing()
                self.bot.last_attack = 0.0
                self.keyboard._send_input.reset_mock()
                self.post.reset_mock()
                decision = StrategyDecision(action, "TEST", direction="right",
                                            target_x=0.8, player_x=0.5)
                with patch.object(self.bot.strategy, "decide", return_value=decision):
                    for now in (20.0, 20.1, 20.2):
                        self.act(now)
                self.assertEqual(self.keyboard._send_input.call_count, 2 if action == "face" else 6)
                self.assertTrue(all(c.args[2] not in {vk_for("left"), vk_for("right")}
                                    for c in self.post.call_args_list))
                self.post.assert_not_called()

    def test_turn_failure_does_not_cache_success_or_send_skill(self):
        self.act(10.0)
        def fail_down(vk, key_up):
            if not key_up:
                raise OSError("测试转向失败")
        self.keyboard._send_input.side_effect = fail_down
        self.act(10.1)
        self.assertFalse(self.bot.armed)
        self.assertIsNone(self.bot.attack_turn_direction)
        self.assertFalse(self.keyboard.hybrid.held)
        self.post.assert_not_called()

    def test_real_leases_do_not_reactivate_on_every_attack_or_target_gap(self):
        attacks = []
        for tick in range(100):
            now = 10.0 + tick / 10
            self.act(now, target_box=None if tick % 5 == 2 else (200, 90, 20, 20))
            if self.bot.last_attack == now:
                attacks.append(now)
        self.assertGreaterEqual(len(attacks), 23)
        self.assertEqual(sum(c.args[0] in {37, 39} for c in self.keyboard._send_input.call_args_list), 2)
        self.assertEqual(self.keyboard._send_input.call_count, 2 + 2 * len(attacks))
        self.assertEqual(self.desktop.history, [])

    def test_background_target_flips_send_only_skills_without_focus_or_turn_cache(self):
        self.desktop.current = 20
        self.desktop.is_idle = False
        attacks = 0
        for tick in range(100):
            now = 10. + tick / 10
            self.act(now, target_box=(200, 90, 20, 20) if tick % 2 else (20, 90, 20, 20))
            attacks += self.bot.last_attack == now
        self.assertGreaterEqual(attacks, 30)
        self.assertEqual(self.desktop.current, 20)
        self.assertFalse(self.desktop.history)
        self.keyboard._send_input.assert_not_called()
        self.assertEqual(self.post.call_count, 2 * attacks)
        self.assertTrue(all(c.args[2] == vk_for("shift") for c in self.post.call_args_list))
        self.assertIsNone(self.bot.direction)
        self.assertIsNone(getattr(self.bot, "attack_turn_direction", None))
        self.assertEqual(self.bot.state, "HYBRID_ATTACK_FIXED")
        attack_logs = [c.kwargs for c in self.bot.log.write.call_args_list if c.args[0] == "attack"]
        self.assertTrue(all(c["direction"] is None and c["turn_deferred"] for c in attack_logs))

    def test_background_focus_change_and_visual_recovery_do_not_force_turn(self):
        self.act(10.)
        self.act(10.1)
        self.act(10.2)
        self.desktop.current = 20
        self.keyboard._send_input.reset_mock()
        self.act(10.5)
        self.act(10.6, player_box=None)
        self.act(11.3)
        self.assertEqual(self.bot.state, "HYBRID_ATTACK_FIXED")
        self.assertEqual(self.desktop.current, 20)
        self.assertFalse(self.desktop.history)
        self.keyboard._send_input.assert_not_called()
        self.assertGreaterEqual(self.post.call_count, 4)

    def test_background_face_and_jump_attack_do_not_activate_or_send_directions(self):
        from mbv.strategies.base import StrategyDecision
        self.desktop.current = 20
        for action in ("face", "jump_attack"):
            with self.subTest(action=action):
                self.post.reset_mock()
                decision = StrategyDecision(action, "TEST", direction="right", target_x=.8, player_x=.5)
                with patch.object(self.bot.strategy, "decide", return_value=decision):
                    self.act(10.)
                self.assertFalse(self.desktop.history)
                self.keyboard._send_input.assert_not_called()
                self.assertTrue(all(c.args[2] not in {37, 39} for c in self.post.call_args_list))
                if action == "face":
                    self.post.assert_not_called()
                    self.assertEqual(self.bot.state, "HYBRID_TURN_DEFERRED")
                else:
                    self.assertEqual(self.post.call_count, 4)
                    self.assertEqual(self.bot.state, "HYBRID_JUMP_ATTACK_FIXED")

    def test_actual_safe_return_still_acquires_foreground_from_background(self):
        self.desktop.current = 20
        self.runtime.act(marker=(.7, .5), now=10., foreground=20)
        self.assertEqual(self.desktop.history, [("focus", 10)])
        self.assertEqual(self.bot.state, "HYBRID_WAIT_FRAME")
        self.keyboard._send_input.assert_not_called()
        self.now = 10.1
        self.runtime.act(marker=(.7, .5), now=10.1, foreground=10)
        self.now = 10.8
        self.bot.last_nameplate_seen_at = 10.8
        self.runtime.act(marker=(.7, .5), now=10.8, foreground=10)
        self.keyboard._send_input.assert_any_call(vk_for("left"), False)

    def test_foreground_lost_between_turn_check_and_lease_does_not_focus(self):
        def lose_focus_during_idle():
            self.desktop.current = 20
            return True
        self.desktop.idle = lose_focus_during_idle
        self.act(10.)
        self.assertEqual(self.bot.state, "HYBRID_ATTACK_FIXED")
        self.assertFalse(self.desktop.history)
        self.keyboard._send_input.assert_not_called()
        self.assertEqual(self.post.call_count, 2)

    def test_background_full_player_loss_waits_instead_of_focusing_to_search(self):
        self.desktop.current = 20
        self.now = 12.
        self.runtime.act(marker=(.5, .5), now=12., player_box=None, foreground=20)
        self.assertEqual(self.bot.state, "HYBRID_WAIT_LOCALIZATION")
        self.assertTrue(self.bot.armed)
        self.assertFalse(self.desktop.history)
        self.keyboard._send_input.assert_not_called()
        self.post.assert_not_called()

    def test_destination_pickup_holds_across_frames_then_releases_once(self):
        from mbv.strategies.base import StrategyDecision
        key = self.bot.config["keys"]["pickup"]
        pickup = StrategyDecision("pickup", "PICKUP_COLLECT", pickup_interval_seconds=.15)
        for foreground in (10, 20):
            with self.subTest(foreground=foreground):
                self.desktop.current = foreground
                self.keyboard._send_input.reset_mock()
                self.post.reset_mock()
                with patch.object(self.bot.strategy, "decide", return_value=pickup):
                    for now in (10., 10.2, 10.4):
                        self.act(now)
                self.assertIn(vk_for(key), self.keyboard.held)
                channel = self.keyboard._send_input if foreground == 10 else self.post
                self.assertEqual(channel.call_count, 1)  # 连续帧不松键、不重复点按。
                self.assertFalse(self.desktop.history)
                with patch.object(self.bot.strategy, "decide", return_value=StrategyDecision("stop", "WAIT")):
                    self.act(10.5)
                self.assertEqual(channel.call_count, 2)
                self.assertNotIn(vk_for(key), self.keyboard.held)

    def test_held_pickup_expires_without_new_frames_and_can_resume(self):
        from mbv.strategies.base import StrategyDecision
        pickup = StrategyDecision("pickup", "PICKUP_COLLECT", pickup_interval_seconds=.15)
        key = self.bot.config["keys"]["pickup"]
        with patch.object(self.bot.strategy, "decide", return_value=pickup):
            self.act(10.)
            self.act(10.5)
            with self.keyboard._lock:
                self.keyboard._repeat_key(vk_for(key), 11.)
                self.assertIn(vk_for(key), self.keyboard.held)
                self.keyboard._repeat_key(vk_for(key), 11.31)
                self.assertNotIn(vk_for(key), self.keyboard.held)
            self.assertEqual(self.keyboard._send_input.call_count, 3)
            self.act(11.4)
            self.assertEqual(self.keyboard._send_input.call_count, 4)

    def test_pickup_focus_loss_releases_hardware_before_resuming_background_hold(self):
        from mbv.strategies.base import StrategyDecision
        pickup = StrategyDecision("pickup", "PICKUP_COLLECT", pickup_interval_seconds=.15)
        key = self.bot.config["keys"]["pickup"]
        with patch.object(self.bot.strategy, "decide", return_value=pickup):
            self.act(10.)
            self.desktop.current = 20
            self.act(10.2)
            self.assertEqual(self.keyboard._send_input.call_count, 2)
            self.assertEqual(self.post.call_count, 1)
            self.assertEqual(self.keyboard._hybrid_presses[vk_for(key)].channel, "message")
            self.assertFalse(self.desktop.history)

    def test_pickup_repeat_respects_original_hybrid_channel_without_keyup(self):
        from mbv.strategies.base import StrategyDecision
        pickup = StrategyDecision("pickup", "PICKUP_COLLECT", pickup_interval_seconds=.15)
        key = self.bot.config["keys"]["pickup"]
        for foreground in (10, 20):
            with self.subTest(foreground=foreground):
                self.desktop.current = foreground
                self.keyboard._send_input.reset_mock()
                self.post.reset_mock()
                with patch.object(self.bot.strategy, "decide", return_value=pickup):
                    self.act(10.)
                with patch("mbv.input.user32.IsWindow", return_value=True), self.keyboard._lock:
                    for now in (10.11, 10.22, 10.33):
                        self.keyboard._repeat_key(vk_for(key), now)
                channel = self.keyboard._send_input if foreground == 10 else self.post
                self.assertEqual(channel.call_count, 4)
                self.assertFalse(self.desktop.history)
                if foreground == 10:
                    self.assertTrue(all(c.args[1] is False for c in channel.call_args_list))
                    self.post.assert_not_called()
                else:
                    self.assertTrue(all(c.args[1] == 0x100 for c in channel.call_args_list))
                    self.keyboard._send_input.assert_not_called()
                with patch.object(self.bot.strategy, "decide", return_value=StrategyDecision("stop", "WAIT")):
                    self.act(10.5)
                self.assertEqual(channel.call_count, 5)

    def test_pickup_repeat_failure_releases_before_reporting_input_error(self):
        from mbv.strategies.base import StrategyDecision
        with patch.object(self.bot.strategy, "decide", return_value=StrategyDecision(
                "pickup", "PICKUP_COLLECT", pickup_interval_seconds=.15)):
            self.act(10.)
        def fail_repeat(vk, up):
            if not up:
                raise OSError("repeat failed")
        self.keyboard._send_input.side_effect = fail_repeat
        key = self.bot.config["keys"]["pickup"]
        with self.keyboard._lock, self.assertRaisesRegex(OSError, "repeat failed"):
            self.keyboard._repeat_key(vk_for(key), 10.2)
        self.assertNotIn(vk_for(key), self.keyboard.held)
        self.assertNotIn(vk_for(key), self.keyboard.hybrid.skill_held)
        self.assertFalse(self.keyboard.hybrid.held)


if __name__ == "__main__":
    unittest.main()
