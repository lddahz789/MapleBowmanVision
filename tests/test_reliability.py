from __future__ import annotations

import json
import os
from pathlib import Path
import threading
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from mbv.bot import BowmanBot
from mbv.buffs import AutoBuffController
from mbv.config import save_config
from mbv.panel import ControlPanel
from mbv.player_tracking import PlayerTrackState
from mbv.strategies import get_strategy
from mbv.strategies.base import StrategyActionContext
from mbv.vision import Detection, PlayerAnchor, SceneFeatures

ROOT = Path(__file__).resolve().parents[1]


class ReacquisitionTests(unittest.TestCase):
    def test_empty_or_ambiguous_frame_breaks_nameplate_confirmation(self):
        own = Detection((90, 80, 20, 10), .9, "own.png", identity_score=.96)
        other = Detection((150, 80, 20, 10), .9, "other.png", identity_score=.96)
        config = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
        for gap in ([], [own, other]):
            with self.subTest(gap=len(gap)):
                bot = BowmanBot.__new__(BowmanBot)
                bot.player_track = PlayerTrackState()
                bot._detect_player_nameplate = MagicMock(side_effect=[
                    ([own], .9, "own.png"), (gap, .9, None),
                    ([own], .9, "own.png"), ([own], .9, "own.png"),
                ])
                bot._detect_player_auxiliary = MagicMock(return_value=([], -1., [], -1.))
                scene = SceneFeatures(np.zeros((200, 400, 3), dtype=np.uint8))
                results = [bot._track_player(scene, config["vision"], 10 + i * .1) for i in range(4)]
                self.assertEqual(results[:3], [None, None, None])
                self.assertEqual(results[3].raw_box, own.box)

    def test_two_searches_in_one_frame_do_not_count_as_two_frames(self):
        track = PlayerTrackState()
        candidate = PlayerAnchor((90, 80, 20, 1), .9, "姓名板", None)
        track.begin_frame()
        self.assertIsNone(track.consider_reacquisition(candidate, 2, 20, kind="nameplate"))
        self.assertIsNone(track.consider_reacquisition(candidate, 2, 20, kind="nameplate"))
        track.mark_miss()
        track.begin_frame()
        self.assertIsNotNone(track.consider_reacquisition(candidate, 2, 20, kind="nameplate"))

    def test_empty_frame_breaks_auxiliary_confirmation(self):
        track = PlayerTrackState()
        candidate = PlayerAnchor((90, 80, 20, 1), .95, "头部", None)
        for _index in range(2):
            track.begin_frame()
            self.assertIsNone(track.consider_reacquisition(candidate, 3, 20, kind="startup_aux"))
            track.mark_miss()
        track.begin_frame()
        track.mark_miss()
        track.begin_frame()
        self.assertIsNone(track.consider_reacquisition(candidate, 3, 20, kind="startup_aux"))
        self.assertEqual(track.pending_count, 1)


