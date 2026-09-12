"""隔离的 UI 预览：只使用示例配置，不枚举游戏、不截游戏、不发送按键。"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mbv.config import load_config, save_config
from mbv.panel import ControlPanel


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("classic", "newmaple"), default="newmaple")
    parser.add_argument("--size", default="680x900")
    args = parser.parse_args()
    os.environ["MBV_QA_CAPTURE"] = "1"
    example = ROOT / ("profiles/newmaple/config.example.json" if args.profile == "newmaple" else "config.example.json")
    with TemporaryDirectory(prefix="mbv-ui-preview-") as directory, ExitStack() as stack:
        config_path = Path(directory) / "config.json"
        save_config(config_path, load_config(example))
        stack.enter_context(patch("mbv.panel.window_candidates", return_value=[]))
        stack.enter_context(patch("mbv.bot.Keyboard"))
        stack.enter_context(patch("mbv.bot.load_templates", return_value=[]))
        stack.enter_context(patch("mbv.bot.SessionLog"))
        stack.enter_context(patch("mbv.bot.BowmanBot.run", side_effect=RuntimeError("隔离预览不允许连接游戏")))
        panel = ControlPanel(config_path, enable_input=False)
        panel.root.title("MapleBowmanVision · UI 预览（隔离）")
        panel.root.geometry(args.size + "+80+80")
        panel.mainloop()


if __name__ == "__main__":
    main()
