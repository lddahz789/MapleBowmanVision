from __future__ import annotations

import math
from typing import Any

from mbv.strategies.base import (
    StrategyActionContext,
    StrategyCaptureField,
    StrategyChoiceField,
    StrategyDecision,
    StrategySettingField,
    StrategyToggleField,
    TargetSelection,
    TargetSelectionContext,
    horizontal_overlap_ratio,
)
from mbv.vision import (
    MINIMAP_REGION_SPACE,
    PLAYER_RELATIVE_REGION_SPACE,
    attack_rect_from_player,
    player_anchor_center,
    player_relative_region_rect,
    point_in_attack_rect,
)


TARGET_PRIORITY_CHOICES = (
    ("region_priority_then_distance", "区域优先级 → 水平距离"),
    ("nearest", "水平距离最近"),
    ("highest_score", "识别分最高"),
)


def normalize_target_regions(value: Any) -> list[dict[str, Any]]:
    """清理角色相对索敌区；旧的屏幕固定区域不能可靠迁移，直接失效。"""
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    for index, raw in enumerate(value, start=1):
        if not isinstance(raw, dict):
            continue
        if raw.get("space") != PLAYER_RELATIVE_REGION_SPACE:
            continue
        try:
            offset_x = float(raw["offset_x"])
            offset_y = float(raw["offset_y"])
            width = float(raw["w"])
            height = float(raw["h"])
        except (KeyError, TypeError, ValueError):
            continue
        if not all(math.isfinite(item) for item in (offset_x, offset_y, width, height)):
            continue
        offset_x = max(-1.0, min(1.0, offset_x))
        offset_y = max(-1.0, min(1.0, offset_y))
        width = max(0.0, min(1.0, width))
        height = max(0.0, min(1.0, height))
        if width <= 0.0 or height <= 0.0:
            continue
        region_id = str(raw.get("id", "")).strip()
        if not region_id or region_id in used_ids:
            suffix = index
            region_id = f"region_{suffix}"
            while region_id in used_ids:
                suffix += 1
                region_id = f"region_{suffix}"
        used_ids.add(region_id)
        try:
            priority = int(raw.get("priority", index))
        except (TypeError, ValueError):
            priority = index
        name = str(raw.get("name", "")).strip() or f"索敌区 {index}"
        normalized.append(
            {
                "id": region_id,
                "name": name,
                "enabled": bool(raw.get("enabled", True)),
                "priority": max(0, min(999, priority)),
                "space": PLAYER_RELATIVE_REGION_SPACE,
                "offset_x": round(offset_x, 6),
                "offset_y": round(offset_y, 6),
                "w": round(width, 6),
                "h": round(height, 6),
            }
        )
    return normalized


