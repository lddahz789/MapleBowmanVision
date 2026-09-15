"""龙咆哮的定点计数与回位决策；不执行截图、按键或窗口操作。"""
from __future__ import annotations

import math
from typing import Any, Iterable

from mbv.strategies.base import (
    StrategyActionContext,
    StrategyDecision,
    StrategySettingField,
    TargetSelection,
    TargetSelectionContext,
    valid_point,
)
from mbv.strategies.melee import bounded_number
from mbv.strategies.regions import normalize_target_regions
from mbv.vision import (
    Detection,
    MINIMAP_REGION_SPACE,
    point_in_attack_rect,
)


def _valid_position(value: Any) -> bool:
    return (
        isinstance(value, (tuple, list))
        and len(value) == 2
        and valid_point({"x": value[0], "y": value[1]})
    )


def _outside(delta: float, tolerance: float) -> bool:
    # 小地图归一化值的浮点误差不应把恰好位于容差边缘的点判为越界。
    return abs(delta) > tolerance + 1e-9


def _range_candidates(
    detections: Iterable[Detection],
    width: int,
    height: int,
) -> tuple[Detection, ...]:
    if width <= 0 or height <= 0:
        return ()
    # 怪物检测输入已裁为固定战斗识别区，不再依赖角色屏幕锚点。
    rects = [(0, 0, width, height)]
    eligible: list[Detection] = []
    seen_boxes: set[tuple[int, int, int, int]] = set()
    # 公共视觉层已做模板去重。这里只枚举一次候选，不按区域累加怪物数；
    # 同一框的重复引用也不增加计数，不再额外匹配或合并相邻的真实怪物。
    for detection in detections:
        x, y, box_width, box_height = detection.box
        if box_width <= 0 or box_height <= 0 or detection.box in seen_boxes:
            continue
        if any(point_in_attack_rect(x + box_width / 2.0, y + box_height / 2.0, rect)
               for rect in rects):
            seen_boxes.add(detection.box)
            eligible.append(detection)
    return tuple(eligible)


