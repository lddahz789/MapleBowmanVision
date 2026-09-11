import threading
import unittest
from unittest.mock import MagicMock, patch

from mbv.input import Keyboard, vk_for


class ContinuousHoldTests(unittest.TestCase):
    def test_real_foreground_worker_releases_hold_without_another_frame(self):
        keyboard = Keyboard("foreground")
        released = threading.Event()
        keyboard._send_input = MagicMock(side_effect=lambda vk, up: released.set() if up else None)
        try:
            keyboard.hold("z", seconds=.1)
            self.assertTrue(released.wait(1.), "前台首次 hold 必须启动独立超时松键线程")
            with keyboard._lock:
                self.assertFalse(keyboard.held)
        finally:
            keyboard.release_all()

    def test_foreground_hold_starts_worker_after_registering_deadline(self):
        keyboard = Keyboard("foreground")
        keyboard._send_input = MagicMock()
        with patch("mbv.input.threading.Thread") as thread:
            keyboard.hold("z")
            thread.assert_called_once()
            thread.return_value.start.assert_called_once()
            self.assertIn(vk_for("z"), keyboard._movement_deadlines)
            keyboard.release_all()

    def test_hold_repeats_keydown_without_keyup_until_release(self):
        for delivery in ("foreground", "background", "window_message"):
            with self.subTest(delivery=delivery):
                keyboard = Keyboard(delivery)
                keyboard.hwnd = keyboard.root_hwnd = 10
                keyboard._send_input = MagicMock()
                keyboard._ensure_repeat_thread = MagicMock()
                with patch("mbv.input.user32.IsWindow", return_value=True), \
                     patch("mbv.input.user32.PostMessageW", return_value=True) as post, \
                     patch("mbv.input.window_is_foreground", return_value=True), \
                     patch("mbv.input.time.monotonic", return_value=10.):
                    keyboard.hold("z")
                    with keyboard._lock:
                        for now in (10.11, 10.12, 10.22, 10.23, 10.33):
                            keyboard._repeat_key(vk_for("z"), now)
                    channel = post if delivery == "window_message" else keyboard._send_input
                    self.assertEqual(channel.call_count, 4)
                    if delivery == "window_message":
                        self.assertTrue(all(c.args[1] == 0x100 for c in post.call_args_list))
                        self.assertTrue(all(c.args[3] & (1 << 30) for c in post.call_args_list[1:]))
                    else:
                        self.assertTrue(all(c.args == (vk_for("z"), False) for c in channel.call_args_list))
                    keyboard.up("z")
                    self.assertEqual(channel.call_count, 5)
                    self.assertFalse(keyboard._hold_repeat_at)

    def test_foreground_hold_stops_repeating_on_focus_loss(self):
        keyboard = Keyboard("foreground")
        keyboard._send_input = MagicMock()
        keyboard._ensure_repeat_thread = MagicMock()
        with patch("mbv.input.time.monotonic", return_value=10.), \
             patch("mbv.input.window_is_foreground", return_value=False):
            keyboard.hold("z")
            with keyboard._lock:
                keyboard._repeat_key(vk_for("z"), 10.2)
        self.assertEqual(keyboard._send_input.call_args.args, (vk_for("z"), True))
        self.assertEqual(keyboard._send_input.call_count, 2)
        self.assertFalse(keyboard.held)

    def test_normal_direction_hold_does_not_enable_new_repetition(self):
        keyboard = Keyboard("foreground")
        keyboard._send_input = MagicMock()
        keyboard._ensure_repeat_thread = MagicMock()
        keyboard.down("right")
        with keyboard._lock:
            keyboard._repeat_key(vk_for("right"), 10.)
            keyboard._repeat_key(vk_for("right"), 10.2)
        self.assertEqual(keyboard._send_input.call_count, 1)
        self.assertFalse(keyboard._hold_repeat_at)
        keyboard.up("right")

    def test_non_hybrid_modes_hold_without_repeated_taps_and_expire(self):
        for delivery in ("foreground", "background", "window_message"):
            with self.subTest(delivery=delivery):
                keyboard = Keyboard(delivery)
                keyboard.hwnd = keyboard.root_hwnd = 10
                keyboard._send_input = MagicMock()
                keyboard._ensure_repeat_thread = MagicMock()
                with patch("mbv.input.user32.IsWindow", return_value=True), \
                     patch("mbv.input.user32.PostMessageW", return_value=True) as post, \
                     patch("mbv.input.window_is_foreground", return_value=True), \
                     patch("mbv.input.time.monotonic", return_value=10.) as clock:
                    keyboard.hold("z")
                    clock.return_value = 10.5
                    keyboard.hold("z")
                    channel = post if delivery == "window_message" else keyboard._send_input
                    self.assertEqual(channel.call_count, 1)
                    self.assertIn(vk_for("z"), keyboard.held)
                    self.assertAlmostEqual(keyboard._movement_deadlines[vk_for("z")], 11.3)
                    with keyboard._lock:
                        keyboard._repeat_key(vk_for("z"), 11.31)
                    self.assertEqual(channel.call_count, 2)
                    self.assertFalse(keyboard.held)
                    keyboard.release_all()

    def test_explicit_release_cancels_hold_deadline(self):
        keyboard = Keyboard("foreground")
        keyboard._send_input = MagicMock()
        keyboard._ensure_repeat_thread = MagicMock()
        keyboard.hold("z")
        keyboard.up("z")
        self.assertFalse(keyboard._movement_deadlines)
        self.assertFalse(keyboard.held)
        self.assertEqual(keyboard._send_input.call_count, 2)
