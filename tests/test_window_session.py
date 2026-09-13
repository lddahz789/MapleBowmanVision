from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
import queue
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, call, patch

import numpy as np

from mbv.bot import BowmanBot
from mbv.config import load_config
from mbv.overlay import RuntimeOverlay
from mbv.strategies.base import TargetSelection
from mbv.vision import Detection
from mbv.window import WindowInfo, WindowTarget


class WindowSessionTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name in ("Keyboard", "SessionLog", "BackgroundCapture", "mss.MSS"):
            self.stack.enter_context(patch(f"mbv.bot.{name}"))
        self.stack.enter_context(patch("mbv.bot.load_templates", return_value=[]))
        self.stack.enter_context(patch("mbv.bot.process_integrity_level", return_value=0))
        self.stack.enter_context(patch("mbv.bot.window_process_path", return_value=(456, "game.exe")))
        self.stack.enter_context(patch.object(BowmanBot, "monitor_hotkeys"))
        self.stop_monitor = self.stack.enter_context(patch.object(BowmanBot, "stop_hotkey_monitor"))
        config = load_config(Path(__file__).resolve().parents[1] / "config.example.json")
        config["input"]["delivery"] = "foreground"
        self.bot = BowmanBot(config, input_authorized=False)
        self.overlay = MagicMock()
        self.window = WindowInfo(123, "测试游戏", 0, 0, 400, 300, pid=456)
        self.target = WindowTarget(123, 456, "测试游戏", "game.exe")

    def test_initial_binding_failure_keeps_panel_overlay_and_cleans_input(self):
        self.bot.localization_diagnostics = MagicMock()
        with patch("mbv.bot.resolve_window_target", side_effect=RuntimeError("窗口已关闭")), \
             patch("mbv.bot.find_game_window") as find:
            with self.assertRaisesRegex(RuntimeError, "窗口已关闭"):
                self.bot.run(self.overlay, target=self.target, close_overlay_on_exit=False)
        find.assert_not_called()
        self.bot.keyboard.release_all.assert_called_once()
        self.assertFalse(self.bot.auto_potion.enabled)
        self.assertIsNone(self.bot.window)
        self.bot.localization_diagnostics.close.assert_called_once_with()
        self.overlay.close.assert_not_called()
        self.overlay.update.assert_called_once_with({"background_hidden": True})

    def test_selected_window_disappears_before_first_capture_and_never_falls_back(self):
        with patch("mbv.bot.resolve_window_target", side_effect=[self.window, RuntimeError("窗口已失效")]), \
             patch("mbv.bot.validate_window_target"), \
             patch("mbv.bot.capture_client") as capture, \
             patch("mbv.bot.find_game_window") as find:
            with self.assertRaisesRegex(RuntimeError, "窗口已失效"):
                self.bot.run(self.overlay, target=self.target, close_overlay_on_exit=False)
        self.bot.keyboard.bind_window.assert_called_once_with(123, expected_pid=456)
        self.bot.keyboard.release_all.assert_called_once()
        capture.assert_not_called()
        find.assert_not_called()
        self.stop_monitor.assert_called()
        self.overlay.close.assert_not_called()

    def test_selected_session_can_finish_without_destroying_hud(self):
        frame = np.zeros((300, 400, 3), dtype=np.uint8)
        self.overlay.update.side_effect = lambda state: self.bot.f9_requested.set() if "width" in state else None
        self.bot.act = MagicMock()
        with patch("mbv.bot.resolve_window_target", return_value=self.window), \
             patch("mbv.bot.validate_window_target"), \
             patch("mbv.bot.client_window", return_value=self.window), \
             patch("mbv.bot.capture_client", return_value=frame), \
             patch("mbv.bot.find_game_window") as find, \
             patch("mbv.bot.time.sleep"):
            self.bot.run(self.overlay, target=self.target, close_overlay_on_exit=False)
        find.assert_not_called()
        self.bot.act.assert_called_once()
        self.assertFalse(self.bot.armed)
        self.overlay.close.assert_not_called()
        self.assertEqual(self.overlay.update.call_args.args[0], {"background_hidden": True})

    def _run_two_frames(self, *, selected: bool, monster: Detection | None) -> None:
        frame = np.zeros((300, 400, 3), dtype=np.uint8)
        completed_frames = []

        def finish_second_frame(state):
            if "width" in state:
                completed_frames.append(state)
                if len(completed_frames) == 2:
                    self.bot.f9_requested.set()

        def resolve(target):
            self.assertIsInstance(target, WindowTarget)
            self.assertIs(target, self.target)
            return self.window

        selection = TargetSelection(target=monster, chase_target=None)
        self.bot.strategy = SimpleNamespace(
            select_targets=MagicMock(return_value=selection), capture_fields=(),
        )
        self.bot.act = MagicMock()
        self.overlay.update.side_effect = finish_second_frame
        with patch("mbv.bot.resolve_window_target", side_effect=resolve) as resolve_target, \
             patch("mbv.bot.validate_window_target"), \
             patch("mbv.bot.find_game_window", return_value=self.window) as find, \
             patch("mbv.bot.client_window", return_value=self.window), \
             patch("mbv.bot.capture_client", return_value=frame) as capture, \
             patch("mbv.bot.find_detections", return_value=([], -1.0, None)), \
             patch.object(self.bot, "_track_player", return_value=None), \
             patch("mbv.bot.user32"), \
             patch("mbv.bot.threading.Thread"), \
             patch("mbv.bot.time.sleep"), \
             patch("mbv.bot.time.monotonic", return_value=1000.0), \
             patch("mbv.bot.time.perf_counter_ns", return_value=1000000):
            self.bot.run(
                self.overlay, target=self.target if selected else None,
                close_overlay_on_exit=False,
            )
        self.assertEqual(len(completed_frames), 2)
        self.assertEqual(capture.call_count, 2)
        self.assertEqual(self.bot.act.call_count, 2)
        self.assertEqual([item.args[5] for item in self.bot.act.call_args_list],
                         [monster.box if monster is not None else None] * 2)
        if selected:
            find.assert_not_called()
            # 初次连接和每帧开始都复核同一个会话，不能被本帧怪物目标覆盖。
            self.assertEqual(resolve_target.call_args_list, [call(self.target)] * 3)
        else:
            find.assert_called_once_with(self.bot.config)
            resolve_target.assert_not_called()
        self.bot.keyboard.tap.assert_not_called()
        self.bot.keyboard.down.assert_not_called()

    def test_selected_target_is_preserved_across_two_frames_with_monster(self):
        self._run_two_frames(selected=True, monster=Detection((10, 10, 20, 20), 0.95, "测试怪物"))

    def test_selected_target_is_revalidated_across_two_frames_without_monster(self):
        self._run_two_frames(selected=True, monster=None)

    def test_legacy_session_does_not_resolve_monster_as_window_on_second_frame(self):
        self._run_two_frames(selected=False, monster=Detection((10, 10, 20, 20), 0.95, "测试怪物"))

    def test_legacy_session_failure_still_closes_its_own_overlay(self):
        with patch("mbv.bot.find_game_window", side_effect=RuntimeError("无窗口")) as find:
            with self.assertRaisesRegex(RuntimeError, "无窗口"):
                self.bot.run(self.overlay)
        find.assert_called_once_with(self.bot.config)
        self.overlay.close.assert_called_once()

    def test_stop_or_capture_requested_during_detection_blocks_old_frame_action(self):
        for event_name in ("f9_requested", "vision_suspended"):
            with self.subTest(event=event_name):
                self.bot._try_auto_potion = MagicMock()
                self.bot._act = MagicMock()
                event = getattr(self.bot, event_name)
                event.set()
                self.bot.act(self.window, 1., 1., None, None, None, None, 400, False, 1.)
                event.clear()
                self.bot._try_auto_potion.assert_not_called()
                self.bot._act.assert_not_called()

    def test_action_revalidates_selected_target_before_potion_or_combat(self):
        self.bot._window_target = self.target
        self.bot._try_auto_potion = MagicMock()
        self.bot._act = MagicMock()
        with patch("mbv.bot.resolve_window_target", side_effect=RuntimeError("进程变化")):
            with self.assertRaisesRegex(RuntimeError, "进程变化"):
                self.bot.act(self.window, 1., 1., None, None, None, None, 400, False, 1.)
        self.bot._try_auto_potion.assert_not_called()
        self.bot._act.assert_not_called()

    def test_input_mode_reload_preserves_exact_selected_pid(self):
        self.bot._window_target = self.target
        self.bot.window = self.window
        self.bot.keyboard.root_hwnd = 123
        changed = deepcopy(self.bot.config)
        changed["input"]["delivery"] = "window_message"
        with patch("mbv.bot.validate_window_target"):
            self.bot.apply_config(changed)
        self.bot.keyboard.bind_window.assert_called_with(123, expected_pid=456)
        self.assertEqual(self.bot._window_target, self.target)

    def test_input_binding_rejects_different_hwnd(self):
        self.bot._window_target = self.target
        with patch("mbv.bot.validate_window_target"):
            with self.assertRaisesRegex(OSError, "不一致"):
                self.bot._bind_input_window(999)
        self.bot.keyboard.bind_window.assert_not_called()

    def test_disconnected_session_can_change_input_mode_without_rebinding_old_window(self):
        self.bot._window_target = self.target
        self.bot.window = None
        self.bot.keyboard.root_hwnd = 123
        old_keyboard = self.bot.keyboard
        changed = deepcopy(self.bot.config)
        changed["input"]["delivery"] = "window_message"
        with patch("mbv.bot.validate_window_target", side_effect=RuntimeError("已关闭")) as validate:
            self.bot.apply_config(changed)
        validate.assert_not_called()
        old_keyboard.release_all.assert_called()
        self.bot.keyboard.bind_window.assert_not_called()
        self.assertEqual(self.bot.delivery, "window_message")
        self.assertEqual(self.bot.config["input"]["delivery"], "window_message")


class OverlaySessionTests(unittest.TestCase):
    def test_reset_clears_pending_and_drawn_state_without_closing_overlay(self):
        overlay = RuntimeOverlay.__new__(RuntimeOverlay)
        overlay._root = MagicMock()
        overlay._exit_root = MagicMock()
        overlay._canvas = MagicMock()
        overlay._hwnd = 1
        overlay._closed = False
        overlay._updates = queue.Queue()
        overlay._updates.put({"width": 800, "title": "旧窗口"})
        overlay._last_state = {"width": 800}
        overlay._last_drawn_state = {"width": 800}
        overlay._window_geometry = (0, 0, 800, 600)
        overlay._draw = MagicMock()
        overlay.reset_session()
        overlay.show()
        self.assertTrue(overlay._updates.empty())
        self.assertIsNone(overlay._last_state)
        self.assertIsNone(overlay._last_drawn_state)
        self.assertIsNone(overlay._window_geometry)
        self.assertFalse(overlay._closed)
        overlay._draw.assert_not_called()
        overlay._canvas.delete.assert_called_once_with("all")


if __name__ == "__main__":
    unittest.main()
