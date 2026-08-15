from __future__ import annotations

from typing import Any

from mbv.strategies.base import (
    StrategyActionContext,
    StrategyDecision,
    StrategySettingField,
    TargetSelection,
    TargetSelectionContext,
)
from mbv.vision import choose_nearest_same_level_target, choose_nearest_target


class BowmanDynamicStrategy:
    key = "bowman_dynamic"
    display_name = "弓箭手动态"
    profession = "弓箭手"
    description = (
        "按小地图水平和垂直位置优先回到平台安全点附近；掉到下层时跳回，"
        "位于上层时下跳。范围内选择通用索敌区中最近的同层怪物攻击，"
        "每次攻击前按当前目标方向重新转向。框外同层怪物则接近；无目标时可拾取或巡逻。"
    )
    required_recognition_data = ("platform_center",)
    toggle_fields = ()
    capture_fields = ()
    setting_fields = (
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
        "platform_center_tolerance": 0.08,
        "platform_center_vertical_tolerance": 0.06,
        "platform_return_jump_interval_seconds": 0.45,
    }

    def select_targets(self, context: TargetSelectionContext) -> TargetSelection:
        attack_box = context.target_area
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
        return TargetSelection(target, chase_target)

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
            return StrategyDecision(
                "attack",
                "ATTACK",
                target_x=target_x,
                player_x=combat_player_x,
                target_seen=True,
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
