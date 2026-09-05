from __future__ import annotations

from typing import Any

from mbv.strategies.base import (
    StrategyActionContext,
    StrategyDecision,
    StrategySettingField,
    TargetSelection,
    TargetSelectionContext,
)
from mbv.vision import choose_nearest_bidirectional_target


class StationaryAttackStrategy:
    key = "stationary_attack"
    display_name = "原地攻击"
    profession = "通用"
    description = (
        "保持玩家当前位置不动，只在通用索敌区内选择左右两侧的最近同层怪物，"
        "仅在目标换边时转向后原地攻击。平时偏离小地图安全点也优先回位；"
        "每隔 45 秒向右短走一步，确认位移并回位后再继续输出。"
    )
    required_recognition_data = ("platform_center",)
    toggle_fields = ()
    choice_fields = ()
    capture_fields = ()
    setting_fields = (
        StrategySettingField(
            "periodic_step_interval_seconds",
            "定时右移间隔秒",
            step=1.0,
            minimum=5.0,
            maximum=600.0,
        ),
        StrategySettingField(
            "periodic_step_seconds",
            "向右小步时长秒",
            step=0.01,
            minimum=0.03,
            maximum=0.5,
        ),
        StrategySettingField(
            "platform_center_tolerance",
            "小地图回位半径",
            step=0.005,
            minimum=0.005,
            maximum=0.2,
        ),
        StrategySettingField(
            "platform_center_vertical_tolerance",
            "小地图垂直安全半径",
            step=0.01,
            minimum=0.01,
            maximum=0.5,
        ),
        StrategySettingField(
            "platform_return_jump_interval_seconds",
            "回安全点跳跃间隔秒",
            step=0.05,
            minimum=0.1,
            maximum=2.0,
        ),
    )
    default_settings: dict[str, Any] = {
        "periodic_step_interval_seconds": 45.0,
        "periodic_step_seconds": 0.12,
        "platform_center_tolerance": 0.015,
        "platform_center_vertical_tolerance": 0.06,
        "platform_return_jump_interval_seconds": 0.45,
    }

    def select_targets(self, context: TargetSelectionContext) -> TargetSelection:
        target = choose_nearest_bidirectional_target(
            context.detections,
            context.player_box,
            context.scene_width,
            context.scene_height,
            context.target_area,
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
        if context.marker is None:
            return StrategyDecision("stop", "MARKER_LOST", player_x=player_x)

        # 每帧检查安全点，不能只在周期短步之后回位。
        if context.marker is not None:
            marker_x = max(0.0, min(1.0, float(context.marker[0])))
            marker_y = max(0.0, min(1.0, float(context.marker[1])))
            center = context.recognition.get("platform_center", {})
            center_x = max(0.0, min(1.0, float(center.get("x", 0.5))))
            center_y = max(0.0, min(1.0, float(center.get("y", 0.5))))
            horizontal_tolerance = max(
                0.005,
                min(0.2, float(context.settings.get("platform_center_tolerance", 0.015))),
            )
            vertical_tolerance = max(
                0.01,
                min(
                    0.5,
                    float(context.settings.get("platform_center_vertical_tolerance", 0.06)),
                ),
            )
            horizontal_delta = marker_x - center_x
            vertical_delta = marker_y - center_y
            if abs(horizontal_delta) > horizontal_tolerance:
                direction = "left" if horizontal_delta > 0.0 else "right"
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
                    return StrategyDecision("down_jump", "RETURN_CENTER_DOWN_JUMP", player_x=marker_x)
                direction = None
                if horizontal_delta > horizontal_tolerance * 0.5:
                    direction = "left"
                elif horizontal_delta < -horizontal_tolerance * 0.5:
                    direction = "right"
                return StrategyDecision(
                    "jump",
                    f"RETURN_CENTER_JUMP_{(direction or 'up').upper()}",
                    direction=direction,
                    player_x=marker_x,
                )
            if context.periodic_step_pending_return:
                return StrategyDecision(
                    "stop",
                    "PERIODIC_STEP_RETURNED",
                    player_x=marker_x,
                    periodic_step_return_complete=True,
                )

        step_interval = max(
            5.0,
            min(
                600.0,
                float(context.settings.get("periodic_step_interval_seconds", 45.0)),
            ),
        )
        if context.now - context.last_periodic_step >= step_interval:
            step_seconds = max(
                0.03,
                min(0.5, float(context.settings.get("periodic_step_seconds", 0.12))),
            )
            return StrategyDecision(
                "step",
                "PERIODIC_STEP_RIGHT",
                direction="right",
                player_x=player_x,
                move_seconds=step_seconds,
            )
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
