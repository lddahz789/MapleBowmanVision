"""共享的按怪物体型判定近身技能逻辑；不执行输入。"""
from __future__ import annotations

import math
from typing import Any


def bounded_number(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        if isinstance(value, bool):
            raise TypeError
        number = float(value)
    except (TypeError, ValueError):
        number = default
    if not math.isfinite(number):
        number = default
    return max(minimum, min(maximum, number))


def normalized_player_gap(player_x: float, target: tuple[int, int, int, int]) -> float:
    """玩家水平锚点到怪物边缘的间隙，以当前怪物宽度为单位。"""
    target_x, _target_y, target_width, _target_height = target
    target_center_x = float(target_x) + float(target_width) / 2.0
    edge_gap = max(0.0, abs(target_center_x - float(player_x)) - float(target_width) / 2.0)
    return edge_gap / max(1.0, float(target_width))


def melee_distances(settings: dict[str, Any]) -> tuple[float, float]:
    enter = bounded_number(settings.get("melee_enter_distance_multiplier"), 0.35, 0.0, 3.0)
    leave = bounded_number(settings.get("melee_exit_distance_multiplier"), 0.9, 0.0, 5.0)
    return enter, max(enter, leave)


def normalize_melee_settings(settings: dict[str, Any]) -> None:
    key = settings.get("melee_skill_key", "")
    settings["melee_skill_key"] = key.strip().lower() if isinstance(key, str) else ""
    enter, leave = melee_distances(settings)
    settings["melee_enter_distance_multiplier"] = enter
    settings["melee_exit_distance_multiplier"] = leave


def choose_melee_skill(
    player_x: float,
    target_box: tuple[int, int, int, int],
    settings: dict[str, Any],
    previous_skill: str | None,
) -> str | None:
    """返回近身技能键；进入后保持至退出阈值，空键立即禁用。"""
    key = settings.get("melee_skill_key", "")
    if not isinstance(key, str) or not key.strip():
        return None
    enter, leave = melee_distances(settings)
    threshold = leave if previous_skill == "melee" else enter
    if normalized_player_gap(player_x, target_box) <= threshold:
        return key.strip().lower()
    return None
