from __future__ import annotations

import ctypes
from ctypes import wintypes
import unittest
from unittest.mock import MagicMock, patch

from mbv import window


class WindowHandleTests(unittest.TestCase):
    def test_decimal_and_hex_use_exact_handle_without_title_search(self):
        for text in ("123", " 0x7b ", "0X7B", "000123"):
            with self.subTest(text=text):
                native = MagicMock()
                native.IsWindow.return_value = True
                native.GetWindowTextLengthW.return_value = 0

                def process_id(_hwnd, pointer):
                    ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD)).contents.value = 456
                    return 1

                native.GetWindowThreadProcessId.side_effect = process_id
                with patch.object(window, "user32", native), \
                     patch.object(window, "window_process_path", return_value=(456, "game.exe")), \
                     patch.object(window.os, "getpid", return_value=999), \
                     patch.object(window, "resolve_window_target") as resolve, \
                     patch.object(window, "visible_windows") as enumerate_windows, \
                     patch.object(window, "find_game_window") as find:
                    target = window.window_target_from_handle(text)
                self.assertEqual(target, window.WindowTarget(123, 456, "", "game.exe"))
                resolve.assert_called_once_with(target)
                enumerate_windows.assert_not_called()
                find.assert_not_called()

    def test_invalid_input_never_calls_windows(self):
        for text in ("", "0", "0x0", "-1", "+1", "1.2", "abc", "123h", "0b10",
                     "1_000", "１２３", "0x", str(1 << (ctypes.sizeof(ctypes.c_void_p) * 8))):
            with self.subTest(text=text), patch.object(window, "user32") as native:
                with self.assertRaises(ValueError):
                    window.window_target_from_handle(text)
                self.assertEqual(native.mock_calls, [])

    def test_title_lookup_preserves_native_pointer_width(self):
        hwnd = (1 << 40) + 123 if ctypes.sizeof(ctypes.c_void_p) == 8 else 123
        with patch.object(window, "user32") as native, \
             patch.object(window, "window_process_path", return_value=(456, "game.exe")), \
             patch.object(window.os, "getpid", return_value=999), \
             patch.object(window, "_validate_window_identity"), \
             patch.object(window, "resolve_window_target"):
            native.GetWindowTextLengthW.return_value = 0
            target = window.window_target_from_handle(hex(hwnd))
            self.assertEqual(target.hwnd, hwnd)
            self.assertEqual(native.GetWindowTextLengthW.call_args.args[0].value, hwnd)
            self.assertEqual(native.GetWindowTextW.call_args.args[0].value, hwnd)

    def test_invalid_window_does_not_look_up_title_or_process(self):
        with patch.object(window, "user32") as native, \
             patch.object(window, "window_process_path") as process:
            native.IsWindow.return_value = False
            with self.assertRaisesRegex(RuntimeError, "不存在"):
                window.window_target_from_handle("123")
            process.assert_not_called()

    def test_own_window_is_rejected(self):
        with patch.object(window, "user32"), \
             patch.object(window, "window_process_path", return_value=(456, "python.exe")), \
             patch.object(window.os, "getpid", return_value=456):
            with self.assertRaisesRegex(RuntimeError, "自身"):
                window.window_target_from_handle("123")

    def test_pid_change_is_rejected_before_resolving_geometry(self):
        native = MagicMock()
        native.GetWindowThreadProcessId.return_value = 0
        with patch.object(window, "user32", native), \
             patch.object(window, "window_process_path", return_value=(456, "game.exe")), \
             patch.object(window.os, "getpid", return_value=999), \
             patch.object(window, "resolve_window_target") as resolve:
            with self.assertRaisesRegex(RuntimeError, "进程已变化"):
                window.window_target_from_handle("123")
            resolve.assert_not_called()

    def test_minimized_or_closed_target_propagates_without_fallback(self):
        with patch.object(window, "user32") as native, \
             patch.object(window, "window_process_path", return_value=(456, "game.exe")), \
             patch.object(window.os, "getpid", return_value=999), \
             patch.object(window, "_validate_window_identity"), \
             patch.object(window, "resolve_window_target", side_effect=RuntimeError("已最小化")), \
             patch.object(window, "find_game_window") as find:
            native.GetWindowTextLengthW.return_value = 0
            with self.assertRaisesRegex(RuntimeError, "最小化"):
                window.window_target_from_handle("123")
            find.assert_not_called()