class ThrowingStarSafeStrategy:
    key = "throwing_star_safe"
    display_name = "标飞安全输出"
    profession = "飞侠·标飞"
    description = (
        "安全输出区在小地图上框选；启用后，玩家低于安全区时优先朝中心移动并连续向上跳。"
        "可配置多个跟随角色位置但不随面向翻转的独立索敌区，并可在近距离攻击前跳跃。"
    )
    required_recognition_data: tuple[str, ...] = ()
    toggle_fields = (
        StrategyToggleField("use_target_regions", "启用标飞多索敌区"),
        StrategyToggleField("use_common_target_box", "同时限制在通用索敌框内"),
        StrategyToggleField("only_targets_below_player", "只攻击角色下方目标"),
        StrategyToggleField("auto_face_target", "索敌成功后先自动面向目标"),
        StrategyToggleField("use_near_target_jump_attack", "启用近目标每次跳跃攻击"),
        StrategyToggleField("use_close_jump_attack", "启用近身重叠跳跃攻击"),
        StrategyToggleField("use_safe_output_area", "启用标飞安全输出区回位"),
        StrategyToggleField("patrol_inside_safe_area", "安全输出区内无目标时巡逻"),
    )
    capture_fields = (
        StrategyCaptureField(
            recognition_key="throwing_star_safe_output_area",
            button_label="在小地图框选安全输出区",
            prompt="在放大的小地图上框选标飞允许站立并输出的安全区域，回车确认",
            debug_label="标飞安全输出区",
            enable_setting="use_safe_output_area",
            coordinate_space=MINIMAP_REGION_SPACE,
        ),
        StrategyCaptureField(
            recognition_key="throwing_star_target_regions",
            button_label="新增标飞索敌区",
            prompt="框选一个跟随角色位置且不随面向翻转的标飞索敌区，回车确认",
            debug_label="标飞索敌区",
            enable_setting="use_target_regions",
            settings_path="target_regions",
            multiple=True,
        ),
    )
    choice_fields = (
        StrategyChoiceField(
            "target_priority_mode",
            "选敌优先级",
            TARGET_PRIORITY_CHOICES,
        ),
    )
    setting_fields = (
        StrategySettingField(
            "target_face_tap_seconds",
            "自动转向短按秒",
            step=0.005,
            minimum=0.0,
            maximum=0.1,
        ),
        StrategySettingField(
            "jump_interval_seconds",
            "回位跳跃间隔秒",
            step=0.05,
            minimum=0.1,
            maximum=2.0,
        ),
        StrategySettingField(
            "safe_patrol_edge_margin",
            "安全区巡逻边距",
            step=0.01,
            minimum=0.0,
            maximum=0.2,
        ),
        StrategySettingField(
            "minimum_target_vertical_gap",
            "目标最小向下高度差",
            step=0.01,
            minimum=0.0,
            maximum=0.5,
        ),
        StrategySettingField(
            "near_target_jump_attack_distance_px",
            "近目标跳攻距离像素",
            step=5.0,
            minimum=0.0,
            maximum=1000.0,
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
        "use_target_regions": True,
        "use_common_target_box": False,
        "only_targets_below_player": True,
        "auto_face_target": True,
        "target_priority_mode": "region_priority_then_distance",
        "target_regions": [],
        "target_face_tap_seconds": 0.025,
        "use_near_target_jump_attack": True,
        "near_target_jump_attack_distance_px": 120.0,
        "use_close_jump_attack": True,
        "use_safe_output_area": False,
        "patrol_inside_safe_area": False,
        "jump_interval_seconds": 0.35,
        "safe_patrol_edge_margin": 0.02,
        "minimum_target_vertical_gap": 0.02,
        "close_overlap_threshold": 0.2,
        "jump_attack_cooldown_seconds": 0.45,
    }

    def normalize_settings(self, settings: dict[str, Any]) -> None:
        settings["target_regions"] = normalize_target_regions(settings.get("target_regions"))
        choices = {value for value, _label in TARGET_PRIORITY_CHOICES}
        if settings.get("target_priority_mode") not in choices:
            settings["target_priority_mode"] = "region_priority_then_distance"

    def select_targets(self, context: TargetSelectionContext) -> TargetSelection:
        if context.player_box is None:
            return TargetSelection(target=None, chase_target=None)
        player_x, player_y = context.player_anchor or player_anchor_center(
            context.player_box,
            context.player_raw_box,
        )
        minimum_gap = max(0.0, min(0.5, float(context.settings.get("minimum_target_vertical_gap", 0.02))))
        minimum_y = player_y + minimum_gap * max(1, context.scene_height)
        only_below = bool(context.settings.get("only_targets_below_player", True))
        use_regions = bool(context.settings.get("use_target_regions", True))
        regions = [
            region
            for region in normalize_target_regions(context.settings.get("target_regions"))
            if region["enabled"]
        ]
        region_rects = [
            (
                int(region["priority"]),
                player_relative_region_rect(
                    (player_x, player_y),
                    context.scene_width,
                    context.scene_height,
                    region,
                ),
            )
            for region in regions
        ]
        ranked: list[tuple[int, float, float, Any]] = []
        for detection in context.detections:
            x, y, width, height = detection.box
            target_x = x + width / 2.0
            target_y = y + height / 2.0
            if only_below and target_y < minimum_y:
                continue
            priority = 0
            if use_regions:
                matches = [
                    region_priority
                    for region_priority, region_rect in region_rects
                    if point_in_attack_rect(target_x, target_y, region_rect)
                ]
                if not matches:
                    continue
                priority = min(matches)
            ranked.append((priority, target_x, target_y, detection))

        eligible_count = len(ranked)
        use_common_target_box = bool(context.settings.get("use_common_target_box", False))
        if use_common_target_box:
            attack_rect = attack_rect_from_player(
                (player_x, player_y),
                context.scene_width,
                context.scene_height,
                context.target_area,
                context.facing,
            )
            ranked = [
                item for item in ranked if point_in_attack_rect(item[1], item[2], attack_rect)
            ]
        eligible_detections = tuple(item[3] for item in ranked)
        if not ranked:
            return TargetSelection(
                target=None,
                chase_target=None,
                eligible_candidate_count=eligible_count,
                eligible_detections=eligible_detections,
                uses_common_target_area=use_common_target_box,
            )

        mode = str(context.settings.get("target_priority_mode", "region_priority_then_distance"))
        if mode == "highest_score":
            ranked.sort(
                key=lambda item: (
                    -item[3].score,
                    abs(item[1] - player_x),
                    abs(item[2] - player_y),
                )
            )
        elif mode == "nearest":
            ranked.sort(
                key=lambda item: (
                    abs(item[1] - player_x),
                    abs(item[2] - player_y),
                    -item[3].score,
                )
            )
        else:
            ranked.sort(
                key=lambda item: (
                    item[0],
                    abs(item[1] - player_x),
                    abs(item[2] - player_y),
                    -item[3].score,
                )
            )
        return TargetSelection(
            target=ranked[0][3],
            chase_target=None,
            eligible_candidate_count=eligible_count,
            eligible_detections=eligible_detections,
            uses_common_target_area=use_common_target_box,
        )

    def decide(self, context: StrategyActionContext) -> StrategyDecision:
        if context.player_box is None and not context.minimap_only:
            return StrategyDecision("stop", "PLAYER_SCREEN_LOST")

        anchor_x, anchor_y = context.player_anchor or (
            context.player_box[0] + context.player_box[2] / 2.0 if context.player_box else 0.0,
            float(context.player_box[1]) if context.player_box else 0.0,
        )
        player_x = anchor_x / max(1, context.combat_width)
        player_y = anchor_y / max(1, context.combat_height)
        safe_patrol_bounds: tuple[float, float] | None = None
        safe_player_x: float | None = None
        if bool(context.settings.get("use_safe_output_area", False)):
            area = context.recognition.get("throwing_star_safe_output_area", {})
            if not isinstance(area, dict) or area.get("space") != MINIMAP_REGION_SPACE:
                return StrategyDecision("stop", "SAFE_OUTPUT_UNCALIBRATED", player_x=player_x)
            if context.marker is None:
                return StrategyDecision("stop", "MARKER_LOST", player_x=player_x)
            safe_player_x, safe_player_y = context.marker
            left = max(0.0, min(1.0, float(area.get("x", 0.45))))
            top = max(0.0, min(1.0, float(area.get("y", 0.4))))
            width = max(0.0, min(1.0 - left, float(area.get("w", 0.1))))
            height = max(0.0, min(1.0 - top, float(area.get("h", 0.1))))
            right = left + width
            bottom = top + height

            direction = None
            if safe_player_x < left:
                direction = "right"
            elif safe_player_x > right:
                direction = "left"

            # 安全区和角色位置统一使用小地图坐标，不受战斗画面镜头滚动影响。
            if safe_player_y > bottom:
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

            if safe_player_y < top:
                return StrategyDecision("stop", "SAFE_OUTPUT_ABOVE", player_x=player_x)

            if direction is not None:
                return StrategyDecision(
                    "move",
                    f"RETURN_SAFE_{direction.upper()}",
                    direction=direction,
                    player_x=player_x,
                )
            safe_patrol_bounds = (left, right)

        if context.minimap_only:
            return StrategyDecision("stop", "MINIMAP_WAITING_VISUAL")

        if context.target_box is not None:
            target_x = (context.target_box[0] + context.target_box[2] / 2) / max(1, context.combat_width)
            desired_direction = "left" if target_x < player_x else "right"
            dead_zone = max(0.0, float(context.behavior.get("attack_dead_zone", 0.015)))
            if abs(target_x - player_x) <= dead_zone and context.direction in {"left", "right"}:
                desired_direction = context.direction
            if (
                bool(context.settings.get("auto_face_target", True))
                and context.direction != desired_direction
            ):
                return StrategyDecision(
                    "face",
                    f"FACE_TARGET_{desired_direction.upper()}",
                    direction=desired_direction,
                    target_x=target_x,
                    player_x=player_x,
                    target_seen=True,
                    face_tap_seconds=max(
                        0.0,
                        min(0.1, float(context.settings.get("target_face_tap_seconds", 0.025))),
                    ),
                )
            overlap_ratio = horizontal_overlap_ratio(context.player_box, context.target_box)
            target_center_x = context.target_box[0] + context.target_box[2] / 2
            target_distance_px = abs(target_center_x - anchor_x)
            near_jump_distance_px = max(
                0.0,
                min(
                    1000.0,
                    float(context.settings.get("near_target_jump_attack_distance_px", 120.0)),
                ),
            )
            if (
                bool(context.settings.get("use_near_target_jump_attack", True))
                and target_distance_px <= near_jump_distance_px
            ):
                attack_interval = max(
                    0.05,
                    float(context.behavior.get("attack_interval_seconds", 0.24)),
                )
                if context.now - context.last_jump_attack >= attack_interval:
                    return StrategyDecision(
                        "jump_attack",
                        "JUMP_ATTACK_NEAR",
                        target_x=target_x,
                        player_x=player_x,
                        target_seen=True,
                        face_each_attack=False,
                        close_overlap_ratio=overlap_ratio,
                    )
                return StrategyDecision(
                    "stop",
                    "WAITING_NEAR_JUMP_ATTACK",
                    player_x=player_x,
                    target_seen=True,
                    close_overlap_ratio=overlap_ratio,
                )
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
        if (
            safe_patrol_bounds is not None
            and bool(context.settings.get("patrol_inside_safe_area", False))
        ):
            left, right = safe_patrol_bounds
            margin = max(
                0.0,
                min(
                    (right - left) / 2.0,
                    float(context.settings.get("safe_patrol_edge_margin", 0.02)),
                ),
            )
            if safe_player_x is not None and safe_player_x <= left + margin:
                patrol_direction = "right"
            elif safe_player_x is not None and safe_player_x >= right - margin:
                patrol_direction = "left"
            elif context.direction in {"left", "right"}:
                patrol_direction = context.direction
            else:
                patrol_direction = "right"
            return StrategyDecision(
                "move",
                f"SAFE_PATROL_{patrol_direction.upper()}",
                direction=patrol_direction,
                player_x=player_x,
            )
        if context.has_monster_candidates:
            return StrategyDecision("stop", "TARGET_OUT_OF_RANGE", player_x=player_x)
        return StrategyDecision("stop", "SCANNING", player_x=player_x)