class DragonRoarStrategy:
    key = "dragon_roar"
    display_name = "龙咆哮·定点"
    profession = "战士·龙骑士"
    description = (
        "只用唯一实时小地图标记定位，固定战斗识别区内本帧怪物数量严格超过阈值才施放龙咆哮。"
        "不需要姓名牌或头部；沿用平台安全点，偏离先回位，标记丢失停止，回位超时后需暂停重启。"
    )
    localization_mode = "minimap"
    allow_player_lost_recovery = False
    required_recognition_data: tuple[str, ...] = ("platform_center",)
    toggle_fields = ()
    choice_fields = ()
    capture_fields = ()
    setting_fields = (
        StrategySettingField("skill_key", "龙咆哮技能键（空=不施放）",
                             step=None, minimum=None, maximum=None, capture_key=True),
        StrategySettingField("monster_count_threshold", "怪物数量超过此值才施放",
                             step=1, minimum=0, maximum=23, direct_numeric_input=True),
        StrategySettingField("cast_interval_seconds", "施放间隔（毫秒）",
                             step=0.1, minimum=0.1, maximum=10.0, direct_numeric_input=True,
                             display_multiplier=1000.0),
        StrategySettingField("return_tolerance_x", "定点水平容差（小地图比例）",
                             step=0.005, minimum=0.005, maximum=0.5, direct_numeric_input=True),
        StrategySettingField("return_tolerance_y", "定点垂直容差（小地图比例）",
                             step=0.01, minimum=0.01, maximum=0.5, direct_numeric_input=True),
        StrategySettingField("return_jump_interval_seconds", "回位跳跃间隔秒",
                             step=0.05, minimum=0.1, maximum=2.0, direct_numeric_input=True),
        StrategySettingField("return_timeout_seconds", "单次回位最长秒",
                             step=1.0, minimum=3.0, maximum=60.0, direct_numeric_input=True),
    )
    default_settings: dict[str, Any] = {
        "skill_key": "",
        "monster_count_threshold": 2,
        "cast_interval_seconds": 1.0,
        "return_tolerance_x": 0.015,
        "return_tolerance_y": 0.06,
        "return_jump_interval_seconds": 0.45,
        "return_timeout_seconds": 15.0,
        "attack_regions": [],
    }

    def normalize_settings(self, settings: dict[str, Any]) -> None:
        key = settings.get("skill_key", "")
        settings["skill_key"] = key.strip().lower() if isinstance(key, str) else ""
        for field in self.setting_fields:
            if field.capture_key:
                continue
            value = bounded_number(settings.get(field.path), self.default_settings[field.path],
                                   field.minimum, field.maximum)
            settings[field.path] = int(value) if field.path == "monster_count_threshold" else value
        settings["attack_regions"] = normalize_target_regions(settings.get("attack_regions"))

    def select_targets(self, context: TargetSelectionContext) -> TargetSelection:
        eligible: tuple[Detection, ...] = ()
        if context.detections_fresh:
            eligible = _range_candidates(
                context.detections, context.scene_width, context.scene_height,
            )
        # 代表目标仅用于显示；施法依据固定战斗区的实时数量，不依赖面向。
        target = min(eligible, key=lambda item: math.hypot(
            item.box[0] + item.box[2] / 2.0 - context.scene_width / 2,
            item.box[1] + item.box[3] / 2.0 - context.scene_height / 2,
        ), default=None)
        return TargetSelection(target=target, chase_target=None,
                               eligible_candidate_count=len(eligible), eligible_detections=eligible,
                               uses_common_target_area=False)

    def decide(self, context: StrategyActionContext) -> StrategyDecision:
        settings = dict(context.settings)
        self.normalize_settings(settings)
        state = dict(context.runtime_state)
        state.setdefault("phase", "idle")
        state["navigation_active"] = True
        eligible = _range_candidates(
            context.eligible_detections, context.combat_width, context.combat_height,
        )
        state["monster_count"] = len(eligible)

        def decision(action: str, name: str, **kwargs: Any) -> StrategyDecision:
            return StrategyDecision(action=action, state=name, runtime_state=state, **kwargs)

        if state["phase"] == "blocked":
            return decision("stop", "DRAGON_RETURN_BLOCKED")
        point = context.recognition.get("platform_center")
        if (not context.recognition.get("platform_center_captured")
                or context.recognition.get("platform_center_space") != MINIMAP_REGION_SPACE
                or not valid_point(point)):
            return decision("stop", "DRAGON_POINT_UNCALIBRATED")
        if context.combat_width <= 0 or context.combat_height <= 0:
            return decision("stop", "DRAGON_RANGE_UNCALIBRATED")
        if not _valid_position(context.marker):
            return decision("stop", "MARKER_LOST")

        marker_x, marker_y = context.marker
        dx, dy = marker_x - point["x"], marker_y - point["y"]
        tolerance_x = settings["return_tolerance_x"]
        tolerance_y = settings["return_tolerance_y"]
        if state["phase"] != "returning" and (
            _outside(dx, tolerance_x) or _outside(dy, tolerance_y)
        ):
            state["phase"] = "returning"
            state["return_started_at"] = context.now

        if state["phase"] == "returning":
            started = bounded_number(state.get("return_started_at"), context.now, 0.0, context.now)
            state["return_started_at"] = started
            if context.now - started >= settings["return_timeout_seconds"]:
                state["phase"] = "blocked"
                state["return_reason"] = "timeout"
                return decision("stop", "DRAGON_RETURN_BLOCKED")
            # 返回途中收紧到启动容差的六成，避免边界附近反复施法和移动。
            arrival_x, arrival_y = tolerance_x * 0.6, tolerance_y * 0.6
            if _outside(dx, arrival_x):
                direction = "left" if dx > 0 else "right"
                return decision("move", f"DRAGON_RETURN_{direction.upper()}",
                                direction=direction, player_x=marker_x, target_x=point["x"])
            if _outside(dy, arrival_y):
                if context.now - context.last_jump < settings["return_jump_interval_seconds"]:
                    return decision("stop", "DRAGON_WAITING_RETURN_JUMP")
                if dy > 0:
                    return decision("jump", "DRAGON_RETURN_JUMP", player_x=marker_x)
                return decision("down_jump", "DRAGON_RETURN_DOWN_JUMP", player_x=marker_x)
            state["phase"] = "idle"
            state.pop("return_started_at", None)

        skill_key = settings["skill_key"]
        if not skill_key:
            return decision("stop", "DRAGON_SKILL_UNBOUND")
        if len(eligible) <= settings["monster_count_threshold"]:
            return decision("stop", "DRAGON_WAITING_MONSTERS", target_seen=bool(eligible))
        return decision("cast", "DRAGON_ROAR", target_seen=True,
                        attack_key=skill_key, attack_skill="dragon_roar",
                        attack_interval_seconds=settings["cast_interval_seconds"],
                        face_each_attack=False)
