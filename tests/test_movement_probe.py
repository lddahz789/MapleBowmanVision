from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from mbv.movement_probe import VARIANTS, Variant, message_param, send_message, send_trial
from mbv.win32 import WM_KEYDOWN, WM_KEYUP


class ProbeTests(unittest.TestCase):
    def test_matrix_covers_transport_and_parameter_variants(self):
        self.assertEqual(len(VARIANTS), 8)
        self.assertEqual({v.transport for v in VARIANTS}, {"post", "sync"})
        self.assertEqual({v.parameter for v in VARIANTS}, {"standard", "nonextended", "zero"})

    def test_extended_repeat_and_release_bits(self):
        variant = Variant("post", "standard")
        self.assertTrue(message_param(37, False, False, variant) & (1 << 24))
        self.assertTrue(message_param(37, False, True, variant) & (1 << 30))
        self.assertEqual(message_param(37, True, True, variant) >> 30, 3)
        self.assertFalse(message_param(37, False, True, Variant("post", "standard", True)) & (1 << 30))
        self.assertFalse(message_param(37, False, False, Variant("sync", "nonextended")) & (1 << 24))
        self.assertEqual(message_param(37, False, False, Variant("sync", "zero")), 0)

    def test_sync_only_targets_requested_hwnd_and_has_timeout(self):
        with patch("mbv.movement_probe.user32.SendMessageTimeoutW", return_value=1) as sync, patch(
            "mbv.movement_probe.user32.PostMessageW"
        ) as post, patch("mbv.movement_probe.user32.SendInput") as global_input:
            send_message(123, 37, False, False, Variant("sync", "standard"))
            self.assertEqual(sync.call_args.args[:3], (123, WM_KEYDOWN, 37))
            self.assertEqual(sync.call_args.args[5], 40)
            post.assert_not_called()
            global_input.assert_not_called()

    def test_timeout_is_reported_not_treated_as_success(self):
        with patch("mbv.movement_probe.user32.SendMessageTimeoutW", return_value=0):
            with self.assertRaises(OSError):
                send_message(123, 37, False, False, Variant("sync", "standard"))

    def test_guard_interrupt_still_releases_key(self):
        guard = MagicMock(side_effect=[None, None, RuntimeError("foreground")])
        with patch("mbv.movement_probe.time.sleep"), patch(
            "mbv.movement_probe.send_message"
        ) as send:
            with self.assertRaisesRegex(RuntimeError, "foreground"):
                send_trial(123, "left", Variant("sync", "standard"), guard)
            self.assertEqual(send.call_args_list[0].args[2], False)
            self.assertEqual(send.call_args_list[-1].args[2], True)

    def test_preflight_guard_failure_sends_nothing(self):
        with patch("mbv.movement_probe.send_message") as send:
            with self.assertRaises(RuntimeError):
                send_trial(123, "left", Variant("post", "standard"),
                           MagicMock(side_effect=RuntimeError("not ready")))
            send.assert_not_called()

    def test_failed_release_posts_standard_keyup_without_global_input(self):
        guard = MagicMock()
        with patch("mbv.movement_probe.send_message", side_effect=OSError("timeout")), patch(
            "mbv.movement_probe.user32.PostMessageW", return_value=1
        ) as post, patch("mbv.movement_probe.user32.SendInput") as global_input:
            with self.assertRaises(OSError):
                send_trial(123, "right", Variant("sync", "zero"), guard)
            self.assertEqual(post.call_args.args[:3], (123, WM_KEYUP, 39))
            self.assertEqual(post.call_args.args[3] >> 30, 3)
            global_input.assert_not_called()

    def test_rejects_long_or_non_directional_trials(self):
        for direction, seconds in (("a", .3), ("left", 10), ("left", float("nan"))):
            with self.assertRaises(ValueError):
                send_trial(123, direction, VARIANTS[0], MagicMock(), seconds)

    def test_changed_window_owner_never_receives_message(self):
        with patch("mbv.movement_probe.window_process_path", return_value=(999, "other")), patch(
            "mbv.movement_probe.user32.PostMessageW"
        ) as post:
            with self.assertRaises(OSError):
                send_message(123, 37, False, False, VARIANTS[0], expected_pid=111)
            post.assert_not_called()
