from __future__ import annotations

import math
from typing import Any

from mbv.strategies.base import (
    StrategyActionContext,
    StrategyDecision,
    StrategySettingField,
    TargetSelection,
    TargetSelectionContext,
)
from mbv.vision import (
    Detection,
    attack_rect_from_player,
    choose_nearest_same_level_target,
    choose_nearest_target,
    player_anchor_center,
    point_in_attack_rect,
)


def _bounded_number(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        if isinstance(value, bool):
            raise TypeError
        number = float(value)
    except (TypeError, ValueError):
        number = default
    if not math.isfinite(number):
        number = default
    return max(minimum, min(maximum, number))


def normalized_monster_gap(
    first: tuple[int, int, int, int],
    second: tuple[int, int, int, int],
) -> float:
    """两只怪物矩形的边缘间隙，以两者平均可见体型为单位。"""
    first_x, first_y, first_width, first_height = first
    second_x, second_y, second_width, second_height = second
    horizontal_gap = max(
        0.0,
        float(first_x - (second_x + second_width)),
        float(second_x - (first_x + first_width)),
    )
    vertical_gap = max(
        0.0,
        float(first_y - (second_y + second_height)),
        float(second_y - (first_y + first_height)),
    )
    average_size = (
        math.sqrt(max(1.0, float(first_width * first_height)))
        + math.sqrt(max(1.0, float(second_width * second_height)))
    ) / 2.0
    return math.hypot(horizontal_gap, vertical_gap) / max(1.0, average_size)


def normalized_player_gap(
    player_x: float,
    target: tuple[int, int, int, int],
) -> float:
    """玩家水平中心到怪物边缘的间隙，以当前怪物宽度为单位。"""
    target_x, _target_y, target_width, _target_height = target
    target_center_x = float(target_x) + float(target_width) / 2.0
    edge_gap = max(0.0, abs(target_center_x - float(player_x)) - float(target_width) / 2.0)
    return edge_gap / max(1.0, float(target_width))


def nearby_monster_count(
    target_box: tuple[int, int, int, int],
    eligible_detections: tuple[Detection, ...],
    maximum_gap: float,
) -> int:
    count = 1
    for detection in eligible_detections:
        other_box = detection.box
        if other_box == target_box:
            continue
        if normalized_monster_gap(target_box, other_box) <= maximum_gap:
            count += 1
    return count


def choose_aoe_cluster_target(
    eligible_detections: tuple[Detection, ...],
    player_point: tuple[float, float],
    settings: dict[str, Any],
) -> Detection | None:
    """选择怪物数最多、且离玩家最近的聚怪中心候选。"""
    if not str(settings.get("aoe_skill_key", "")).strip():
        return None
    cluster_distance = _bounded_number(
        settings.get("aoe_cluster_distance_multiplier"), 0.75, 0.0, 4.0
    )
    minimum_monsters = int(
        round(_bounded_number(settings.get("aoe_min_monsters"), 2.0, 2.0, 8.0))
    )
    player_x, player_y = player_point
    ranked: list[tuple[int, float, float, Detection]] = []
    for detection in eligible_detections:
        count = nearby_monster_count(detection.box, eligible_detections, cluster_distance)
        if count < minimum_monsters:
            continue
        x, y, width, height = detection.box
        distance = math.hypot(x + width / 2.0 - player_x, y + height / 2.0 - player_y)
        ranked.append((-count, distance, -float(detection.score), detection))
    if not ranked:
        return None
    ranked.sort(key=lambda item: (item[0], item[1], item[2]))
    return ranked[0][3]


def choose_bowman_attack_skill(
    player_x: float,
    target_box: tuple[int, int, int, int],
    eligible_detections: tuple[Detection, ...],
    settings: dict[str, Any],
    previous_skill: str | None,
) -> tuple[str, str | None]:
    """近身优先，其次聚怪 AOE，最后使用单体技能。"""
    single_key = str(settings.get("single_skill_key", "")).strip().lower() or None
    melee_key = str(settings.get("melee_skill_key", "")).strip().lower() or None
    aoe_key = str(settings.get("aoe_skill_key", "")).strip().lower() or None
    enter_distance = _bounded_number(
        settings.get("melee_enter_distance_multiplier"),
        0.35,
        0.0,
        3.0,
    )
    exit_distance = max(
        enter_distance,
        _bounded_number(
            settings.get("melee_exit_distance_multiplier"),
            0.9,
            0.0,
            5.0,
        ),
    )
    player_gap = normalized_player_gap(player_x, target_box)
    if melee_key and (
        player_gap <= enter_distance
        or (previous_skill == "melee" and player_gap <= exit_distance)
    ):
        return "melee", melee_key

    cluster_distance = _bounded_number(
        settings.get("aoe_cluster_distance_multiplier"),
        0.75,
        0.0,
        4.0,
    )
    minimum_monsters = int(
        round(_bounded_number(settings.get("aoe_min_monsters"), 2.0, 2.0, 8.0))
    )
    nearby_count = nearby_monster_count(target_box, eligible_detections, cluster_distance)
    if aoe_key and nearby_count >= minimum_monsters:
        return "aoe", aoe_key
    return "single", single_key


class BowmanDynamicStrategy:
    key = "bowman_dynamic"
    display_name = "弓箭手动态"
    profession = "弓箭手"
    description = (
        "按小地图水平和垂直位置优先回到平台安全点附近；掉到下层时跳回，"
        "位于上层时下跳。范围内选择通用索敌区中最近的同层怪物攻击，"
        "近身怪物优先使用近身技能，聚集怪物使用 AOE，否则使用单体技能。"
        "距离按怪物模板体型自动换算；框外同层怪物则接近，无目标时可拾取或巡逻。"
    )
    required_recognition_data = ("platform_center",)
    toggle_fields = ()
    choice_fields = ()
    capture_fields = ()
    setting_fields = (
        StrategySettingField(
            "aoe_skill_key",
            "AOE 技能键（空=不用）",
            step=None,
            minimum=None,
            maximum=None,
            capture_key=True,
        ),
        StrategySettingField(
            "single_skill_key",
            "单体技能键（空=攻击键）",
            step=None,
            minimum=None,
            maximum=None,
            capture_key=True,
        ),
        StrategySettingField(
            "melee_skill_key",
            "近身技能键（空=不用）",
            step=None,
            minimum=None,
            maximum=None,
            capture_key=True,
        ),
        StrategySettingField(
            "aoe_min_monsters",
            "AOE 最少怪物数",
            step=1.0,
            minimum=2.0,
            maximum=8.0,
            direct_numeric_input=True,
        ),
        StrategySettingField(
            "aoe_cluster_distance_multiplier",
            "AOE 聚怪距离倍率",
            step=0.1,
            minimum=0.0,
            maximum=4.0,
            direct_numeric_input=True,
        ),
        StrategySettingField(
            "melee_enter_distance_multiplier",
            "近身进入距离倍率",
            step=0.1,
            minimum=0.0,
            maximum=3.0,
            direct_numeric_input=True,
        ),
        StrategySettingField(
            "melee_exit_distance_multiplier",
            "近身退出距离倍率",
            step=0.1,
            minimum=0.0,
            maximum=5.0,
            direct_numeric_input=True,
        ),
        StrategySettingField("platform_center_tolerance", "小地图水平安全半径", maximum=0.5),
        StrategySettingField("platform_center_vertical_tolerance", "小地图垂直安全半径", maximum=0.5),
        StrategySettingField(
            "platform_return_jump_interval_seconds",
            "回安全点跳跃间隔秒",
            step=0.05,
            minimum=0.1,
            maximum=2.0,
        ),
    )
    default_settings: dict[str, Any] = {
        "aoe_skill_key": "",
        "single_skill_key": "",
        "melee_skill_key": "",
        "aoe_min_monsters": 2,
        "aoe_cluster_distance_multiplier": 0.75,
        "melee_enter_distance_multiplier": 0.35,
        "melee_exit_distance_multiplier": 0.9,
        "platform_center_tolerance": 0.08,
        "platform_center_vertical_tolerance": 0.06,
        "platform_return_jump_interval_seconds": 0.45,
    }

    def normalize_settings(self, settings: dict[str, Any]) -> None:
        for key in ("aoe_skill_key", "single_skill_key", "melee_skill_key"):
            value = settings.get(key, "")
            settings[key] = str(value).strip().lower() if isinstance(value, str) else ""
        settings["aoe_min_monsters"] = int(
            round(_bounded_number(settings.get("aoe_min_monsters"), 2.0, 2.0, 8.0))
        )
        settings["aoe_cluster_distance_multiplier"] = _bounded_number(
            settings.get("aoe_cluster_distance_multiplier"), 0.75, 0.0, 4.0
        )
        enter_distance = _bounded_number(
            settings.get("melee_enter_distance_multiplier"), 0.35, 0.0, 3.0
        )
        settings["melee_enter_distance_multiplier"] = enter_distance
        settings["melee_exit_distance_multiplier"] = max(
            enter_distance,
            _bounded_number(settings.get("melee_exit_distance_multiplier"), 0.9, 0.0, 5.0),
        )

    def select_targets(self, context: TargetSelectionContext) -> TargetSelection:
        attack_box = context.target_area
        eligible_detections: tuple[Detection, ...] = ()
        if context.player_box is not None:
            player_point = context.player_anchor or player_anchor_center(
                context.player_box,
                context.player_raw_box,
            )
            attack_rect = attack_rect_from_player(
                player_point,
                context.scene_width,
                context.scene_height,
                attack_box,
                context.facing,
            )
            eligible_detections = tuple(
                detection
                for detection in context.detections
                if point_in_attack_rect(
                    detection.box[0] + detection.box[2] / 2.0,
                    detection.box[1] + detection.box[3] / 2.0,
                    attack_rect,
                )
            )
        target = choose_nearest_target(
            context.detections,
            context.player_box,
            context.scene_width,
            context.scene_height,
            attack_box,
            facing=context.facing,
            raw_box=context.player_raw_box,
            player_anchor=context.player_anchor,
            distance_mode="euclidean",
        )
        if target is not None and eligible_detections:
            player_point = context.player_anchor or player_anchor_center(
                context.player_box,
                context.player_raw_box,
            )
            melee_key = str(context.settings.get("melee_skill_key", "")).strip()
            melee_enter = _bounded_number(
                context.settings.get("melee_enter_distance_multiplier"), 0.35, 0.0, 3.0
            )
            melee_exit = max(
                melee_enter,
                _bounded_number(
                    context.settings.get("melee_exit_distance_multiplier"), 0.9, 0.0, 5.0
                ),
            )
            melee_limit = melee_exit if context.previous_attack_skill == "melee" else melee_enter
            keep_nearest_for_melee = bool(melee_key) and (
                normalized_player_gap(player_point[0], target.box) <= melee_limit
            )
            if not keep_nearest_for_melee:
                cluster_target = choose_aoe_cluster_target(
                    eligible_detections,
                    player_point,
                    context.settings,
                )
                if cluster_target is not None:
                    target = cluster_target
        chase_target = None
        if target is None:
            chase_target = choose_nearest_same_level_target(
                context.detections,
                context.player_box,
                context.scene_width,
                context.scene_height,
                attack_box,
                raw_box=context.player_raw_box,
                player_anchor=context.player_anchor,
                distance_mode="euclidean",
            )
        return TargetSelection(
            target,
            chase_target,
            eligible_detections=eligible_detections,
        )

    def decide(self, context: StrategyActionContext) -> StrategyDecision:
        if context.player_box is None:
            return StrategyDecision("stop", "PLAYER_SCREEN_LOST")

        combat_anchor_x = context.player_anchor[0] if context.player_anchor is not None else (
            context.player_box[0] + context.player_box[2] / 2
        )
        combat_player_x = combat_anchor_x / max(1, context.combat_width)
        if context.marker is None:
            return StrategyDecision("stop", "MARKER_LOST")

        marker_x = max(0.0, min(1.0, float(context.marker[0])))
        marker_y = max(0.0, min(1.0, float(context.marker[1])))
        center = context.recognition.get("platform_center", {})
        center_x = max(0.0, min(1.0, float(center.get("x", 0.5))))
        center_y = max(0.0, min(1.0, float(center.get("y", 0.5))))
        horizontal_tolerance = max(
            0.0,
            min(0.5, float(context.settings.get("platform_center_tolerance", 0.08))),
        )
        vertical_tolerance = max(
            0.0,
            min(0.5, float(context.settings.get("platform_center_vertical_tolerance", 0.06))),
        )
        horizontal_delta = marker_x - center_x
        vertical_delta = marker_y - center_y
        if abs(horizontal_delta) > horizontal_tolerance:
            direction = "left" if marker_x > center_x else "right"
            return StrategyDecision(
                "move",
                f"RETURN_CENTER_{direction.upper()}",
                direction=direction,
                player_x=marker_x,
            )

        if abs(vertical_delta) > vertical_tolerance:
            jump_interval = max(
                0.1,
                min(
                    2.0,
                    float(context.settings.get("platform_return_jump_interval_seconds", 0.45)),
                ),
            )
            if context.now - context.last_jump < jump_interval:
                return StrategyDecision("stop", "WAITING_CENTER_JUMP", player_x=marker_x)
            if vertical_delta < 0.0:
                return StrategyDecision(
                    "down_jump",
                    "RETURN_CENTER_DOWN_JUMP",
                    player_x=marker_x,
                )
            alignment_dead_zone = min(0.03, max(0.01, horizontal_tolerance * 0.35))
            direction = None
            if horizontal_delta > alignment_dead_zone:
                direction = "left"
            elif horizontal_delta < -alignment_dead_zone:
                direction = "right"
            return StrategyDecision(
                "jump",
                f"RETURN_CENTER_JUMP_{(direction or 'up').upper()}",
                direction=direction,
                player_x=marker_x,
            )

        if context.target_box is not None:
            target_x = (context.target_box[0] + context.target_box[2] / 2) / max(1, context.combat_width)
            attack_skill, attack_key = choose_bowman_attack_skill(
                combat_anchor_x,
                context.target_box,
                context.eligible_detections,
                context.settings,
                context.previous_attack_skill,
            )
            return StrategyDecision(
                "attack",
                f"ATTACK_{attack_skill.upper()}",
                target_x=target_x,
                player_x=combat_player_x,
                target_seen=True,
                attack_key=attack_key,
                attack_skill=attack_skill,
            )
        if context.chase_box is not None:
            target_x = (context.chase_box[0] + context.chase_box[2] / 2) / max(1, context.combat_width)
            return StrategyDecision(
                "chase",
                "CHASE",
                target_x=target_x,
                player_x=combat_player_x,
                target_seen=True,
            )
        if context.has_monster_candidates:
            return StrategyDecision("stop", "TARGET_OUT_OF_RANGE")

        target_lost_for = context.now - context.last_target_seen
        if (
            bool(context.behavior["pickup_after_target_lost"])
            and 0.25 < target_lost_for < 0.8
            and context.now - context.last_pickup > 1.0
        ):
            return StrategyDecision("pickup", "PICKUP")
        if (
            not bool(context.behavior["fallback_patrol"])
            or target_lost_for < float(context.behavior["target_lost_patrol_delay_seconds"])
        ):
            return StrategyDecision("stop", "SCANNING")
        direction = context.direction
        if context.marker[0] <= float(context.behavior["patrol_left"]):
            direction = "right"
        elif context.marker[0] >= float(context.behavior["patrol_right"]):
            direction = "left"
        return StrategyDecision("move", f"PATROL_{(direction or 'right').upper()}", direction=direction or "right")
