from __future__ import annotations

import threading
import ctypes
from ctypes import wintypes
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from mbv.background_capture import BackgroundCapture, BackgroundCaptureError
from mbv.bot import BowmanBot
from mbv.input import Keyboard, input_delivery, vk_for
from mbv.window import WindowInfo
from mbv.win32 import WM_KEYDOWN, WM_KEYUP, user32


class BackgroundKeyboardTests(unittest.TestCase):
    def test_real_windows_message_queue_receives_input_without_changing_focus(self):
        # 创建本测试自己的不可见消息窗口，不向任何用户程序发送按键。
        callback_type = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
        )

        class WindowClass(ctypes.Structure):
            _fields_ = [
                ("style", wintypes.UINT), ("procedure", callback_type),
                ("class_extra", ctypes.c_int), ("window_extra", ctypes.c_int),
                ("instance", wintypes.HINSTANCE), ("icon", wintypes.HICON),
                ("cursor", wintypes.HANDLE), ("background", wintypes.HBRUSH),
                ("menu", wintypes.LPCWSTR), ("name", wintypes.LPCWSTR),
            ]

        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        user32.RegisterClassW.argtypes = [ctypes.POINTER(WindowClass)]
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p,
        ]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.DestroyWindow.argtypes = [wintypes.HWND]
        user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
        user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.DispatchMessageW.restype = ctypes.c_ssize_t
        events = []

        def receive(hwnd, message, key, lparam):
            if message in (WM_KEYDOWN, WM_KEYUP):
                events.append((message, key))
                return 0
            return user32.DefWindowProcW(hwnd, message, key, lparam)

        definition = WindowClass()
        definition.procedure = callback_type(receive)
        definition.name = "MBVBackgroundMessageTest"
        self.assertTrue(user32.RegisterClassW(ctypes.byref(definition)))
        hwnd = None
        foreground = user32.GetForegroundWindow()
        try:
            hwnd = user32.CreateWindowExW(0, definition.name, "", 0, 0, 0, 0, 0, -3, None, None, None)
            self.assertTrue(hwnd)
            keyboard = Keyboard("window_message")
            keyboard.bind_window(hwnd)
            with patch("mbv.input.user32.SendInput") as send:
                keyboard.tap("right", 0.01)
                send.assert_not_called()
            message = wintypes.MSG()
            while user32.PeekMessageW(ctypes.byref(message), hwnd, 0, 0, 1):
                user32.DispatchMessageW(ctypes.byref(message))
            self.assertEqual(events, [(WM_KEYDOWN, 39), (WM_KEYUP, 39)])
            self.assertEqual(user32.GetForegroundWindow(), foreground)
        finally:
            if hwnd:
                user32.DestroyWindow(hwnd)
            user32.UnregisterClassW(definition.name, None)

    def test_message_mode_never_sends_global_input_or_focus_messages(self):
        for foreground in (123, 999):
            with self.subTest(foreground=foreground), patch(
                "mbv.input.user32.PostMessageW", return_value=1
            ) as post, patch("mbv.input.user32.IsWindow", return_value=True), patch(
                "mbv.input.user32.GetForegroundWindow", return_value=foreground
            ), patch("mbv.input.user32.SendInput") as send, patch(
                "mbv.input.user32.SendMessageTimeoutW"
            ) as timed:
                keyboard = Keyboard("window_message")
                keyboard.root_hwnd, keyboard.hwnd = 123, 456
                keyboard.tap("a", 0.01)
                self.assertEqual(
                    [(call.args[0], call.args[1], call.args[2]) for call in post.call_args_list],
                    [(456, WM_KEYDOWN, vk_for("a")), (456, WM_KEYUP, vk_for("a"))],
                )
                send.assert_not_called()
                timed.assert_not_called()

    def test_message_failure_never_falls_back_to_hardware(self):
        with patch("mbv.input.user32.IsWindow", return_value=True), patch(
            "mbv.input.user32.PostMessageW", return_value=0
        ), patch("mbv.input.user32.SendInput") as send:
            keyboard = Keyboard("window_message")
            keyboard.hwnd = 123
            with self.assertRaises(OSError):
                keyboard.tap("right", 0.01)
            send.assert_not_called()

    def test_foreground_pause_releases_held_key(self):
        keyboard = Keyboard("foreground")
        with patch.object(keyboard, "_send_input") as send:
            keyboard.down("right")
            keyboard.release_all()
        self.assertEqual([call.args for call in send.call_args_list], [(39, False), (39, True)])
        self.assertFalse(keyboard.held)

    def test_repeat_is_finished_before_final_keyup(self):
        keyboard = Keyboard("window_message")
        keyboard.hwnd = 123
        repeated = threading.Event()
        events = []

        def dispatch(vk, key_up, *, was_down=False):
            events.append((vk, key_up))
            if was_down and not key_up:
                repeated.set()

        with patch.object(keyboard, "_dispatch", side_effect=dispatch):
            try:
                keyboard.down("right")
                self.assertTrue(repeated.wait(1.0))
            finally:
                keyboard.release_all()
        self.assertEqual(events[-1], (39, True))
        self.assertFalse(keyboard.held)
        self.assertIsNone(keyboard._repeat_thread)

    def test_new_mode_and_legacy_aliases_remain_distinct(self):
        self.assertEqual(input_delivery({"input": {"delivery": "window_message"}}), "window_message")
        self.assertEqual(input_delivery({"input": {"delivery": "postmessage"}}), "background")


