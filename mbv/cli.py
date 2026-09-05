from __future__ import annotations

import argparse
from datetime import datetime
import os
from pathlib import Path
import sys
import threading

from mbv.bot import BowmanBot
from mbv.calibrate import (
    calibrate,
    capture_monster_filter,
    capture_player_aux_template,
    capture_player_template,
    capture_recognition_region,
    capture_template,
)
from mbv.config import create_config_from_example, load_config
from mbv.overlay import RuntimeOverlay
from mbv.paths import (
    CLASSIC_PROFILE,
    LOG_DIR,
    PROFILE_KEYS,
    profile_paths,
    profile_paths_from_config_path,
)
from mbv.window import visible_windows
from mbv.win32 import user32


def list_windows() -> None:
    for hwnd, title in visible_windows():
        print(f"0x{hwnd:08X}  {title}")


class ChineseArgumentParser(argparse.ArgumentParser):
    def format_usage(self) -> str:
        return super().format_usage().replace("usage: ", "用法：", 1)

    def format_help(self) -> str:
        text = super().format_help().replace("usage: ", "用法：", 1)
        return text.replace("options:\n", "选项：\n", 1)

    def error(self, message: str) -> None:
        replacements = {
            "unrecognized arguments:": "无法识别的参数：",
            "the following arguments are required:": "缺少必填参数：",
            "expected one argument": "需要提供一个参数值",
            "invalid choice:": "无效选项：",
        }
        for english, chinese in replacements.items():
            message = message.replace(english, chinese)
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}：参数错误：{message}\n")


def parse_args() -> argparse.Namespace:
    parser = ChineseArgumentParser(description="冒险岛弓箭手纯视觉固定地图原型", add_help=False)
    parser.add_argument("-h", "--help", action="help", help="显示这段帮助信息并退出")
    config_source = parser.add_mutually_exclusive_group()
    config_source.add_argument("--config", type=Path, help="配置文件路径")
    config_source.add_argument(
        "--profile",
        choices=PROFILE_KEYS,
        help="运行档案；classic 为原怀旧服，newmaple 为 NewMaple",
    )
    parser.add_argument("--calibrate", action="store_true", help="校准状态栏与小地图")
    parser.add_argument("--capture-recognition-region", action="store_true", help="采集战斗识别区与小地图平台安全点")
    parser.add_argument("--capture-template", action="store_true", help="采集怪物模板")
    parser.add_argument("--capture-monster-filter", action="store_true", help="采集怪物过滤项")
    parser.add_argument("--monster-category", default="", help="怪物模板采集分类；留空为未分类")
    parser.add_argument("--capture-player-template", action="store_true", help="采集玩家模板")
    parser.add_argument("--capture-player-head", action="store_true", help="采集玩家头部模板")
    parser.add_argument("--capture-player-title", action="store_true", help="采集玩家称号勋章模板")
    parser.add_argument("--list-windows", action="store_true", help="列出可见窗口")
    parser.add_argument("--enable-input", action="store_true", help="允许发送按键；仍需按 F8 或控制面板才会启动")
    parser.add_argument("--overlay-only", action="store_true", help="只显示游戏内 HUD，不打开控制面板")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    selected_paths = profile_paths(args.profile or CLASSIC_PROFILE)
    config_path = (args.config or selected_paths.config).resolve()
    if args.list_windows:
        list_windows()
        return 0
    if not config_path.exists():
        known_paths = profile_paths_from_config_path(config_path)
        if known_paths is not None:
            create_config_from_example(config_path, known_paths.example_config)
            print(f"已创建个人配置文件：{config_path}")
        else:
            print(f"找不到配置文件：{config_path}")
            return 2
    if args.calibrate:
        calibrate(config_path)
        return 0
    if args.capture_recognition_region:
        capture_recognition_region(config_path)
        return 0
    if args.capture_template:
        capture_template(config_path, category=args.monster_category)
        return 0
    if args.capture_monster_filter:
        capture_monster_filter(config_path, category=args.monster_category)
        return 0
    if args.capture_player_template:
        capture_player_template(config_path)
        return 0
    if args.capture_player_head:
        capture_player_aux_template(config_path, "head")
        return 0
    if args.capture_player_title:
        capture_player_aux_template(config_path, "title")
        return 0
    if not args.overlay_only:
        from mbv.panel import run_control_panel

        return run_control_panel(config_path, enable_input=bool(args.enable_input))
    config = load_config(config_path)
    bot = BowmanBot(config, input_authorized=bool(args.enable_input))
    overlay = RuntimeOverlay()
    overlay.set_exit_handler(bot.request_exit)
    worker_errors: list[BaseException] = []

    def worker() -> None:
        try:
            bot.run(overlay)
        except BaseException as exc:
            worker_errors.append(exc)
            overlay.close()

    thread = threading.Thread(target=worker, name="MapleVisionWorker", daemon=False)
    thread.start()
    overlay.mainloop()
    thread.join(timeout=3.0)
    if worker_errors:
        raise worker_errors[0]
    return 0


def run() -> None:
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        if os.name == "nt" and Path(sys.executable).name.casefold() == "pythonw.exe":
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            crash_path = LOG_DIR / f"crash-{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt"
            import traceback

            crash_path.write_text(traceback.format_exc(), encoding="utf-8")
            user32.MessageBoxW(
                None,
                f"启动失败：{exc}\n\n详细信息已保存到：\n{crash_path}",
                "冒险岛弓箭手视觉助手",
                0x10,
            )
        raise SystemExit(1)
