from __future__ import annotations

from contextvars import Context
import ctypes
from ctypes import wintypes
import unittest
from unittest.mock import MagicMock, patch

import mss
import numpy as np

from mbv import window


class WindowSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = {
            "profile": "newmaple",
            "window": {
                "exact_titles": ["NewMaple"],
                "title_contains": ["NewMaple"],
                "executable_contains": ["NewMaple.exe"],
            },
        }
        self.pids = {101: 1001, 102: 1002, 103: 1003}
        self.user32 = MagicMock()
        self.user32.IsWindow.return_value = True
        self.user32.IsIconic.return_value = False
        self.user32.GetWindowThreadProcessId.side_effect = self._get_pid
        self.user32.GetClientRect.side_effect = self._get_rect
        self.user32.ClientToScreen.return_value = True
        self.addCleanup(patch.stopall)
        patch.object(window, "user32", self.user32).start()
        self.processes = patch.object(
            window,
            "window_process_path",
            side_effect=lambda hwnd: (self.pids.get(hwnd, 0), r"C:\Games\NewMaple.exe"),
        ).start()
        self.visible = patch.object(window, "visible_windows", return_value=[]).start()

    def _get_pid(self, hwnd: int, pointer: object) -> int:
        ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD)).contents.value = self.pids.get(hwnd, 0)
        return 321

    @staticmethod
    def _get_rect(_hwnd: int, pointer: object) -> bool:
        rect = ctypes.cast(pointer, ctypes.POINTER(wintypes.RECT)).contents
        rect.right, rect.bottom = 1280, 720
        return True

    def target(self, hwnd: int = 101, title: str = "NewMaple") -> window.WindowTarget:
        return window.WindowTarget(hwnd, self.pids[hwnd], title, r"C:\Games\NewMaple.exe")

    def test_candidates_keep_two_same_title_instances_distinct(self) -> None:
        self.visible.return_value = [(101, "NewMaple"), (102, "NewMaple")]
        candidates = window.window_candidates(self.config)
        self.assertEqual([(item.hwnd, item.pid) for item in candidates], [(101, 1001), (102, 1002)])
        self.assertTrue(all(item.score >= 100 for item in candidates))
        self.user32.SetForegroundWindow.assert_not_called()
        self.user32.ShowWindow.assert_not_called()

    def test_candidates_include_unmatched_window_after_default_match(self) -> None:
        self.visible.return_value = [(102, "其他客户端"), (101, "NewMaple")]
        self.processes.side_effect = lambda hwnd: (
            self.pids[hwnd], r"C:\Apps\Other.exe" if hwnd == 102 else r"C:\Games\NewMaple.exe"
        )
        candidates = window.window_candidates(self.config)
        self.assertEqual([item.hwnd for item in candidates], [101, 102])
        self.assertEqual(candidates[1].score, 0)

    def test_preferred_title_and_executable_pair_outranks_old_filters(self) -> None:
        self.config["window"].update(preferred_title="我的客户端", preferred_executable="other.exe")
        self.visible.return_value = [(101, "NewMaple"), (102, "我的客户端")]
        self.processes.side_effect = lambda hwnd: (
            self.pids[hwnd], r"D:\New location\OTHER.EXE" if hwnd == 102 else r"C:\Games\NewMaple.exe"
        )
        candidates = window.window_candidates(self.config)
        self.assertEqual([item.hwnd for item in candidates], [102, 101])
        self.assertEqual(candidates[0].score, 1000)

    def test_title_alone_cannot_match_preferred_executable(self) -> None:
        self.config["window"].update(preferred_title="我的客户端", preferred_executable="other.exe")
        self.visible.return_value = [(101, "我的客户端")]
        self.assertEqual(window.window_candidates(self.config)[0].score, 200)

    def test_incomplete_preference_does_not_receive_bonus(self) -> None:
        self.config["window"]["preferred_title"] = "NewMaple"
        self.visible.return_value = [(101, "NewMaple")]
        self.assertEqual(window.window_candidates(self.config)[0].score, 310)

    def test_candidates_skip_own_process_and_unknown_process(self) -> None:
        self.visible.return_value = [(101, "NewMaple"), (102, "Other"), (103, "NewMaple")]
        self.pids.update({101: 555, 102: 0})
        with patch.object(window.os, "getpid", return_value=555):
            self.assertEqual([item.hwnd for item in window.window_candidates(self.config)], [103])

    def test_candidates_skip_closed_minimized_and_unreadable_windows(self) -> None:
        self.visible.return_value = [(101, "NewMaple"), (102, "NewMaple"), (103, "NewMaple")]
        self.user32.IsWindow.side_effect = lambda hwnd: hwnd != 101
        self.user32.IsIconic.side_effect = lambda hwnd: hwnd == 102
        self.user32.GetClientRect.return_value = False
        self.user32.GetClientRect.side_effect = None
        with patch.object(window.time, "sleep") as sleep:
            self.assertEqual(window.window_candidates(self.config), [])
        sleep.assert_not_called()

    def test_candidates_skip_too_small_client_without_waiting(self) -> None:
        self.visible.return_value = [(101, "NewMaple")]

        def short_rect(_hwnd: int, pointer: object) -> bool:
            rect = ctypes.cast(pointer, ctypes.POINTER(wintypes.RECT)).contents
            rect.right, rect.bottom = 319, 720
            return True

        self.user32.GetClientRect.side_effect = short_rect
        with patch.object(window.time, "sleep") as sleep:
            self.assertEqual(window.window_candidates(self.config), [])
        sleep.assert_not_called()

    def test_resolve_returns_pid_bound_geometry(self) -> None:
        resolved = window.resolve_window_target(self.target())
        self.assertEqual((resolved.hwnd, resolved.pid, resolved.width, resolved.height), (101, 1001, 1280, 720))

    def test_validate_rejects_same_handle_reused_by_another_process(self) -> None:
        target = self.target()
        self.pids[101] = 2001
        with self.assertRaisesRegex(RuntimeError, "进程已变化"):
            window.validate_window_target(target)

    def test_validate_rejects_unknown_pid_and_closed_window(self) -> None:
        with self.assertRaises(RuntimeError):
            window.validate_window_target(window.WindowTarget(101, 0, "NewMaple", ""))
        self.user32.IsWindow.return_value = False
        with self.assertRaises(RuntimeError):
            window.validate_window_target(self.target())

    def test_resolve_rejects_minimized_window_without_restoring_it(self) -> None:
        self.user32.IsIconic.return_value = True
        with self.assertRaisesRegex(RuntimeError, "最小化"):
            window.resolve_window_target(self.target())
        self.user32.ShowWindow.assert_not_called()

    def test_resolve_rechecks_identity_after_reading_geometry(self) -> None:
        target = self.target()

        def changed_rect(hwnd: int, pointer: object) -> bool:
            self.pids[hwnd] = 2001
            return self._get_rect(hwnd, pointer)

        self.user32.GetClientRect.side_effect = changed_rect
        with self.assertRaisesRegex(RuntimeError, "进程已变化"):
            window.resolve_window_target(target)

    def test_context_selects_exact_instance_without_running_default_search(self) -> None:
        with window.selected_window(self.target(102)):
            resolved = window.find_game_window(self.config)
        self.assertEqual(resolved.hwnd, 102)
        self.visible.assert_not_called()

    def test_invalid_context_target_does_not_fallback_to_matching_window(self) -> None:
        target = self.target(102)
        self.pids[102] = 2222
        self.visible.return_value = [(101, "NewMaple")]
        with window.selected_window(target), self.assertRaises(RuntimeError):
            window.find_game_window(self.config)
        self.visible.assert_not_called()

    def test_nested_context_restores_previous_target_after_exception(self) -> None:
        with window.selected_window(self.target(101)):
            with self.assertRaisesRegex(ValueError, "cancel"):
                with window.selected_window(self.target(102)):
                    self.assertEqual(window.find_game_window(self.config).hwnd, 102)
                    raise ValueError("cancel")
            self.assertEqual(window.find_game_window(self.config).hwnd, 101)
        self.visible.return_value = [(103, "NewMaple")]
        self.assertEqual(window.find_game_window(self.config).hwnd, 103)

    def test_selection_does_not_leak_to_another_context_or_mutate_profile(self) -> None:
        self.visible.return_value = [(101, "NewMaple")]
        classic = {"profile": "classic", "window": {"exact_titles": ["NewMaple"]}}
        with window.selected_window(self.target(102)):
            self.assertEqual(window.find_game_window(self.config).hwnd, 102)
            other_context_window = Context().run(window.find_game_window, classic)
        self.assertEqual(other_context_window.hwnd, 101)
        self.assertEqual(classic, {"profile": "classic", "window": {"exact_titles": ["NewMaple"]}})
        self.assertNotIn("hwnd", self.config["window"])

    def test_legacy_default_search_preserves_filter_ranking(self) -> None:
        self.visible.return_value = [(101, "NewMaple 攻略"), (102, "NewMaple")]
        self.processes.side_effect = lambda hwnd: (
            self.pids[hwnd], r"C:\Apps\Browser.exe" if hwnd == 101 else r"C:\Games\NewMaple.exe"
        )
        found = window.find_game_window(self.config)
        self.assertEqual(found.hwnd, 102)
        self.assertEqual(found.pid, 0)

    def test_focus_rejects_reused_handle_before_focus_api_calls(self) -> None:
        resolved = window.resolve_window_target(self.target())
        self.pids[101] = 2001
        with self.assertRaisesRegex(RuntimeError, "进程已变化"):
            window.focus_game_window(resolved)
        self.user32.SetForegroundWindow.assert_not_called()
        self.user32.ShowWindow.assert_not_called()

    def test_topmost_rejects_reused_handle_before_window_change(self) -> None:
        resolved = window.resolve_window_target(self.target())
        self.pids[101] = 2001
        with self.assertRaisesRegex(RuntimeError, "进程已变化"):
            window.set_window_topmost(resolved, False)
        self.user32.SetWindowPos.assert_not_called()

    def test_capture_rejects_reused_handle_before_grabbing(self) -> None:
        resolved = window.resolve_window_target(self.target())
        self.pids[101] = 2001
        sct = MagicMock()
        with self.assertRaisesRegex(RuntimeError, "进程已变化"):
            window.capture_client(sct, resolved)
        sct.grab.assert_not_called()

    def test_capture_rejects_minimized_selected_window(self) -> None:
        resolved = window.resolve_window_target(self.target())
        self.user32.IsIconic.return_value = True
        sct = MagicMock()
        with self.assertRaisesRegex(RuntimeError, "最小化"):
            window.capture_client(sct, resolved)
        sct.grab.assert_not_called()

    def test_capture_checks_identity_again_before_retry(self) -> None:
        resolved = window.resolve_window_target(self.target())
        sct = MagicMock()

        def fail_and_change(_monitor: object) -> None:
            self.pids[101] = 2001
            raise mss.exception.ScreenShotError("temporary capture failure")

        sct.grab.side_effect = fail_and_change
        with patch.object(window.time, "sleep"), self.assertRaisesRegex(RuntimeError, "进程已变化"):
            window.capture_client(sct, resolved, attempts=3)
        self.assertEqual(sct.grab.call_count, 1)

    def test_capture_discards_frame_if_identity_changed_during_grab(self) -> None:
        resolved = window.resolve_window_target(self.target())
        sct = MagicMock()

        def changed_frame(_monitor: object) -> np.ndarray:
            self.pids[101] = 2001
            return np.zeros((2, 3, 4), dtype=np.uint8)

        sct.grab.side_effect = changed_frame
        with self.assertRaisesRegex(RuntimeError, "进程已变化"):
            window.capture_client(sct, resolved)

    def test_capture_legacy_window_does_not_require_pid(self) -> None:
        old_window = window.WindowInfo(101, "NewMaple", 0, 0, 1280, 720)
        sct = MagicMock()
        sct.grab.return_value = np.zeros((2, 3, 4), dtype=np.uint8)
        result = window.capture_client(sct, old_window)
        self.assertEqual(result.shape, (2, 3, 3))
        self.user32.GetWindowThreadProcessId.assert_not_called()


if __name__ == "__main__":
    unittest.main()
