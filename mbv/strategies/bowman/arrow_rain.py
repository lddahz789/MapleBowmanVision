from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any

from mbv.strategies.base import (
    StrategyActionContext, StrategyDecision, StrategySettingField, StrategyToggleField,
    TargetSelection, TargetSelectionContext, valid_point,
)
from mbv.strategies.common.stationary_attack import StationaryAttackStrategy
from mbv.strategies.melee import bounded_number
from mbv.vision import Detection, attack_rect_from_player, player_anchor_center, point_in_attack_rect


@dataclass(frozen=True)
class _CastEvidence:
    target: Detection
    seen_at: float
    player_box: tuple[int, int, int, int]
    marker_pixels: tuple[float, float]
    marker_size: tuple[int, int]
    scene_size: tuple[int, int]


TARGET_GRACE_SECONDS = .25


class ArrowRainStrategy(StationaryAttackStrategy):
    key = "bowman_arrow_rain"
    display_name = "箭雨"
    profession = "弓箭手"
    description = (
        "箭雨左右射程默认各 300 px，可调整；优先攻击射程内怪物，"
        "通用索敌区内的怪物超出射程时先左右靠近，入范围后停走施法。"
        "清完目标后再回位，保留定时右移和可选路线拾取；"
        "普通攻击使用通用攻击键，请绑定箭雨，近身技能可独立设置。"
        "所有攻击均不调整面向；定位异常等安全处理仍优先。"
    )
    default_settings = deepcopy(StationaryAttackStrategy.default_settings)
    default_settings["attack_range_px"] = 300.0
    default_settings["periodic_step_enabled"] = True
    toggle_fields = (
        StrategyToggleField("periodic_step_enabled", "定时向右小步", live_preview=True),
    ) + StationaryAttackStrategy.toggle_fields
    setting_fields = (
        StrategySettingField("attack_range_px", "箭雨左右距离(px)", step=10.,
                             minimum=10., maximum=2000., direct_numeric_input=True),
    ) + StationaryAttackStrategy.setting_fields
    allow_player_lost_recovery = False

    def normalize_settings(self, settings: dict[str, Any]) -> None:
        super().normalize_settings(settings)
        settings["attack_range_px"] = bounded_number(settings.get("attack_range_px"), 300., 10., 2000.)
        enabled = settings.get("periodic_step_enabled", True)
        settings["periodic_step_enabled"] = enabled if isinstance(enabled, bool) else True

    def _periodic_step_enabled(self, context: StrategyActionContext) -> bool:
        return bool(context.settings.get("periodic_step_enabled", True))

    def select_targets(self, context: TargetSelectionContext) -> TargetSelection:
        radius = bounded_number(context.settings.get("attack_range_px"), 300., 10., 2000.)
        search_area = dict(context.target_area)
        area = {**search_area, "forward": radius / max(1, context.scene_width),
                "back": radius / max(1, context.scene_width)}
        def empty(reason: str) -> TargetSelection:
            return TargetSelection(None, None, eligible_candidate_count=0, eligible_detections=(),
                                   attack_area_override=search_area, skill_area_override=area,
                                   skill_area_label="箭雨施法范围", diagnostic={"reason": reason})
        if context.player_box is None:
            return empty("player_unavailable")
        anchor = context.player_anchor or player_anchor_center(context.player_box, context.player_raw_box)
        search_rect = attack_rect_from_player(
            anchor, context.scene_width, context.scene_height, search_area, context.facing)
        evidence = context.continuity_state
        held = not context.detections_fresh
        detections = context.detections
        if held:
            # 只延续已经在原地施放的技能；固定基准不随漏帧滚动续期。
            if not context.allow_target_hold or not isinstance(evidence, _CastEvidence):
                return empty("no_current_detection")
            if not 0 <= context.now - evidence.seen_at <= TARGET_GRACE_SECONDS:
                return empty("target_hold_expired")
            if (not context.player_visual_fresh or context.marker_pixels is None
                    or context.marker_size != evidence.marker_size
                    or (context.scene_width, context.scene_height) != evidence.scene_size
                    or any(abs(a - b) > .5 for a, b in zip(context.marker_pixels, evidence.marker_pixels))
                    or any(abs(a - b) > 3 for a, b in zip(context.player_box, evidence.player_box))):
                return empty("target_hold_localization_changed")
            detections = [evidence.target]
        # 先受用户四边索敌框限制，再按独立像素射程区分施法与靠近。
        eligible = tuple(d for d in detections
                         if 0 <= d.box[0] + d.box[2] / 2 <= context.scene_width
                         and 0 <= d.box[1] + d.box[3] / 2 <= context.scene_height
                         and point_in_attack_rect(d.box[0] + d.box[2] / 2,
                                                  d.box[1] + d.box[3] / 2, search_rect))
        def distance(d):
            return abs(d.box[0] + d.box[2] / 2 - anchor[0])
        in_range = [d for d in eligible if distance(d) <= radius]
        target = min(in_range, key=distance, default=None)
        if held and target is None:
            return empty("target_hold_out_of_range")
        chase = None if target is not None else min(eligible, key=distance, default=None)
        if not held:
            evidence = (_CastEvidence(target, context.now, context.player_box, context.marker_pixels,
                                      context.marker_size, (context.scene_width, context.scene_height))
                        if target is not None and context.player_visual_fresh
                        and context.marker_pixels is not None and context.marker_size is not None else None)
        reason = ("bounded_target_hold" if held else "fresh_attack_target" if target is not None
                  else "outside_skill_range" if chase is not None else "outside_search_area")
        return TargetSelection(target, chase, eligible_candidate_count=len(eligible),
                               eligible_detections=eligible, attack_area_override=search_area,
                               skill_area_override=area, skill_area_label="箭雨施法范围",
                               continuity_state=evidence, diagnostic={
                                   "reason": reason, "eligible_count": len(eligible),
                                   "target_age_seconds": round(context.now - evidence.seen_at, 3)
                                   if evidence is not None else None,
                                   "distance_px": round(distance(target or chase), 1)
                                   if target is not None or chase is not None else None,
                                   "skill_radius_px": radius,
                               })

    def _attack(self, context: StrategyActionContext, player_x: float) -> StrategyDecision:
        attack = super()._attack(context, player_x)
        return replace(
            attack, action="cast",
            state="ARROW_RAIN_MELEE" if attack.attack_key else "ARROW_RAIN",
            attack_key=attack.attack_key or context.default_attack_key,
            attack_skill="melee" if attack.attack_key else "arrow_rain",
            attack_interval_seconds=bounded_number(
                context.behavior.get("attack_interval_seconds"), .2, .1, 10.),
            cast_interval_from_start=True,
            direction=None, target_x=None, face_tap_seconds=None,
        )

    def _route_combat(self, context: StrategyActionContext, player_x: float,
                      state: dict[str, Any]) -> StrategyDecision | None:
        # 无方向技能无需锁定清怪侧，也无需在目标换边时停攻等待。
        state.pop("combat_side", None)
        state.pop("combat_side_missing_since", None)
        if context.action_interrupted or context.minimap_only:
            state.pop("combat_clear_since", None)
        if context.target_box is not None and not context.minimap_only:
            state["combat_pending"] = True
            state.pop("combat_clear_since", None)
            state.pop("dwell_tick_at", None)
            return replace(self._attack(context, player_x), runtime_state=state)
        if context.chase_box is not None and not context.minimap_only:
            state.update(combat_pending=True, navigation_active=True)
            state.pop("combat_clear_since", None)
            state.pop("dwell_tick_at", None)
            target_x = context.chase_box[0] + context.chase_box[2] / 2
            direction = "left" if target_x < player_x * max(1, context.combat_width) else "right"
            # 使用带小地图进展验证的 move，而不是无反馈的连续追踪键。
            return StrategyDecision("move", f"ARROW_RAIN_APPROACH_{direction.upper()}",
                                    direction=direction, player_x=player_x, target_seen=True,
                                    runtime_state=state, cooperative_movement=True)
        if state.get("combat_pending"):
            state.pop("dwell_tick_at", None)
            clear_since = state.setdefault("combat_clear_since", context.now)
            if context.now - clear_since < .4:
                return StrategyDecision("stop", "ARROW_RAIN_CLEAR_WAIT", runtime_state=state)
            state.pop("combat_pending", None)
            state.pop("combat_clear_since", None)
        return None

    def decide(self, context: StrategyActionContext) -> StrategyDecision:
        center = context.recognition.get("platform_center")
        if not valid_point(center):
            return StrategyDecision("stop", "PLATFORM_CENTER_UNCALIBRATED")
        state = dict(context.runtime_state)
        if (context.action_interrupted or context.minimap_only
                or context.player_box is None or context.marker is None):
            state.pop("combat_clear_since", None)
        context = replace(context, runtime_state=state)
        # 活跃拾取路线复用原状态机；正常去返程由上述无方向清怪覆盖，
        # 关闭/掉层/定位异常等安全返程仍不被清怪阻塞。
        if (state.get("phase", "idle") == "idle" and context.marker is not None
                and context.player_box is not None and not context.minimap_only):
            horizontal = bounded_number(context.settings.get("platform_center_tolerance"), .015, .005, .2)
            vertical = bounded_number(context.settings.get("platform_center_vertical_tolerance"), .06, .01, .5)
            away = (abs(context.marker[0] - center["x"]) > horizontal
                    or abs(context.marker[1] - center["y"]) > vertical)
            if (away or context.periodic_step_pending_return or context.chase_box is not None
                    or (context.target_box is None and state.get("combat_pending"))):
                anchor_x = (context.player_anchor[0] if context.player_anchor is not None
                            else context.player_box[0] + context.player_box[2] / 2)
                combat = self._route_combat(context, anchor_x / max(1, context.combat_width), state)
                if combat is not None:
                    return combat
        decision = super().decide(context)
        # 原地等待/普通回位也持久保存无目标确认状态，不能复活已清除的确认链。
        return replace(decision, runtime_state=state) if decision.runtime_state is None else decision
