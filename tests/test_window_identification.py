import unittest
from unittest.mock import patch

from mbv.window import WindowTarget, identify_window, window_choice_labels


class WindowIdentificationTests(unittest.TestCase):
    def test_names_only_and_same_title_numbering_independent_of_order(self):
        first = WindowTarget(101, 10, "NewMaple", "game.exe")
        second = WindowTarget(102, 20, "NewMaple", "game.exe")
        unique = WindowTarget(103, 30, "其他窗口", "other.exe")
        expected = {"NewMaple（窗口 1）": first, "NewMaple（窗口 2）": second, "其他窗口": unique}
        self.assertEqual(window_choice_labels([first, second, unique]), expected)
        self.assertEqual(window_choice_labels([unique, second, first]), expected)

    def test_real_title_cannot_collide_with_generated_label(self):
        targets = [WindowTarget(101, 10, "A", ""), WindowTarget(102, 20, "A", ""),
                   WindowTarget(103, 30, "A（窗口 1）", "")]
        self.assertEqual(len(window_choice_labels(targets)), 3)

    def test_identification_flashes_exact_window_without_focus_or_keys(self):
        target = WindowTarget(101, 10, "A", "")
        with patch("mbv.window.validate_window_target") as validate, patch("mbv.window.user32") as api:
            identify_window(target)
        validate.assert_called_once_with(target)
        info = api.FlashWindowEx.call_args.args[0]._obj
        self.assertEqual((info.hwnd, info.dwFlags, info.uCount), (101, 3, 6))
        self.assertEqual(len(api.mock_calls), 1)

    def test_invalid_window_never_flashes(self):
        with patch("mbv.window.validate_window_target", side_effect=RuntimeError("已关闭")), \
             patch("mbv.window.user32") as api:
            with self.assertRaises(RuntimeError):
                identify_window(WindowTarget(101, 10, "A", ""))
        api.FlashWindowEx.assert_not_called()
