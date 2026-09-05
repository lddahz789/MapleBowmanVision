from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from mbv.foreground_move_probe import restore_original, short_move


class ForegroundProbeTests(unittest.TestCase):
    def test_preflight_failure_never_presses(self):
        keyboard = MagicMock()
        with self.assertRaises(RuntimeError):
            short_move(keyboard, MagicMock(side_effect=RuntimeError("not foreground")))
        keyboard.movement_down.assert_not_called()

    def test_focus_loss_after_down_releases(self):
        keyboard = MagicMock()
        guard = MagicMock(side_effect=[None, RuntimeError("lost focus")])
        with self.assertRaises(RuntimeError):
            short_move(keyboard, guard)
        keyboard.movement_down.assert_called_once_with("right", seconds=.2)
        keyboard.release_all.assert_called_once()

    def test_keydown_failure_still_releases(self):
        keyboard = MagicMock()
        keyboard.movement_down.side_effect = OSError("input failed")
        with self.assertRaises(OSError):
            short_move(keyboard, MagicMock())
        keyboard.release_all.assert_called_once()

    def test_move_is_bounded_and_has_no_capture(self):
        keyboard = MagicMock()
        with patch("mbv.foreground_move_probe.time.monotonic", side_effect=[0, 0, .3]), patch(
            "mbv.foreground_move_probe.time.sleep"
        ):
            short_move(keyboard, MagicMock())
        keyboard.release_all.assert_called_once()
        keyboard.check_health.assert_called_once()

    def test_rejects_long_or_invalid_duration(self):
        for seconds in (1, 0, float("nan")):
            with self.assertRaises(ValueError):
                short_move(MagicMock(), MagicMock(), seconds)

    def test_user_switch_prevents_focus_restore(self):
        emit = MagicMock()
        with patch("mbv.foreground_move_probe.user32.GetForegroundWindow", return_value=999), patch(
            "mbv.foreground_move_probe.bounded_focus"
        ) as focus:
            self.assertFalse(restore_original(123, 42, 456, emit))
            focus.assert_not_called()

    def test_reused_original_handle_prevents_restore(self):
        with patch("mbv.foreground_move_probe.user32.GetForegroundWindow", return_value=456), patch(
            "mbv.foreground_move_probe.user32.IsWindow", return_value=1
        ), patch("mbv.foreground_move_probe.window_process_path", return_value=(99, "other")), patch(
            "mbv.foreground_move_probe.bounded_focus"
        ) as focus:
            self.assertFalse(restore_original(123, 42, 456, MagicMock()))
            focus.assert_not_called()

    def test_restores_only_original_window_after_game(self):
        with patch("mbv.foreground_move_probe.user32.GetForegroundWindow", return_value=456), patch(
            "mbv.foreground_move_probe.user32.IsWindow", return_value=1
        ), patch("mbv.foreground_move_probe.window_process_path", return_value=(42, "original")), patch(
            "mbv.foreground_move_probe.bounded_focus"
        ) as focus:
            self.assertTrue(restore_original(123, 42, 456, MagicMock()))
            focus.assert_called_once_with(123, 42, 456)


if __name__ == "__main__":
    unittest.main()
