from __future__ import annotations

from typing import Any

from mbv.strategies.base import (
    StrategyActionContext,
    StrategyDecision,
    StrategySettingField,
    TargetSelection,
    TargetSelectionContext,
)
from mbv.vision import choose_nearest_target


class StationaryAttackStrategy:
    key = "stationary_attack"
    display_name = "原地攻击"
    profession = "通用"
    description = (
        "保持玩家当前位置不动，只在通用索敌区内选择左右两侧的最近同层怪物，"
        "仅在目标换边时转向后原地攻击；范围外目标不追踪，也不巡逻或返回平台中心。"
    )
    required_recognition_data: tuple[str, ...] = ()
    setting_fields: tuple[StrategySettingField, ...] = ()
    default_settings: dict[str, Any] = {}

    def select_targets(self, context: TargetSelectionContext) -> TargetSelection:
        target = choose_nearest_target(
            context.detections,
            context.player_box,
            context.scene_width,
            context.scene_height,
            context.target_area,
            facing=context.facing,
            raw_box=context.player_raw_box,
            player_anchor=context.player_anchor,
        )
        return TargetSelection(target=target, chase_target=None)

    def decide(self, context: StrategyActionContext) -> StrategyDecision:
        if context.player_box is None:
            return StrategyDecision("stop", "PLAYER_SCREEN_LOST")

        anchor_x = context.player_anchor[0] if context.player_anchor is not None else (
            context.player_box[0] + context.player_box[2] / 2
        )
        player_x = anchor_x / max(1, context.combat_width)
        if context.target_box is not None:
            target_x = (context.target_box[0] + context.target_box[2] / 2) / max(1, context.combat_width)
            return StrategyDecision(
                "attack",
                "ATTACK",
                target_x=target_x,
                player_x=player_x,
                target_seen=True,
                face_each_attack=False,
            )
        if context.has_monster_candidates:
            return StrategyDecision("stop", "TARGET_OUT_OF_RANGE", player_x=player_x)
        return StrategyDecision("stop", "SCANNING", player_x=player_x)
