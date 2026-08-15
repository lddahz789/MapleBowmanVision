"""兼容入口。实现已拆到 mbv/ 包内，批处理仍运行本文件。"""
from __future__ import annotations

from mbv.bot import STATE_LABELS, BowmanBot, runtime_limit_reached
from mbv.calibrate import (
    calibrate,
    capture_combat_region,
    capture_attack_range,
    capture_target_range,
    capture_frozen_selection,
    capture_key_name,
    capture_monster_filter,
    capture_platform_center,
    capture_player_aux_template,
    capture_player_marker,
    capture_player_template,
    capture_recognition_region,
    capture_strategy_area,
    capture_strategy_region,
    capture_status_region,
    capture_template,
    hue_ranges,
)
from mbv.config import create_config_from_example, load_config, save_config, template_counts
from mbv.input import (
    INPUT,
    Keyboard,
    VK,
    WM_CHAR,
    WM_KEYDOWN,
    WM_KEYUP,
    input_delivery,
    key_lparam,
    name_for_vk,
    vk_for,
)
from mbv.vision import (
    Detection,
    PlayerAnchor,
    Template,
    attack_box_from_rectangle,
    bar_fill,
    choose_fused_player_anchor,
    choose_nearest_same_level_target,
    choose_nearest_target,
    find_detections,
    find_monster,
    load_templates,
    monster_template_category,
    monster_template_alpha,
    monster_template_image,
    monster_templates_for_category,
    player_attack_anchor,
    player_marker,
    roi_pixels,
    smooth_player_attack_anchor,
    suppress_monster_detections,
)
from mbv.win32 import process_integrity_level, user32
from mbv.window import WindowInfo, set_window_topmost


def main() -> int:
    from mbv.cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    from mbv.cli import run

    run()