class BackgroundCaptureTests(unittest.TestCase):
    window = WindowInfo(123, "Test", 0, 0, 640, 480)

    def capture_fixture(self):
        capture = BackgroundCapture(timeout=0.01)
        connection = MagicMock()
        process = MagicMock()
        process.pid = 123
        capture._process, capture._connection = process, connection
        return capture, connection, process

    def test_timeout_terminates_capture_helper_and_discards_connection(self):
        capture, connection, process = self.capture_fixture()
        connection.poll.return_value = False
        with self.assertRaisesRegex(BackgroundCaptureError, "超时"):
            capture.capture(self.window)
        process.terminate.assert_called_once()
        connection.close.assert_called_once()
        self.assertIsNone(capture._process)
        with patch.object(capture, "_start") as start:
            with self.assertRaisesRegex(BackgroundCaptureError, "重试"):
                capture.capture(self.window)
            start.assert_not_called()

    def test_blank_or_unsupported_capture_is_reported_as_error(self):
        capture, connection, process = self.capture_fixture()
        connection.poll.return_value = True
        connection.recv.return_value = (False, "窗口截图为空白")
        with self.assertRaisesRegex(BackgroundCaptureError, "空白"):
            capture.capture(self.window)
        process.terminate.assert_called_once()

    def test_valid_frame_is_returned_and_helper_is_reused(self):
        capture, connection, _process = self.capture_fixture()
        frame = np.full((480, 640, 3), 128, dtype=np.uint8)
        connection.poll.return_value = True
        connection.recv.return_value = (True, frame)
        try:
            self.assertIs(capture.capture(self.window), frame)
            self.assertIs(capture.capture(self.window), frame)
            self.assertEqual(connection.send.call_count, 2)
        finally:
            capture.close()


class BackgroundRuntimeTests(unittest.TestCase):
    def test_message_mode_ignores_topmost_preference_and_does_not_focus_game(self):
        instance = BowmanBot.__new__(BowmanBot)
        instance.armed = False
        instance.input_authorized = instance.integrity_ok = True
        instance.config = {"calibrated": True, "window": {"topmost_while_armed": True}}
        instance.strategy = MagicMock(capture_fields=())
        instance.player_templates = [MagicMock()]
        instance.templates = []
        instance.delivery = "window_message"
        instance.background_input = True
        instance.keyboard = MagicMock()
        instance.notify = MagicMock()
        instance.log = MagicMock()
        window = WindowInfo(123, "NewMaple", 0, 0, 640, 480)
        with patch("mbv.bot.missing_recognition_data", return_value=[]), patch(
            "mbv.bot.user32.IsWindow", return_value=True
        ), patch("mbv.bot.user32.IsIconic", return_value=False), patch(
            "mbv.bot.user32.GetForegroundWindow", return_value=999
        ), patch("mbv.bot.client_window", return_value=window), patch(
            "mbv.bot.focus_game_window"
        ) as focus, patch("mbv.bot.set_window_topmost") as topmost, patch(
            "mbv.bot.user32.MessageBeep"
        ):
            instance._toggle(window)
        self.assertTrue(instance.armed)
        self.assertFalse(instance.window_topmost)
        focus.assert_not_called()
        topmost.assert_not_called()


if __name__ == "__main__":
    unittest.main()
