"""真实视觉帧循环中的龙咆哮计数；所有窗口、截图和输入均隔离为 mock。"""
from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from mbv.bot import BowmanBot
from mbv.config import load_config
from mbv.strategies.warrior import DragonRoarStrategy
from mbv.vision import Detection, PlayerAnchor, PLAYER_RELATIVE_REGION_SPACE
from mbv.window import WindowInfo, WindowTarget


class DragonRoarFrameFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name in ("Keyboard", "SessionLog", "BackgroundCapture", "mss.MSS"):
            self.stack.enter_context(patch(f"mbv.bot.{name}"))
        self.stack.enter_context(patch("mbv.bot.load_templates", return_value=[]))
        self.stack.enter_context(patch("mbv.bot.process_integrity_level", return_value=0))
        self.stack.enter_context(patch("mbv.bot.window_process_path", return_value=(456, "game.exe")))
        self.stack.enter_context(patch.object(BowmanBot, "monitor_hotkeys"))
        self.stack.enter_context(patch.object(BowmanBot, "stop_hotkey_monitor"))
        self.stack.enter_context(patch.object(BowmanBot, "_observe_verification_alert"))
        self.stack.enter_context(patch("builtins.print"))
        config = load_config(Path(__file__).resolve().parents[1] / "config.example.json")
        config["input"]["delivery"] = "foreground"
        config["strategy"]["active"] = "dragon_roar"
        config["strategy"]["options"]["dragon_roar"]["attack_regions"] = [{
            "id": "test", "name": "隔离范围", "space": PLAYER_RELATIVE_REGION_SPACE,
            "offset_x": -0.5, "offset_y": -0.5, "w": 1.0, "h": 1.0,
            "enabled": True, "priority": 1,
        }]
        config["regions"]["combat"] = {"x": 0, "y": 0, "w": 1, "h": 1}
        config["vision"]["monster_hold_seconds"] = 1.0
        self.bot = BowmanBot(config, input_authorized=False)
        # 不触及全局注册实例；真实 select_targets 仍参与每帧决策。
        self.bot.strategy = DragonRoarStrategy()
        self.bot.act = MagicMock()
        self.bot._track_player = MagicMock(return_value=PlayerAnchor(
            (190, 150, 20, 1), 0.95, "姓名板", (190, 155, 20, 10),
        ))
        self.window = WindowInfo(123, "隔离视觉测试", 0, 0, 400, 300, pid=456)
        self.target = WindowTarget(123, 456, "隔离视觉测试", "game.exe")
        self.monsters = [Detection((100 + i * 60, 140, 20, 20), 0.95, "slime/monster.png")
                         for i in range(3)]
        self.observed = []

    def run_frames(self, frame_detections, frame_filters=None):
        frame = np.zeros((300, 400, 3), dtype=np.uint8)
        overlay = MagicMock()
        original_selection = self.bot.strategy.select_targets

        def select(context):
            result = original_selection(context)
            self.observed.append((context, result))
            return result

        self.bot.strategy.select_targets = select
        frame_index = -1
        detections_calls = []

        def detect(scene, templates, *_args, **kwargs):
            nonlocal frame_index
            is_filter = kwargs.get("max_detections") == 32
            if is_filter:
                detections = frame_filters[frame_index]
            else:
                frame_index += 1
                detections = frame_detections[frame_index]
            detections_calls.append((frame_index, is_filter, scene))
            return (list(detections), detections[0].score, detections[0].name) if detections else (
                [], -1.0, None,
            )

        def update(state):
            if "width" in state and len(self.observed) == len(frame_detections):
                self.bot.f9_requested.set()

        overlay.update.side_effect = update
        if frame_filters is not None:
            self.bot.monster_filter_templates = [SimpleNamespace(name="slime/filter.png")]
        with patch("mbv.bot.resolve_window_target", return_value=self.window), \
             patch("mbv.bot.validate_window_target"), \
             patch("mbv.bot.client_window", return_value=self.window), \
             patch("mbv.bot.capture_client", side_effect=[frame] * len(frame_detections)) as capture, \
             patch("mbv.bot.find_game_window") as find_window, \
             patch("mbv.bot.find_detections", side_effect=detect), \
             patch("mbv.bot.player_marker_observation", return_value=(
                 SimpleNamespace(point=(0.5, 0.5), unambiguous=True), None)), \
             patch("mbv.bot.time.monotonic", return_value=100.0), \
             patch("mbv.bot.time.sleep"):
            self.bot.run(overlay, target=self.target, close_overlay_on_exit=False)
        self.assertEqual(capture.call_count, len(frame_detections))
        self.assertEqual(self.bot.act.call_count, len(frame_detections))
        find_window.assert_not_called()
        self.bot.keyboard.tap.assert_not_called()
        self.bot.keyboard.down.assert_not_called()
        self.bot.keyboard.movement_down.assert_not_called()
        return detections_calls

    def test_actual_loop_distinguishes_held_empty_frame_from_new_detections(self):
        new_monster = Detection((280, 140, 20, 20), 0.96, "slime/monster.png")
        self.run_frames([self.monsters, [], [new_monster]])
        contexts = [item[0] for item in self.observed]
        selections = [item[1] for item in self.observed]
        self.assertEqual([context.detections_fresh for context in contexts], [True, False, True])
        # 第二帧确实进入了旧 hold 分支，不是候选先被清空造成的偶然通过。
        self.assertEqual(contexts[1].detections, self.monsters)
        self.assertEqual([result.eligible_candidate_count for result in selections], [3, 0, 1])
        self.assertEqual(selections[1].eligible_detections, ())
        self.assertIsNone(selections[1].target)
        self.assertEqual(selections[2].eligible_detections, (new_monster,))
        action_calls = self.bot.act.call_args_list
        self.assertEqual(action_calls[1].kwargs["eligible_detections"], ())
        self.assertFalse(action_calls[1].args[8])  # has_strategy_candidates

    def test_all_filtered_current_detections_are_not_fresh_or_eligible(self):
        filters = [Detection(item.box, 0.97, "slime/filter.png") for item in self.monsters]
        calls = self.run_frames([self.monsters, self.monsters], [[], filters])
        first_context, first_selection = self.observed[0]
        second_context, second_selection = self.observed[1]
        self.assertTrue(first_context.detections_fresh)
        self.assertEqual(first_selection.eligible_candidate_count, 3)
        self.assertFalse(second_context.detections_fresh)
        self.assertEqual(second_context.detections, [])
        self.assertEqual(second_selection.eligible_candidate_count, 0)
        self.assertEqual(second_selection.eligible_detections, ())
        self.assertEqual(self.bot.act.call_args_list[1].kwargs["eligible_detections"], ())
        # 怪物与过滤项检测始终复用每帧同一份 SceneFeatures。
        for index in range(2):
            scenes = [scene for call_index, _is_filter, scene in calls if call_index == index]
            self.assertEqual(len(scenes), 2)
            self.assertIs(scenes[0], scenes[1])


if __name__ == "__main__":
    unittest.main()
