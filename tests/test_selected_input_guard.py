from __future__ import annotations

import threading
import unittest
from unittest.mock import MagicMock, patch

from mbv.input import Keyboard, vk_for
from mbv.win32 import KEYEVENTF_KEYUP, WM_KEYDOWN, WM_KEYUP


class SelectedInputGuardTests(unittest.TestCase):
    """仅操作假的 Win32 表；不会向桌面或任何真实窗口发送输入。"""

    def setUp(self) -> None:
        self.pids = {10: 101, 11: 101, 20: 202, 21: 202}
        self.native = MagicMock()
        self.native.IsWindow.side_effect = lambda hwnd: hwnd in self.pids
        self.native.IsIconic.return_value = False
        self.native.GetWindowThreadProcessId.side_effect = self._pid
        self.native.MapVirtualKeyW.return_value = 0x1E
        self.native.SendInput.return_value = 1
        self.native.PostMessageW.return_value = 1
        self.native.SendMessageTimeoutW.return_value = 1
        self.native.GetForegroundWindow.return_value = 10
        self.native.GetAncestor.return_value = 10
        self.native.IsChild.return_value = False
        for target in ("mbv.input.user32", "mbv.hybrid_movement.user32"):
            patcher = patch(target, self.native)
            patcher.start()
            self.addCleanup(patcher.stop)
        resolver = patch("mbv.input.resolve_input_hwnd", side_effect=lambda root: {10: 11, 20: 21}[root])
        self.resolve = resolver.start()
        self.addCleanup(resolver.stop)
        self.keyboards: list[Keyboard] = []
        self.addCleanup(self._release)

    def _pid(self, hwnd, pointer):
        pointer._obj.value = self.pids.get(hwnd, 0)
        return 1

    def _release(self):
        for keyboard in self.keyboards:
            keyboard.release_all()

    def keyboard(self, delivery="foreground", *, real_repeat=False):
        keyboard = Keyboard(delivery)
        if keyboard.hybrid is not None:
            keyboard.hybrid.start_watchdog = False
        self.keyboards.append(keyboard)
        if not real_repeat:
            keyboard._ensure_repeat_thread = MagicMock()
        keyboard.bind_window(10, expected_pid=101)
        return keyboard

    def hardware_events(self):
        return [bool(call.args[1]._obj.ki.dwFlags & KEYEVENTF_KEYUP)
                for call in self.native.SendInput.call_args_list]

    def test_binding_confirms_root_and_input_child_pid(self):
        keyboard = self.keyboard()
        self.assertEqual((keyboard.root_hwnd, keyboard.hwnd), (10, 11))
        self.assertEqual(keyboard._bound_identity, (10, 11, 101))
        self.native.SendInput.assert_not_called()
        self.native.PostMessageW.assert_not_called()

    def test_binding_rejects_wrong_root_or_child_pid(self):
        for hwnd in (10, 11):
            with self.subTest(hwnd=hwnd):
                self.pids[hwnd] = 999
                keyboard = Keyboard("foreground")
                with self.assertRaisesRegex(OSError, "身份变化"):
                    keyboard.bind_window(10, expected_pid=101)
                self.assertEqual(keyboard.root_hwnd, 0)
                self.pids[hwnd] = 101
        self.native.SendInput.assert_not_called()
        self.native.PostMessageW.assert_not_called()

    def test_root_identity_rechecked_after_child_resolution(self):
        def change_root(_root):
            self.pids[10] = 999
            return 11

        self.resolve.side_effect = change_root
        with self.assertRaises(OSError):
            Keyboard().bind_window(10, expected_pid=101)

    def test_legacy_binding_adds_no_pid_queries(self):
        keyboard = Keyboard()
        keyboard.bind_window(10)
        keyboard.tap("a", 0.01)
        self.native.GetWindowThreadProcessId.assert_not_called()
        self.assertIsNone(keyboard._bound_identity)

    def test_all_modes_reject_new_down_after_root_or_child_reuse(self):
        for delivery in ("foreground", "background", "window_message", "hybrid"):
            for hwnd in (10, 11):
                with self.subTest(delivery=delivery, hwnd=hwnd):
                    self.pids.update({10: 101, 11: 101})
                    keyboard = self.keyboard(delivery)
                    self.pids[hwnd] = 999
                    self.native.SendInput.reset_mock()
                    self.native.PostMessageW.reset_mock()
                    with self.assertRaisesRegex(OSError, "身份变化"):
                        keyboard.down("a")
                    self.native.SendInput.assert_not_called()
                    self.native.PostMessageW.assert_not_called()

    def test_direct_hardware_send_also_rechecks_identity(self):
        keyboard = self.keyboard()
        self.pids.pop(10)
        with self.assertRaises(OSError):
            keyboard._send_input(vk_for("a"), False)
        self.native.SendInput.assert_not_called()

    def test_foreground_hold_repeat_refuses_reused_root_and_releases_own_key(self):
        keyboard = self.keyboard()
        keyboard.hold("z")
        self.pids[10] = 999
        with self.assertRaises(OSError):
            keyboard._repeat_key(vk_for("z"), keyboard._hold_repeat_at[vk_for("z")] + .01)
        self.assertEqual(self.hardware_events(), [False, True])
        self.assertFalse(keyboard.held)
        self.assertFalse(keyboard._hardware_down)

    def test_background_and_message_repeated_down_refuse_reused_child(self):
        self.native.GetForegroundWindow.return_value = 20
        for delivery in ("background", "window_message"):
            with self.subTest(delivery=delivery):
                self.pids[11] = 101
                keyboard = self.keyboard(delivery)
                keyboard.down("a")
                self.pids[11] = 999
                self.native.SendInput.reset_mock()
                self.native.PostMessageW.reset_mock()
                self.native.SendMessageTimeoutW.reset_mock()
                with self.assertRaises(OSError):
                    keyboard._repeat_key(vk_for("a"), 1.)
                self.native.SendInput.assert_not_called()
                self.native.PostMessageW.assert_not_called()
                self.native.SendMessageTimeoutW.assert_not_called()
                keyboard.release_all()

    def test_invalid_keyup_skips_window_messages_but_releases_owned_hardware(self):
        self.native.GetForegroundWindow.return_value = 20
        keyboard = self.keyboard("background")
        keyboard.down("a")
        self.pids[10] = 999
        self.native.PostMessageW.reset_mock()
        self.native.SendMessageTimeoutW.reset_mock()
        keyboard.up("a")
        self.assertEqual(self.hardware_events(), [False, True])
        self.native.PostMessageW.assert_not_called()
        self.native.SendMessageTimeoutW.assert_not_called()
        self.assertFalse(keyboard.held)

    def test_invalid_message_keyup_never_posts_to_reused_window(self):
        for delivery in ("window_message", "hybrid"):
            with self.subTest(delivery=delivery):
                self.pids.update({10: 101, 11: 101})
                self.native.GetForegroundWindow.return_value = 20
                keyboard = self.keyboard(delivery)
                keyboard.down("a")
                self.pids[11] = 999
                self.native.PostMessageW.reset_mock()
                keyboard.up("a")
                self.native.PostMessageW.assert_not_called()
                self.assertFalse(keyboard.held)
        self.native.SendInput.assert_not_called()

    def test_hybrid_hardware_repeat_and_movement_reject_changed_child(self):
        keyboard = self.keyboard("hybrid")
        keyboard.hold("z")
        self.pids[11] = 999
        with self.assertRaises(OSError):
            keyboard._repeat_key(vk_for("z"), keyboard._hold_repeat_at[vk_for("z")] + .01)
        with self.assertRaises(OSError):
            keyboard.movement_down("right")
        with self.assertRaises(OSError):
            keyboard.prepare_movement()
        self.assertEqual(self.hardware_events(), [False, True])

    def test_rebinding_releases_old_window_before_replacing_target(self):
        keyboard = self.keyboard("window_message")
        keyboard.down("a")
        keyboard.bind_window(20, expected_pid=202)
        keyboard.down("a")
        self.assertEqual([(c.args[0], c.args[1]) for c in self.native.PostMessageW.call_args_list],
                         [(11, WM_KEYDOWN), (11, WM_KEYUP), (21, WM_KEYDOWN)])

    def test_hardware_release_failure_blocks_rebinding(self):
        keyboard = self.keyboard()
        keyboard.down("a")
        self.native.SendInput.return_value = 0
        with self.assertRaisesRegex(OSError, "尚未释放"):
            keyboard.bind_window(20, expected_pid=202)
        self.assertEqual(keyboard.root_hwnd, 10)
        self.assertIn(vk_for("a"), keyboard._hardware_down)
        self.native.SendInput.return_value = 1

    def test_release_failure_is_reported_before_panel_can_create_another_keyboard(self):
        for delivery in ("foreground", "window_message", "hybrid"):
            with self.subTest(delivery=delivery):
                keyboard = self.keyboard(delivery)
                keyboard.down("a")
                self.native.SendInput.return_value = 0
                self.native.PostMessageW.return_value = 0
                with self.assertRaisesRegex(OSError, "尚未释放"):
                    keyboard.release_all()
                self.assertIsNone(keyboard._repeat_thread)
                self.assertTrue(keyboard.held or keyboard._hardware_down or keyboard._hybrid_presses)
                self.native.SendInput.return_value = 1
                self.native.PostMessageW.return_value = 1
                keyboard.release_all()
                self.assertFalse(keyboard.held)
                self.assertFalse(keyboard._hardware_down)
                self.assertFalse(keyboard._hybrid_presses)

    def test_successful_second_hardware_release_clears_initial_failure(self):
        keyboard = self.keyboard()
        keyboard.down("a")
        self.native.SendInput.side_effect = [0, 1]
        keyboard.release_all()
        self.assertFalse(keyboard.held)
        self.assertFalse(keyboard._hardware_down)
        self.native.SendInput.side_effect = None

    def test_tap_rebind_releases_old_key_without_releasing_new_session_key(self):
        keyboard = self.keyboard("window_message")

        def switch_session(_seconds):
            keyboard.bind_window(20, expected_pid=202)
            keyboard.down("a")

        with patch("mbv.input.time.sleep", side_effect=switch_session):
            keyboard.tap("a")
        self.assertEqual([(c.args[0], c.args[1]) for c in self.native.PostMessageW.call_args_list],
                         [(11, WM_KEYDOWN), (11, WM_KEYUP), (21, WM_KEYDOWN)])
        self.assertIn(vk_for("a"), keyboard.held)

    def test_real_repeat_thread_releases_invalid_foreground_hold(self):
        keyboard = self.keyboard(real_repeat=True)
        released = threading.Event()

        def send(_count, event, _size):
            if event._obj.ki.dwFlags & KEYEVENTF_KEYUP:
                released.set()
            return 1

        self.native.SendInput.side_effect = send
        keyboard.hold("z")
        self.pids[11] = 999
        self.assertTrue(released.wait(1.), "身份失效后重复线程必须独立抬起自有按键")
        with keyboard._lock:
            self.assertFalse(keyboard.held)
        self.assertEqual(self.hardware_events(), [False, True])


if __name__ == "__main__":
    unittest.main()
