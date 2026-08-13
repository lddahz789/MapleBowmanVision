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
        "优先回到平台中心安全范围；范围内选择通用索敌区中最近的同层怪物攻击，"
        "框外同层怪物则接近。无目标时可按配置拾取或左右巡逻。"
    )
    required_recognition_data = ("platform_center",)
    setting_fields = (
        StrategySettingField("platform_center_tolerance", "平台中心安全半径", maximum=0.5),
    )
    default_settings: dict[str, Any] = {
        "platform_center_tolerance": 0.12,
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
            )
        return TargetSelection(target, chase_target)

    def decide(self, context: StrategyActionContext) -> StrategyDecision:
        if context.player_box is None:
            return StrategyDecision("stop", "PLAYER_SCREEN_LOST")

        anchor_x = context.player_anchor[0] if context.player_anchor is not None else (
            context.player_box[0] + context.player_box[2] / 2
        )
        player_x = anchor_x / max(1, context.combat_width)
        center = context.recognition.get("platform_center", {})
        center_x = max(0.0, min(1.0, float(center.get("x", 0.5))))
        tolerance = max(0.0, min(0.5, float(context.settings.get("platform_center_tolerance", 0.12))))
        if abs(player_x - center_x) > tolerance:
            direction = "left" if player_x > center_x else "right"
            return StrategyDecision(
                "move",
                f"RETURN_CENTER_{direction.upper()}",
                direction=direction,
                player_x=player_x,
            )

        if context.target_box is not None:
            target_x = (context.target_box[0] + context.target_box[2] / 2) / max(1, context.combat_width)
            return StrategyDecision(
                "attack",
                "ATTACK",
                target_x=target_x,
                player_x=player_x,
                target_seen=True,
            )
        if context.chase_box is not None:
            target_x = (context.chase_box[0] + context.chase_box[2] / 2) / max(1, context.combat_width)
            return StrategyDecision(
                "chase",
                "CHASE",
                target_x=target_x,
                player_x=player_x,
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
        if context.marker is None:
            return StrategyDecision("stop", "MARKER_LOST")

        direction = context.direction
        if context.marker[0] <= float(context.behavior["patrol_left"]):
            direction = "right"
        elif context.marker[0] >= float(context.behavior["patrol_right"]):
            direction = "left"
        return StrategyDecision("move", f"PATROL_{(direction or 'right').upper()}", direction=direction or "right")
