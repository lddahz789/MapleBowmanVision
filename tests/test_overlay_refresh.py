from __future__ import annotations

from copy import deepcopy
import threading
import unittest
from unittest.mock import MagicMock, call, patch

from mbv import overlay


class RuntimeOverlayRefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = MagicMock()
        self.exit_root = MagicMock()
        self.canvas = MagicMock()
        self.exit_button = MagicMock()
        for target, kwargs in (
            ("tk.Tk", {"return_value": self.root}),
            ("tk.Toplevel", {"return_value": self.exit_root}),
            ("tk.Canvas", {"return_value": self.canvas}),
            ("tk.Button", {"return_value": self.exit_button}),
            ("_top_level_hwnd", {"side_effect": (123, 456)}),
            ("_exclude_from_capture", {}),
            ("user32.GetWindowLongPtrW", {"return_value": 0}),
            ("user32.SetWindowLongPtrW", {}),
            ("user32.SetWindowPos", {}),
        ):
            patcher = patch(f"mbv.overlay.{target}", **kwargs)
            replacement = patcher.start()
            self.addCleanup(patcher.stop)
            if target == "user32.SetWindowPos":
                self.position = replacement
            elif target == "user32.SetWindowLongPtrW":
                self.styles = replacement
        self.instance = overlay.RuntimeOverlay()
        self.root.reset_mock()
        self.exit_root.reset_mock()
        self.canvas.reset_mock()
        self.exit_button.reset_mock()
        self.state = {
            "left": 10,
            "top": 20,
            "width": 800,
            "height": 600,
            "banner": "输入待命",
            "show_calibration": False,
        }

    def publish(self, state: dict | None = None) -> None:
        self.instance.update(deepcopy(self.state if state is None else state))
        self.instance._poll()

    def test_identical_updates_reuse_canvas_and_layout_without_losing_topmost(self) -> None:
        for _ in range(100):
            self.publish()
        self.canvas.delete.assert_called_once_with("all")
        self.root.geometry.assert_called_once_with("800x600+10+20")
        self.exit_root.geometry.assert_called_once_with("116x34+694+20")
        self.root.deiconify.assert_called_once_with()
        self.exit_root.deiconify.assert_called_once_with()
        # 激活同为 topmost 的游戏后，相同 HUD 状态仍须重新置于游戏上方。
        self.assertEqual(self.position.call_count, 200)
        for positioned in self.position.call_args_list:
            self.assertEqual(positioned.args[1], overlay.HWND_TOPMOST)
            self.assertEqual(
                positioned.args[-1], overlay.SWP_NOACTIVATE | overlay.SWP_SHOWWINDOW
            )

    def test_changed_content_repaints_without_repeating_layout(self) -> None:
        for index in range(100):
            self.publish({**self.state, "notice": f"通知 {index}"})
        self.assertEqual(self.canvas.delete.call_count, 100)
        self.root.geometry.assert_called_once()
        self.exit_root.geometry.assert_called_once()
        self.assertIn(
            "通知 99", [item.kwargs.get("text") for item in self.canvas.create_text.call_args_list]
        )

    def test_move_resize_and_negative_monitor_coordinates_update_both_windows(self) -> None:
        self.publish()
        self.publish({**self.state, "left": -900, "top": -50, "width": 1024, "height": 768})
        self.root.geometry.assert_has_calls([call("800x600+10+20"), call("1024x768-900-50")])
        self.exit_root.geometry.assert_has_calls([call("116x34+694+20"), call("116x34+8-50")])
        self.assertEqual(self.position.call_args_list[-2].args[2:6], (-900, -50, 1024, 768))
        self.assertEqual(self.position.call_args_list[-1].args[2:6], (8, -50, 116, 34))
        self.assertEqual(self.canvas.delete.call_count, 2)

    def test_repeated_background_updates_withdraw_once_then_restore_latest_geometry(self) -> None:
        self.publish()
        hidden = {**self.state, "background_hidden": True, "left": 200}
        for _ in range(100):
            self.publish(hidden)
        self.root.withdraw.assert_called_once()
        self.exit_root.withdraw.assert_called_once()
        self.assertEqual(self.position.call_count, 2)
        self.publish({**hidden, "background_hidden": False})
        self.root.geometry.assert_called_with("800x600+200+20")
        self.assertEqual(self.root.deiconify.call_count, 2)
        self.assertEqual(self.canvas.delete.call_count, 2)

    def test_initial_background_hidden_never_shows_windows(self) -> None:
        for _ in range(10):
            self.publish({"background_hidden": True})
        self.root.deiconify.assert_not_called()
        self.exit_root.deiconify.assert_not_called()
        self.position.assert_not_called()
        self.canvas.delete.assert_not_called()

    def test_capture_hide_consumes_latest_state_without_drawing_then_show_restores_it(self) -> None:
        self.publish()
        self.instance.hide()
        self.publish({**self.state, "notice": "较早通知"})
        latest = {**self.state, "notice": "采集完成", "left": 40}
        self.publish(latest)
        self.assertEqual(self.canvas.delete.call_count, 1)
        self.assertEqual(self.instance._last_state, latest)
        self.instance.show()
        self.assertEqual(self.canvas.delete.call_count, 2)
        self.root.geometry.assert_called_with("800x600+40+20")
        self.assertEqual(self.root.deiconify.call_count, 2)
        self.assertEqual(self.canvas.create_text.call_args.kwargs["text"], "采集完成")

    def test_capture_hide_show_restores_unchanged_canvas_and_exit_window(self) -> None:
        self.publish()
        self.instance.hide()
        self.instance.show()
        self.assertEqual(self.root.deiconify.call_count, 2)
        self.assertEqual(self.exit_root.deiconify.call_count, 2)
        self.canvas.delete.assert_called_once_with("all")

    def test_show_after_capture_respects_background_hidden_state(self) -> None:
        self.publish()
        self.instance.hide()
        self.publish({**self.state, "background_hidden": True})
        self.instance.show()
        self.root.deiconify.assert_called_once()
        self.exit_root.deiconify.assert_called_once()
        self.assertEqual(self.position.call_count, 2)

    def test_mutating_reused_nested_roi_still_repaints(self) -> None:
        state = {
            **self.state,
            "show_calibration": True,
            "hp_roi": {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.1},
        }
        self.instance.update(state)
        self.instance._poll()
        state["hp_roi"]["x"] = 0.5
        self.instance.update(state)
        self.instance._poll()
        self.assertEqual(self.canvas.delete.call_count, 2)
        self.assertEqual(self.canvas.create_rectangle.call_args.args[:4], (400, 120, 640, 180))

    def test_debug_toggle_and_individual_exclusions_repaint(self) -> None:
        self.publish()
        enabled = {**self.state, "show_calibration": True, "player_box": (10, 80, 20, 30)}
        self.publish(enabled)
        self.assertEqual(self.canvas.create_rectangle.call_args.args[:4], (10, 80, 30, 110))
        self.canvas.create_rectangle.reset_mock()
        self.publish({**enabled, "debug_hidden_items": ("player",)})
        self.canvas.create_rectangle.assert_called_once()
        self.assertEqual(self.canvas.create_rectangle.call_args.args[:4], (0, 0, 800, 34))
        self.publish()
        self.assertEqual(self.canvas.create_text.call_args.kwargs["text"], overlay.CALIBRATION_HINT)
        self.assertEqual(self.canvas.delete.call_count, 4)

    def test_worker_only_publishes_latest_state_and_main_thread_paints(self) -> None:
        thread = threading.Thread(
            target=lambda: [self.instance.update({**self.state, "notice": str(n)}) for n in range(10)]
        )
        thread.start()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.root.mock_calls, [])
        self.assertEqual(self.canvas.mock_calls, [])
        self.position.assert_not_called()
        self.instance._poll()
        self.canvas.delete.assert_called_once()
        self.assertEqual(self.canvas.create_text.call_args.kwargs["text"], "9")

    def test_poll_without_new_state_does_not_touch_windows_or_canvas(self) -> None:
        self.publish()
        self.canvas.reset_mock()
        self.position.reset_mock()
        self.root.reset_mock()
        self.instance._poll()
        self.assertEqual(self.canvas.mock_calls, [])
        self.position.assert_not_called()
        self.assertEqual(self.root.mock_calls, [call.after(25, self.instance._poll)])

    def test_close_replaces_pending_update_and_stops_polling(self) -> None:
        self.instance.update(self.state)
        self.instance.close()
        self.instance._poll()
        self.root.destroy.assert_called_once()
        self.root.after.assert_not_called()
        self.canvas.delete.assert_not_called()
        self.assertTrue(self.instance._closed)
        self.instance.show()
        self.root.deiconify.assert_not_called()

    def test_exit_button_stays_disabled_with_progress_label_during_later_updates(self) -> None:
        handler = MagicMock()
        self.instance.set_exit_handler(handler)
        self.publish()
        self.instance._request_exit()
        self.publish({**self.state, "notice": "退出中"})
        handler.assert_called_once()
        self.exit_button.configure.assert_called_once_with(text="正在退出…", state="disabled")

    def test_click_through_hud_and_clickable_exit_styles_are_preserved(self) -> None:
        styles = {item.args[0]: item.args[2] for item in self.styles.call_args_list}
        self.assertTrue(styles[123] & overlay.WS_EX_TRANSPARENT)
        self.assertTrue(styles[123] & overlay.WS_EX_NOACTIVATE)
        self.assertFalse(styles[456] & overlay.WS_EX_TRANSPARENT)
        self.assertFalse(styles[456] & overlay.WS_EX_NOACTIVATE)


if __name__ == "__main__":
    unittest.main()
