from __future__ import annotations

from typing import Any

from mbv.strategies.base import (
    StrategyActionContext,
    StrategyCaptureField,
    StrategyDecision,
    StrategySettingField,
    StrategyToggleField,
    TargetSelection,
    TargetSelectionContext,
    horizontal_overlap_ratio,
)
from mbv.vision import choose_nearest_target, player_anchor_center


class ThrowingStarSafeStrategy:
    key = "throwing_star_safe"
    display_name = "标飞安全输出"
    profession = "飞侠·标飞"
    description = (
        "安全输出区可选；启用后，玩家低于安全区时优先朝中心移动并连续向上跳。"
        "目标与角色水平重叠达到阈值时使用跳跃攻击，否则普通投掷；不追怪、不巡逻。"
    )
    required_recognition_data: tuple[str, ...] = ()
    toggle_fields = (
        StrategyToggleField("use_close_jump_attack", "启用近身重叠跳跃攻击"),
        StrategyToggleField("use_safe_output_area", "启用标飞安全输出区回位"),
    )
    capture_fields = (
        StrategyCaptureField(
            recognition_key="throwing_star_safe_output_area",
            button_label="框选标飞安全输出位置",
            prompt="框选标飞允许站立并输出的安全区域，回车确认",
            debug_label="标飞安全输出区",
            enable_setting="use_safe_output_area",
        ),
    )
    setting_fields = (
        StrategySettingField(
            "jump_interval_seconds",
            "回位跳跃间隔秒",
            step=0.05,
            minimum=0.1,
            maximum=2.0,
        ),
        StrategySettingField(
            "minimum_target_vertical_gap",
            "目标最小向下高度差",
            step=0.01,
            minimum=0.0,
            maximum=0.5,
        ),
        StrategySettingField(
            "close_overlap_threshold",
            "近身水平重叠阈值",
            step=0.05,
            minimum=0.0,
            maximum=1.0,
        ),
        StrategySettingField(
            "jump_attack_cooldown_seconds",
            "跳跃攻击冷却秒",
            step=0.05,
            minimum=0.1,
            maximum=2.0,
        ),
    )
    default_settings: dict[str, Any] = {
        "use_close_jump_attack": True,
        "use_safe_output_area": False,
        "jump_interval_seconds": 0.35,
        "minimum_target_vertical_gap": 0.02,
        "close_overlap_threshold": 0.2,
        "jump_attack_cooldown_seconds": 0.45,
    }

    def select_targets(self, context: TargetSelectionContext) -> TargetSelection:
        if context.player_box is None:
            return TargetSelection(target=None, chase_target=None)
        _player_x, player_y = context.player_anchor or player_anchor_center(
            context.player_box,
            context.player_raw_box,
        )
        minimum_gap = max(
            0.0,
            min(0.5, float(context.settings.get("minimum_target_vertical_gap", 0.02))),
        )
        minimum_y = player_y + minimum_gap * max(1, context.scene_height)
        targets_below = [
            detection
            for detection in context.detections
            if detection.box[1] + detection.box[3] / 2.0 >= minimum_y
        ]
        target = choose_nearest_target(
            targets_below,
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

        anchor_x, anchor_y = context.player_anchor or (
            context.player_box[0] + context.player_box[2] / 2.0,
            float(context.player_box[1]),
        )
        player_x = anchor_x / max(1, context.combat_width)
        player_y = anchor_y / max(1, context.combat_height)
        if bool(context.settings.get("use_safe_output_area", False)):
            area = context.recognition.get("throwing_star_safe_output_area", {})
            left = max(0.0, min(1.0, float(area.get("x", 0.45))))
            top = max(0.0, min(1.0, float(area.get("y", 0.4))))
            width = max(0.0, min(1.0 - left, float(area.get("w", 0.1))))
            height = max(0.0, min(1.0 - top, float(area.get("h", 0.1))))
            right = left + width
            bottom = top + height

            direction = None
            if player_x < left:
                direction = "right"
            elif player_x > right:
                direction = "left"

            # 当前只处理玩家位于安全区下方的回位；位于上方时暂不做下跳逻辑。
            if player_y > bottom:
                jump_interval = max(
                    0.1,
                    min(2.0, float(context.settings.get("jump_interval_seconds", 0.35))),
                )
                if context.now - context.last_jump >= jump_interval:
                    suffix = direction.upper() if direction else "UP"
                    return StrategyDecision(
                        "jump",
                        f"RETURN_SAFE_JUMP_{suffix}",
                        direction=direction,
                        player_x=player_x,
                    )
                if direction is not None:
                    return StrategyDecision(
                        "move",
                        f"RETURN_SAFE_{direction.upper()}",
                        direction=direction,
                        player_x=player_x,
                    )
                return StrategyDecision("stop", "WAITING_SAFE_JUMP", player_x=player_x)

            if player_y < top:
                return StrategyDecision("stop", "SAFE_OUTPUT_ABOVE", player_x=player_x)

            if direction is not None:
                return StrategyDecision(
                    "move",
                    f"RETURN_SAFE_{direction.upper()}",
                    direction=direction,
                    player_x=player_x,
                )

        if context.target_box is not None:
            target_x = (context.target_box[0] + context.target_box[2] / 2) / max(1, context.combat_width)
            overlap_ratio = horizontal_overlap_ratio(context.player_box, context.target_box)
            overlap_threshold = max(
                0.0,
                min(1.0, float(context.settings.get("close_overlap_threshold", 0.2))),
            )
            if bool(context.settings.get("use_close_jump_attack", True)) and overlap_ratio >= overlap_threshold:
                cooldown = max(
                    0.1,
                    min(2.0, float(context.settings.get("jump_attack_cooldown_seconds", 0.45))),
                )
                if context.now - context.last_jump_attack >= cooldown:
                    return StrategyDecision(
                        "jump_attack",
                        "JUMP_ATTACK_CLOSE",
                        target_x=target_x,
                        player_x=player_x,
                        target_seen=True,
                        face_each_attack=False,
                        close_overlap_ratio=overlap_ratio,
                    )
                return StrategyDecision(
                    "stop",
                    "WAITING_JUMP_ATTACK",
                    player_x=player_x,
                    target_seen=True,
                    close_overlap_ratio=overlap_ratio,
                )
            return StrategyDecision(
                "attack",
                "ATTACK",
                target_x=target_x,
                player_x=player_x,
                target_seen=True,
                face_each_attack=False,
                close_overlap_ratio=overlap_ratio,
            )
        if context.has_monster_candidates:
            return StrategyDecision("stop", "TARGET_OUT_OF_RANGE", player_x=player_x)
        return StrategyDecision("stop", "SCANNING", player_x=player_x)
