from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from mbv.bot import BowmanBot
from mbv.config import load_config


class RuntimeTimingTests(unittest.TestCase):
    def test_saved_fps_changes_next_frame_budget_and_monitor_without_restart(self):
        config = load_config(Path(__file__).resolve().parents[1] / "config.example.json")
        config["input"]["delivery"] = "foreground"
        config["capture"]["fps"] = 12
        window = SimpleNamespace(hwnd=123, title="测试窗口", width=400, height=200, left=0, top=0)
        frame = np.zeros((200, 400, 3), dtype=np.uint8)
        with ExitStack() as stack:
            for name, replacement in (
                ("Keyboard", MagicMock()),
                ("SessionLog", MagicMock()),
                ("load_templates", MagicMock(return_value=[])),
                ("find_game_window", MagicMock(return_value=window)),
                ("window_process_path", MagicMock(return_value=(456, "game.exe"))),
                ("process_integrity_level", MagicMock(return_value=0)),
                ("client_window", MagicMock(return_value=window)),
                ("capture_client", MagicMock(return_value=frame)),
                ("BackgroundCapture", MagicMock()),
                ("mss.MSS", MagicMock()),
                ("time.monotonic", MagicMock(return_value=100.0)),
            ):
                stack.enter_context(patch(f"mbv.bot.{name}", replacement))
            sleep = stack.enter_context(patch("mbv.bot.time.sleep"))
            stack.enter_context(patch.object(BowmanBot, "monitor_hotkeys"))
            stack.enter_context(patch.object(BowmanBot, "stop_hotkey_monitor"))
            bot = BowmanBot(config, input_authorized=False)
            bot.performance = MagicMock()
            bot.act = MagicMock()
            bot.disarm = MagicMock()
            overlay = MagicMock()
            next_fps = iter([6, 1])

            def update(_state):
                try:
                    requested_fps = next(next_fps)
                except StopIteration:
                    bot.f9_requested.set()
                    return
                changed = deepcopy(bot.config)
                changed["capture"]["fps"] = requested_fps
                bot.apply_config(changed)

            overlay.update.side_effect = update
            bot.run(overlay)

        self.assertEqual(overlay.update.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1 / 12, 1 / 6, 1 / 2])
        self.assertEqual(
            [call.kwargs["target_fps"] for call in bot.performance.record_frame.call_args_list],
            [12, 6, 2],
        )
        self.assertEqual(
            [call.args[0] for call in bot.performance.update_target_fps.call_args_list],
            [12, 6, 2],
        )


if __name__ == "__main__":
    unittest.main()
