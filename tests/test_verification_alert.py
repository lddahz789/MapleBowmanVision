from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

import cv2
import numpy as np

from mbv.bot import BowmanBot
from mbv.config import load_config, save_config
from mbv.panel import ControlPanel
from mbv.verification_alert import (
    DEFAULT_REGION, KEYWORD_MASK, KeywordMatch, VerificationAlert,
    keyword_match, normalize_alert_settings, play_alert_sound,
)


def sample_frame(x=8, y=600, scale=1.0):
    frame = np.zeros((720, 1280, 3), np.uint8)
    mask = cv2.resize(KEYWORD_MASK, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
    frame[y:y + mask.shape[0], x:x + mask.shape[1]][mask != 0] = (170, 170, 255)
    return frame


class KeywordDetectionTests(unittest.TestCase):
    def test_finds_keyword_at_different_chat_rows_and_scales(self):
        for y in (580, 610, 630):
            for scale in (1., .8, 1.25, 1.5, 2.):
                with self.subTest(y=y, scale=scale):
                    result = keyword_match(sample_frame(y=y, scale=scale), DEFAULT_REGION)
                    self.assertIsNotNone(result)
                    self.assertEqual(result.box[:2], (8, y))
                    self.assertGreater(result.score, .99)

    def test_ignores_keyword_outside_chat_and_math_panel(self):
        self.assertIsNone(keyword_match(sample_frame(850, 350), DEFAULT_REGION))
        self.assertIsNone(keyword_match(sample_frame(800, 600), DEFAULT_REGION))

    def test_empty_tiny_and_solid_color_are_not_keyword(self):
        for frame in (np.zeros((0, 0, 3), np.uint8), np.zeros((20, 20, 3), np.uint8),
                      np.full((720, 1280, 3), (170, 170, 255), np.uint8)):
            self.assertIsNone(keyword_match(frame, DEFAULT_REGION))

    def test_partial_keyword_random_text_and_different_color_are_rejected(self):
        frame = sample_frame()
        frame[600:612, 28:48] = 0
        self.assertIsNone(keyword_match(frame, DEFAULT_REGION))
        frame = sample_frame()
        frame[frame[:, :, 2] > 0] = (255, 255, 255)
        self.assertIsNone(keyword_match(frame, DEFAULT_REGION))
        rng = np.random.default_rng(5)
        frame = np.zeros((720, 1280, 3), np.uint8)
        frame[rng.random(frame.shape[:2]) < .15] = (170, 170, 255)
        self.assertIsNone(keyword_match(frame, DEFAULT_REGION))

    def test_custom_region_is_respected(self):
        region = {"x": .5, "y": .6, "w": .5, "h": .3}
        self.assertIsNotNone(keyword_match(sample_frame(800, 600), region))


class AlertDebounceTests(unittest.TestCase):
    def setUp(self):
        self.monitor = VerificationAlert()
        self.settings = {"enabled": True}
        self.frame = np.zeros((1, 1, 3), np.uint8)
        self.hit = KeywordMatch(.98, (8, 600, 60, 12))
        self.patcher = patch("mbv.verification_alert.keyword_match", return_value=self.hit)
        self.matcher = self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def scan(self, now):
        return self.monitor.observe(self.frame, self.settings, now)

    def test_three_separate_scans_and_at_most_two_scans_per_second(self):
        self.assertIsNone(self.scan(0.))
        self.assertIsNone(self.scan(.1))
        self.assertIsNone(self.scan(.2))
        self.assertEqual(self.matcher.call_count, 1)
        self.assertIsNone(self.scan(.5))
        self.assertEqual(self.scan(1.), self.hit)

    def test_single_hit_is_not_enough_and_miss_resets(self):
        self.scan(0.)
        self.matcher.return_value = None
        self.scan(.5)
        self.matcher.return_value = self.hit
        self.assertIsNone(self.scan(1.))
        self.assertIsNone(self.scan(1.5))
        self.assertEqual(self.scan(2.), self.hit)

    def test_old_message_does_not_realert_even_after_minutes_or_row_change(self):
        results = [self.scan(t / 2) for t in range(800)]
        self.assertEqual(sum(r is not None for r in results), 1)
        self.matcher.return_value = KeywordMatch(.98, (8, 590, 60, 12))
        self.assertIsNone(self.scan(400.))

    def test_rearm_requires_continuous_absence_and_cooldown(self):
        for t in (0., .5, 1.):
            self.scan(t)
        self.matcher.return_value = None
        for t in range(2, 13):
            self.scan(float(t))
        self.assertFalse(self.monitor.latched)
        self.matcher.return_value = self.hit
        self.assertIsNone(self.scan(13.))
        self.assertIsNone(self.scan(13.5))
        self.assertIsNone(self.scan(14.))
        for t in range(15, 61):
            self.assertIsNone(self.scan(float(t)))
        self.assertEqual(self.scan(61.), self.hit)

    def test_capture_gap_cannot_confirm_hits_or_clear_old_message(self):
        self.scan(0.)
        self.scan(.5)
        self.assertIsNone(self.scan(100.))
        self.assertIsNone(self.scan(100.5))
        self.assertEqual(self.scan(101.), self.hit)
        self.matcher.return_value = None
        self.scan(102.)
        self.scan(200.)
        self.assertTrue(self.monitor.latched)

    def test_disable_prevents_scanning_and_can_reset_failed_monitor(self):
        self.settings["enabled"] = False
        self.scan(0.)
        self.matcher.assert_not_called()
        self.settings["enabled"] = True
        self.scan(1.)
        self.monitor.failed = True
        self.scan(2.)
        self.assertEqual(self.matcher.call_count, 1)
        self.settings["enabled"] = False
        self.scan(3.)
        self.settings["enabled"] = True
        self.scan(4.)
        self.assertFalse(self.monitor.failed)
        self.assertEqual(self.matcher.call_count, 2)


class AlertIntegrationTests(unittest.TestCase):
    def make_bot(self, armed=True):
        bot = BowmanBot.__new__(BowmanBot)
        bot.config = {"profile": "newmaple", "verification_alert": {"enabled": True}}
        bot.verification_alert = VerificationAlert()
        bot.verification_alert_status = ""
        bot.log = MagicMock()
        bot.keyboard = MagicMock()
        bot.disarm = MagicMock()
        bot.stop_move = MagicMock()
        bot.armed = armed
        bot.state = "ATTACK_LEFT"
        bot.last_attack = 100.
        bot.strategy_runtime_state = {"phase": "returning"}
        return bot

    def test_alert_does_not_change_armed_state_strategy_attack_or_keys(self):
        for armed in (True, False):
            bot = self.make_bot(armed)
            before = deepcopy((bot.armed, bot.state, bot.last_attack, bot.strategy_runtime_state, bot.config))
            with patch("mbv.verification_alert.play_alert_sound") as sound:
                for now in (0., .5, 1., 1.5):
                    bot._observe_verification_alert(sample_frame(), now)
                sound.assert_called_once_with()
            self.assertEqual(before, (bot.armed, bot.state, bot.last_attack, bot.strategy_runtime_state, bot.config))
            self.assertEqual(bot.keyboard.mock_calls, [])
            bot.disarm.assert_not_called()
            bot.stop_move.assert_not_called()
            self.assertEqual(bot.log.write.call_args.kwargs["action"], "sound_only")

    def test_recognition_sound_and_log_errors_do_not_pause_or_escape(self):
        for stage in ("recognition", "sound", "log"):
            with self.subTest(stage=stage):
                bot = self.make_bot()
                with patch("mbv.verification_alert.play_alert_sound") as sound, \
                     patch("mbv.verification_alert.keyword_match", return_value=KeywordMatch(1., (8, 600, 60, 12))) as match:
                    if stage == "recognition":
                        match.side_effect = ValueError("bad frame")
                    elif stage == "sound":
                        sound.side_effect = RuntimeError("audio failed")
                    else:
                        bot.log.write.side_effect = OSError("disk full")
                    for now in (0., .5, 1., 1.5):
                        bot._observe_verification_alert(sample_frame(), now)
                self.assertTrue(bot.armed)
                self.assertEqual(bot.verification_alert.failed, stage != "log")
                self.assertIn("不可用", bot.verification_alert_status)
                self.assertEqual(bot.keyboard.mock_calls, [])
                bot.disarm.assert_not_called()

    def test_classic_profile_never_uses_newmaple_template(self):
        bot = self.make_bot()
        bot.config["profile"] = "classic"
        with patch("mbv.verification_alert.keyword_match") as match:
            for now in (0., .5, 1.):
                bot._observe_verification_alert(sample_frame(), now)
            match.assert_not_called()

    def test_sound_is_async_and_test_button_only_plays_sound(self):
        import winsound
        with patch("winsound.PlaySound") as play:
            play_alert_sound()
            self.assertTrue(play.call_args.args[1] & winsound.SND_ASYNC)
        panel = ControlPanel.__new__(ControlPanel)
        panel.bot = MagicMock()
        with patch("mbv.verification_alert.play_alert_sound") as sound:
            panel._test_verification_sound()
            sound.assert_called_once_with()
        self.assertEqual(panel.bot.mock_calls, [])

    def test_toggle_saves_only_alert_setting_without_apply_config_or_disarm(self):
        panel = ControlPanel.__new__(ControlPanel)
        panel.verification_alert_enabled = MagicMock()
        panel.verification_alert_enabled.get.return_value = True
        panel._preview_common_setting = MagicMock()
        panel.bot = MagicMock()
        panel._toggle_verification_alert()
        panel._preview_common_setting.assert_called_once_with("verification_alert.enabled", True)
        self.assertEqual(panel.bot.mock_calls, [])


class AlertConfigTests(unittest.TestCase):
    def test_old_configs_default_off_and_changes_roundtrip(self):
        root = Path(__file__).resolve().parents[1]
        for example in (root / "config.example.json", root / "profiles/newmaple/config.example.json"):
            config = load_config(example)
            self.assertFalse(config["verification_alert"]["enabled"])
            config.pop("verification_alert")
            with TemporaryDirectory() as tmp:
                path = Path(tmp) / "config.json"
                save_config(path, config)
                loaded = load_config(path)
                self.assertFalse(loaded["verification_alert"]["enabled"])
                loaded["verification_alert"]["enabled"] = True
                save_config(path, loaded)
                self.assertTrue(load_config(path)["verification_alert"]["enabled"])

    def test_malformed_settings_are_safe_and_dont_modify_caller(self):
        for raw in (None, [], "true", {"enabled": "false"},
                    {"chat_region": {"x": float("nan"), "y": 0, "w": 1, "h": 1}},
                    {"chat_region": {"x": .8, "y": 0, "w": .4, "h": 1}}):
            settings = normalize_alert_settings(raw)
            self.assertFalse(settings["enabled"])
            self.assertEqual(settings["chat_region"], DEFAULT_REGION)


if __name__ == "__main__":
    unittest.main()