class BuffPreparationTests(unittest.TestCase):
    def setUp(self):
        self.bot = bot = BowmanBot.__new__(BowmanBot)
        bot.armed = bot.input_authorized = True
        bot.auto_buff = AutoBuffController()
        bot.config = {"buffs": {"buff_1": {"enabled": True, "key": "a", "interval_seconds": 10}}}
        bot.keyboard = MagicMock()
        bot.stop_move = MagicMock()
        bot._reset_player_lost_recovery = MagicMock()
        bot.disarm = MagicMock()
        bot.log = MagicMock()
        bot.window = MagicMock(hwnd=123)
        bot.background_input = False
        bot.f8_requested = threading.Event()
        bot.f9_requested = threading.Event()
        bot.vision_suspended = threading.Event()

    def test_stop_requests_during_preparation_cancel_before_keydown(self):
        for name in ("f8_requested", "f9_requested", "vision_suspended"):
            with self.subTest(name=name):
                self.bot.buff_preparation = None
                self.assertTrue(self.bot._try_auto_buff(10))
                event = getattr(self.bot, name)
                event.set()
                self.assertTrue(self.bot._try_auto_buff(11))
                event.clear()
                self.bot.keyboard.tap.assert_not_called()
                self.assertEqual(self.bot.auto_buff.last_cast_at, {})

    def test_focus_loss_before_cast_does_not_send_or_record_buff(self):
        self.bot._try_auto_buff(10)
        with patch("mbv.bot.user32.IsWindow", return_value=True), patch(
            "mbv.bot.user32.IsIconic", return_value=False
        ), patch("mbv.bot.user32.GetForegroundWindow", return_value=999):
            self.bot._try_auto_buff(11)
        self.bot.disarm.assert_called_once()
        self.bot.keyboard.tap.assert_not_called()
        self.assertEqual(self.bot.auto_buff.last_cast_at, {})

    def test_background_buff_can_cast_while_another_window_is_foreground(self):
        self.bot.background_input = True
        self.bot._try_auto_buff(10)
        with patch("mbv.bot.user32.IsWindow", return_value=True), patch(
            "mbv.bot.user32.IsIconic", return_value=False
        ), patch("mbv.bot.user32.GetForegroundWindow", return_value=999), patch(
            "mbv.bot.time.monotonic", return_value=11
        ):
            self.bot._try_auto_buff(11)
        self.bot.keyboard.tap.assert_called_once_with("a", .18)
        self.assertEqual(self.bot.auto_buff.last_cast_at["buff_1"], 11)

    def test_disabling_pending_buff_does_not_cast_or_consume_interval(self):
        self.bot._try_auto_buff(10)
        self.bot.config["buffs"]["buff_1"]["enabled"] = False
        self.assertFalse(self.bot._try_auto_buff(11))
        self.bot.keyboard.tap.assert_not_called()
        self.assertIsNone(self.bot.buff_preparation)
        self.assertEqual(self.bot.auto_buff.last_cast_at, {})

    def test_changed_key_restarts_preparation(self):
        self.bot._try_auto_buff(10)
        self.bot.config["buffs"]["buff_1"]["key"] = "b"
        self.bot._try_auto_buff(11)
        self.assertEqual(self.bot.buff_preparation[0].key, "b")
        self.assertAlmostEqual(self.bot.buff_preparation[1], 11.45)
        self.bot.keyboard.tap.assert_not_called()

    def test_short_first_interval_does_not_starve_other_buffs(self):
        controller = AutoBuffController()
        config = {f"buff_{i}": {"key": "a", "interval_seconds": 1} for i in (1, 2, 3)}
        order = []
        for now in (10, 12, 14, 16, 18, 20):
            action = controller.decide(config, now)
            order.append(action.slot)
            controller.record(action, now)
        self.assertEqual(order, ["buff_1", "buff_2", "buff_3"] * 2)


class PanelErrorTests(unittest.TestCase):
    def test_worker_error_is_delivered_by_main_thread_tick(self):
        panel = ControlPanel.__new__(ControlPanel)
        panel.bot = MagicMock()
        error = ValueError("original error")
        panel.bot.run.side_effect = error
        panel.worker_errors = []
        panel.overlay = MagicMock()
        panel.root = MagicMock()
        panel._worker_failed = MagicMock()
        panel._run_bot()
        panel.root.after.assert_not_called()
        panel._tick()
        panel._worker_failed.assert_called_once_with(error)


class SafeAreaTests(unittest.TestCase):
    def test_safe_area_uses_minimap_y_even_when_screen_y_disagrees(self):
        strategy = get_strategy("throwing_star_safe")
        for marker_y, screen_y, expected in ((.25, 10, "SAFE_PATROL_RIGHT"), (.1, 450, "SAFE_OUTPUT_ABOVE")):
            with self.subTest(marker_y=marker_y):
                context = StrategyActionContext(
                    marker=(.5, marker_y), player_box=(480, screen_y, 40, 1),
                    player_anchor=(500., float(screen_y)), target_box=None, chase_box=None,
                    combat_width=1000, combat_height=500, has_monster_candidates=False,
                    now=10, last_target_seen=9, last_pickup=0, direction="right", behavior={},
                    settings={"use_safe_output_area": True, "patrol_inside_safe_area": True},
                    recognition={"throwing_star_safe_output_area": {
                        "space": "minimap", "x": .4, "y": .2, "w": .2, "h": .1,
                    }},
                )
                self.assertEqual(strategy.decide(context).state, expected)


class AtomicConfigTests(unittest.TestCase):
    def test_success_preserves_previous_config_in_backup(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            save_config(path, {"value": 1})
            save_config(path, {"value": 2})
            self.assertEqual(json.loads(path.read_text()), {"value": 2})
            self.assertEqual(json.loads(path.with_name("config.json.bak").read_text()), {"value": 1})

    def test_failed_final_replace_keeps_original_readable_and_cleans_temporary(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            save_config(path, {"value": 1})
            replace = os.replace

            def fail_final(source, destination):
                if destination == path:
                    raise OSError("simulated failure")
                replace(source, destination)

            with patch("mbv.config.os.replace", side_effect=fail_final), self.assertRaises(OSError):
                save_config(path, {"value": 2})
            self.assertEqual(json.loads(path.read_text()), {"value": 1})
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])

    def test_serialization_failure_does_not_touch_original_or_backup(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            save_config(path, {"value": 1})
            with self.assertRaises(ValueError):
                save_config(path, {"value": float("nan")})
            self.assertEqual(json.loads(path.read_text()), {"value": 1})
            self.assertFalse(path.with_name("config.json.bak").exists())


if __name__ == "__main__":
    unittest.main()
