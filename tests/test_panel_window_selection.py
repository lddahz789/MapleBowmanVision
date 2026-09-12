from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from mbv.config import load_config
from mbv.input import input_delivery
from mbv.panel import ControlPanel, DELIVERY_LABELS
from mbv.window import WindowInfo, WindowTarget, find_game_window


ROOT = Path(__file__).resolve().parents[1]


class PanelWindowSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config_path = Path(self.temporary.name) / "config.json"
        self.config_path.write_text(
            (ROOT / "config.example.json").read_text(encoding="utf-8"), encoding="utf-8"
        )
        self.target = WindowTarget(101, 202, "同名游戏", r"E:\游戏\NewMaple.exe", 200)

    def panel(self, connected: bool = False) -> ControlPanel:
        panel = ControlPanel.__new__(ControlPanel)
        panel.config_path = self.config_path
        panel.root = MagicMock()
        panel.bot = MagicMock()
        panel.bot.armed = False
        panel.bot.auto_potion.enabled = False
        panel.bot.calibration_overlay_visible = False
        panel.bot.calibration_overlay_hidden_items = frozenset({"minimap"})
        panel.overlay = MagicMock()
        panel.worker = MagicMock() if connected else None
        if connected:
            panel.worker.is_alive.return_value = True
        panel.worker_errors = []
        panel._selected_target = self.target if connected else None
        panel._pending_target = None
        panel._stopping_session = False
        panel._closing = False
        panel._input_authorized = False
        panel._connection_message = "等待选择"
        panel._window_lookup = {"1. 同名游戏": self.target}
        panel.window_choice = MagicMock()
        panel.window_choice.get.return_value = "1. 同名游戏"
        panel.window_combo = MagicMock()
        panel.window_hint = MagicMock()
        panel.window_refresh_button = MagicMock()
        panel.window_connect_button = MagicMock()
        panel.window_disconnect_button = MagicMock()
        panel.status = MagicMock()
        panel.auto_potion_enabled = MagicMock()
        panel._refresh_counts = MagicMock()
        panel._load_entries = MagicMock()
        panel._persist_settings = MagicMock(return_value=True)
        panel.busy = False
        return panel

    def test_real_panel_opens_without_window_and_saves_existing_defaults(self) -> None:
        import tkinter as tk

        hidden_root = tk.Tk()
        hidden_root.withdraw()
        with (patch("mbv.panel.tk.Tk", return_value=hidden_root),
              patch("mbv.panel.configure_app_identity", return_value=True),
              patch("mbv.panel.RuntimeOverlay"),
              patch("mbv.panel._top_level_hwnd", return_value=0),
              patch("mbv.panel._exclude_from_capture"),
              patch("mbv.panel.prevent_window_activate"),
              patch("mbv.panel.window_candidates", return_value=[]),
              patch("mbv.bot.Keyboard"),
              patch("mbv.bot.SessionLog"),
              patch("mbv.bot.load_templates", return_value=[]),
              patch("mbv.bot.BowmanBot.run") as run,
              patch("mbv.panel.messagebox.showerror") as error):
            panel = ControlPanel(self.config_path, enable_input=False)
            try:
                self.assertEqual(panel.root.state(), "withdrawn")
                self.assertIsNone(panel.worker)
                self.assertIsNone(panel._selected_target)
                self.assertIn("尚未找到", panel.window_hint.get())
                self.assertEqual(panel.delivery.get(), DELIVERY_LABELS[input_delivery(load_config(self.config_path))])
                panel.hp_threshold_percent.set(47)
                self.assertTrue(panel._persist_settings(
                    apply_runtime=False, notify=False, show_error=False
                ))
                self.assertEqual(load_config(self.config_path)["behavior"]["hp_threshold"], 0.47)
                panel._tick()
                self.assertEqual(str(panel.arm_button["state"]), "disabled")
                self.assertEqual(str(panel.potion_button["state"]), "disabled")
                with patch.object(panel.bot, "request_exit"):
                    panel.quit()
                self.assertTrue(panel._closing)
                run.assert_not_called()
                error.assert_not_called()
            finally:
                for after_id in panel.root.tk.splitlist(panel.root.tk.call("after", "info")):
                    panel.root.tk.call("after", "cancel", after_id)
                panel.root.destroy()

    def test_refresh_prefers_previous_config_without_connecting(self) -> None:
        panel = self.panel()
        panel._window_lookup = {}
        unmatched = WindowTarget(303, 404, "其他窗口", "other.exe", 0)
        with patch("mbv.panel.window_candidates", return_value=[self.target, unmatched]):
            panel._refresh_windows()
        self.assertIn("同名游戏", panel.window_choice.set.call_args.args[0])
        self.assertIsNone(panel.worker)
        panel.bot.run.assert_not_called()

    def test_same_window_with_changed_preference_score_does_not_restart(self) -> None:
        panel = self.panel(connected=True)
        reranked = WindowTarget(self.target.hwnd, self.target.pid, self.target.title,
                                self.target.process_path, self.target.score + 1000)
        panel._window_lookup["1. 同名游戏"] = reranked
        with (patch("mbv.panel.resolve_window_target"),
              patch("mbv.panel.BowmanBot") as new_bot):
            panel._connect_window()
        new_bot.assert_not_called()
        self.assertFalse(panel._stopping_session)
        panel.bot.request_exit.assert_not_called()

    def test_unbound_capture_arm_and_potion_do_not_run(self) -> None:
        panel = self.panel()
        action = MagicMock()
        panel._run_tool("姓名板采集", action)
        panel._toggle_arm()
        panel._toggle_auto_potion()
        action.assert_not_called()
        panel.bot.request_toggle.assert_not_called()
        panel.bot.request_auto_potion.assert_not_called()
        panel.auto_potion_enabled.set.assert_called_with(False)

    def test_calibration_scope_uses_exact_target_after_reloading_config(self) -> None:
        panel = self.panel(connected=True)
        selected_info = WindowInfo(101, "同名游戏", 0, 0, 1024, 768)
        seen = []
        with (patch("mbv.panel.validate_window_target"),
              patch("mbv.window.resolve_window_target", return_value=selected_info) as resolve,
              patch("mbv.window.visible_windows") as enumerate_windows):
            panel._run_tool("校准", lambda: seen.append(find_game_window(load_config(self.config_path))))
        self.assertEqual(seen, [selected_info])
        resolve.assert_called_once_with(self.target)
        enumerate_windows.assert_not_called()
        panel.bot.resume_vision.assert_called_once()

    def test_offline_template_management_does_not_require_target(self) -> None:
        panel = self.panel()
        action = MagicMock()
        panel._run_tool("模板分类", action, requires_window=False)
        action.assert_called_once()
        panel.bot.reload_from_disk.assert_called_once_with(self.config_path)
        panel.overlay.show.assert_not_called()

    def test_new_connection_preserves_profile_calibration_and_only_saves_preferences(self) -> None:
        panel = self.panel()
        config = load_config(self.config_path)
        config["calibrated"] = True
        for item in config["calibration"]["items"].values():
            item["complete"] = True
        config["custom_local_setting"] = {"retain": 123}
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        new_bot = MagicMock()
        thread = MagicMock()
        with (patch("mbv.panel.resolve_window_target"),
              patch("mbv.panel.BowmanBot", return_value=new_bot) as bot_factory,
              patch("mbv.panel.threading.Thread", return_value=thread)):
            panel._connect_window()
        saved = load_config(self.config_path)
        self.assertEqual(saved["profile"], config["profile"])
        self.assertTrue(saved["calibrated"])
        self.assertEqual(saved["custom_local_setting"], {"retain": 123})
        self.assertEqual(saved["window"]["exact_titles"], config["window"]["exact_titles"])
        self.assertEqual(saved["window"]["preferred_title"], self.target.title)
        self.assertEqual(saved["window"]["preferred_executable"], "NewMaple.exe")
        self.assertNotIn("hwnd", saved["window"])
        self.assertNotIn("pid", saved["window"])
        self.assertEqual(new_bot.calibration_overlay_hidden_items, frozenset({"minimap"}))
        self.assertFalse(new_bot.calibration_overlay_visible)
        bot_factory.assert_called_once()
        thread.start.assert_called_once()
        panel.overlay.reset_session.assert_called_once()
        panel._run_bot()
        new_bot.run.assert_called_once_with(panel.overlay, target=self.target, close_overlay_on_exit=False)

    def test_reconnect_waits_for_old_worker_cleanup_before_new_bot(self) -> None:
        panel = self.panel(connected=True)
        old_bot, old_worker = panel.bot, panel.worker
        target2 = WindowTarget(303, 404, "新窗口", "other.exe")
        panel._window_lookup["1. 同名游戏"] = target2
        order = []
        old_bot.f9_requested.set.side_effect = lambda: order.append("cancel")
        old_bot.suspend_vision.side_effect = lambda: order.append("release")
        panel.overlay.reset_session.side_effect = lambda: order.append("clear_hud")

        def create_bot(*args, **kwargs):
            self.assertFalse(old_worker.is_alive())
            self.assertIn("release", order)
            order.append("new_bot")
            return MagicMock()

        with (patch("mbv.panel.resolve_window_target"),
              patch("mbv.panel.BowmanBot", side_effect=create_bot) as create,
              patch("mbv.panel.threading.Thread", return_value=MagicMock())):
            panel._connect_window()
            create.assert_not_called()
            self.assertTrue(panel._stopping_session)
            old_bot.auto_potion.set_enabled.assert_called_once_with(False)
            old_bot.request_exit.assert_called_once()
            panel._consume_worker_completion()
            create.assert_not_called()
            old_worker.is_alive.return_value = False
            panel._consume_worker_completion()
            create.assert_called_once()
        self.assertLess(order.index("cancel"), order.index("release"))
        self.assertLess(order.index("clear_hud"), order.index("new_bot"))
        self.assertEqual(panel._selected_target, target2)

    def test_disconnect_stays_open_and_clears_selected_target(self) -> None:
        panel = self.panel(connected=True)
        panel._disconnect_window()
        panel.worker.is_alive.return_value = False
        self.assertTrue(panel._consume_worker_completion())
        self.assertIsNone(panel._selected_target)
        self.assertIsNone(panel.worker)
        self.assertFalse(panel._closing)
        panel.overlay.close.assert_not_called()

    def test_worker_error_is_reported_without_closing_panel(self) -> None:
        panel = self.panel(connected=True)
        panel.worker.is_alive.return_value = False
        error = ValueError("窗口已关闭")
        panel.bot.run.side_effect = error
        panel._run_bot()
        panel.root.after.assert_not_called()
        self.assertTrue(panel._consume_worker_completion())
        self.assertIsNone(panel._selected_target)
        self.assertFalse(panel.worker_errors)
        self.assertIn("窗口已关闭", panel.window_hint.set.call_args.args[0])
        panel.overlay.close.assert_not_called()
        panel.root.destroy.assert_not_called()

    def test_failed_cleanup_does_not_start_pending_target(self) -> None:
        panel = self.panel(connected=True)
        panel.bot.suspend_vision.side_effect = RuntimeError("抬键失败")
        panel._stop_session(WindowTarget(303, 404, "新窗口", "other.exe"))
        panel.worker.is_alive.return_value = False
        with patch("mbv.panel.BowmanBot") as new_bot:
            panel._consume_worker_completion()
        new_bot.assert_not_called()
        self.assertIsNone(panel._selected_target)
        self.assertIn("抬键失败", panel.window_hint.set.call_args.args[0])

    def test_new_session_cannot_discard_old_unreleased_keyboard(self) -> None:
        panel = self.panel()
        old_bot = panel.bot
        old_bot.keyboard.release_all.side_effect = OSError("旧按键尚未释放")
        with (patch("mbv.panel.resolve_window_target"),
              patch("mbv.panel.BowmanBot") as new_bot):
            panel._start_session(self.target)
            panel._start_session(self.target)
        new_bot.assert_not_called()
        self.assertIs(panel.bot, old_bot)
        self.assertIsNone(panel._selected_target)
        self.assertIsNone(panel.worker)
        self.assertEqual(old_bot.keyboard.release_all.call_count, 2)
        self.assertIn("旧按键尚未释放", panel.window_hint.set.call_args.args[0])

    def test_user_f9_normal_completion_still_quits_panel(self) -> None:
        panel = self.panel(connected=True)
        panel.worker.is_alive.return_value = False
        panel._cancel_performance_refresh = MagicMock()
        self.assertFalse(panel._consume_worker_completion())
        self.assertTrue(panel._closing)
        panel.overlay.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
