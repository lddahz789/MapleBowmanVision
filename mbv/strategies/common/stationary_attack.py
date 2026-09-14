from __future__ import annotations

from typing import Any
from dataclasses import replace

from mbv.strategies.base import (
    StrategyActionContext,
    StrategyCaptureField,
    StrategyDecision,
    StrategySettingField,
    StrategyToggleField,
    TargetSelection,
    TargetSelectionContext,
    valid_point,
)
from mbv.vision import (
    attack_rect_from_player, choose_nearest_bidirectional_target,
    player_anchor_center, point_in_attack_rect,
)
from mbv.strategies.melee import bounded_number, choose_melee_skill, normalize_melee_settings


class StationaryAttackStrategy:
    key = "stationary_attack"
    display_name = "原地攻击"
    profession = "通用"
    description = (
        "保持玩家当前位置不动，只在通用索敌区内选择左右两侧的最近同层怪物，"
        "仅在目标换边时转向后原地攻击；目标贴近时使用近身技能，远离后恢复普通攻击。"
        "近身距离按怪物宽度自动换算。平时偏离小地图安全点也优先回位；"
        "每隔 45 秒向右短走一步，确认位移并回位后再继续输出。"
        "可开启同平台定时路线拾取：去程与返程均先原地清怪，再继续行进。"
    )
    required_recognition_data = ("platform_center",)
    toggle_fields = (StrategyToggleField("route_pickup_enabled", "定时路线拾取（同平台）", live_preview=True),)
    choice_fields = ()
    capture_fields = (StrategyCaptureField(
        "stationary_pickup_point", "采集目标拾取点", "点击与安全点同平台的目标拾取点",
        "目标拾取点", enable_setting="route_pickup_enabled",
        coordinate_space="minimap", capture_kind="point",
    ),)
    setting_fields = (
        StrategySettingField("route_pickup_interval_seconds", "路线拾取间隔秒", step=1.0,
                             minimum=5.0, maximum=86400.0, direct_numeric_input=True),
        StrategySettingField("route_pickup_dwell_seconds", "到点有效拾取秒", step=0.1,
                             minimum=0.3, maximum=10.0, direct_numeric_input=True),
        StrategySettingField("route_pickup_timeout_seconds", "到点前最长秒（含清怪）", step=1.0,
                             minimum=5.0, maximum=600.0, direct_numeric_input=True),
        StrategySettingField("route_pickup_visual_grace_seconds", "拾取定位丢失等待秒", step=0.1,
                             minimum=0.2, maximum=3.0, direct_numeric_input=True),
        StrategySettingField("route_pickup_collect_timeout_seconds", "到点阶段最长秒（含清怪）", step=1.0,
                             minimum=10.0, maximum=600.0, direct_numeric_input=True),
        StrategySettingField(
            "melee_skill_key",
            "近身技能键（空=不用）",
            step=None,
            minimum=None,
            maximum=None,
            capture_key=True,
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
        "route_pickup_enabled": False,
        "route_pickup_interval_seconds": 180.0,
        "route_pickup_dwell_seconds": 1.5,
        "route_pickup_timeout_seconds": 90.0,
        "route_pickup_visual_grace_seconds": 1.0,
        "route_pickup_collect_timeout_seconds": 60.0,
        "melee_skill_key": "",
        "melee_enter_distance_multiplier": 0.35,
        "melee_exit_distance_multiplier": 0.9,
        "periodic_step_interval_seconds": 45.0,
        "periodic_step_seconds": 0.12,
        "platform_center_tolerance": 0.015,
        "platform_center_vertical_tolerance": 0.06,
        "platform_return_jump_interval_seconds": 0.45,
    }

    def normalize_settings(self, settings: dict[str, Any]) -> None:
        normalize_melee_settings(settings)
        if "route_pickup_enabled" in settings:
            settings["route_pickup_enabled"] = settings["route_pickup_enabled"] is True
        for key, default, minimum, maximum in (
            ("route_pickup_interval_seconds", 180.0, 5.0, 86400.0),
            ("route_pickup_dwell_seconds", 1.5, 0.3, 10.0),
            ("route_pickup_timeout_seconds", 90.0, 5.0, 600.0),
            ("route_pickup_visual_grace_seconds", 1.0, 0.2, 3.0),
            ("route_pickup_collect_timeout_seconds", 60.0, 10.0, 600.0),
        ):
            if key in settings:
                settings[key] = bounded_number(settings[key], default, minimum, maximum)
        if "route_pickup_collect_timeout_seconds" in settings:
            settings["route_pickup_collect_timeout_seconds"] = max(
                settings["route_pickup_collect_timeout_seconds"],
                bounded_number(settings.get("route_pickup_dwell_seconds"), 1.5, .3, 10.) + 1.,
            )

    def _attack(self, context: StrategyActionContext, player_x: float) -> StrategyDecision:
        target = context.target_box
        assert target is not None
        melee_key = choose_melee_skill(
            player_x * max(1, context.combat_width), target, context.settings,
            context.previous_attack_skill,
        )
        return StrategyDecision(
            "attack", "ATTACK_MELEE" if melee_key else "ATTACK",
            target_x=(target[0] + target[2] / 2) / max(1, context.combat_width),
            player_x=player_x, target_seen=True, face_each_attack=False,
            attack_key=melee_key, attack_skill="melee" if melee_key else "single",
        )

    def _route_combat(self, context: StrategyActionContext, player_x: float,
                      state: dict[str, Any]) -> StrategyDecision | None:
        """只清理当前有效目标；短暂无目标先等待，不追怪、不宣称确认死亡。"""
        if context.action_interrupted:
            state.pop("combat_side_missing_since", None)
        if context.target_box is not None:
            state["combat_pending"] = True
            state.pop("combat_clear_since", None)
            state.pop("dwell_tick_at", None)
            # 最近目标每帧可能左右互换。路线清怪先清当前侧，不能把另一侧
            # 稍近一点解释成必须立即转身；只使用本帧索敌区内的有效检测。
            boxes = [d.box for d in context.eligible_detections]
            if context.target_box not in boxes:
                boxes.append(context.target_box)
            px = player_x * max(1, context.combat_width)
            side = state.get("combat_side")
            same_side = [box for box in boxes if
                         ("left" if box[0] + box[2] / 2 < px else "right") == side]
            target = context.target_box
            if same_side:
                target = min(same_side, key=lambda box: abs(box[0] + box[2] / 2 - px))
                state.pop("combat_side_missing_since", None)
            elif side is not None:
                missing = state.setdefault("combat_side_missing_since", context.now)
                if context.now - missing < 0.4:
                    return StrategyDecision("stop", "PICKUP_CLEAR_WAIT", runtime_state=state)
                state.pop("combat_side_missing_since", None)
            state["combat_side"] = "left" if target[0] + target[2] / 2 < px else "right"
            return replace(self._attack(replace(context, target_box=target), player_x),
                           runtime_state=state, face_tap_seconds=0.08)
        if state.get("combat_pending"):
            state.pop("dwell_tick_at", None)
            clear_since = state.setdefault("combat_clear_since", context.now)
            if context.now - clear_since < 0.4:
                return StrategyDecision("stop", "PICKUP_CLEAR_WAIT", runtime_state=state)
            state.pop("combat_pending", None)
            state.pop("combat_clear_since", None)
            state.pop("combat_side", None)
            state.pop("combat_side_missing_since", None)
        return None

    def _route(self, context: StrategyActionContext, player_x: float,
               home: bool, same_level: bool, tolerance: float
               ) -> tuple[StrategyDecision | None, dict[str, Any]]:
        state = dict(context.runtime_state)
        if context.minimap_only or context.action_interrupted:
            state.pop("combat_clear_since", None)
            state.pop("combat_side_missing_since", None)
        phase = state.get("phase", "idle")
        enabled = bool(context.settings.get("route_pickup_enabled", False))
        point = context.recognition.get("stationary_pickup_point")
        center = context.recognition.get("platform_center")
        valid = (context.recognition.get("stationary_pickup_point_captured", False)
                 and context.recognition.get("stationary_pickup_point_space") == "minimap"
                 and valid_point(point) and valid_point(center))
        vertical_tolerance = float(context.settings.get("platform_center_vertical_tolerance", 0.06))
        valid = (valid and abs(point["y"] - center["y"]) <= vertical_tolerance
                 and abs(point["x"] - center["x"]) > tolerance)
        interval = bounded_number(context.settings.get("route_pickup_interval_seconds"), 180., 5., 86400.)
        timeout = bounded_number(context.settings.get("route_pickup_timeout_seconds"), 90., 5., 600.)
        if phase == "idle":
            if not enabled:
                return None, state
            if not valid:
                if not home:
                    return None, state
                return StrategyDecision("stop", "PICKUP_POINT_INVALID"), state
            if (not home or context.minimap_only or context.periodic_step_pending_return
                    or context.now - state.get("last_completed_at", context.started_at) < interval):
                return None, state
            state.update(phase="outbound", route_started_at=context.now,
                         outbound_started_at=context.now,
                         navigation_active=True, departed=False)
            state.pop("return_reason", None)
            phase = "outbound"
        if not home:
            state["departed"] = True
        if phase in {"outbound", "collect"}:
            grace = bounded_number(context.settings.get("route_pickup_visual_grace_seconds"), 1., .2, 3.)
            reason = None
            if not enabled:
                reason = "disabled"
            elif not valid:
                reason = "point_invalid"
            elif not same_level:
                reason = "off_platform"
            elif context.localization_lost_seconds >= grace:
                reason = "localization_timeout"
            if reason:
                state.update(phase="returning", return_reason=reason)
                phase = "returning"
            elif context.minimap_only:
                state.pop("dwell_tick_at", None)
                return StrategyDecision("stop", "PICKUP_WAIT_LOCALIZATION", runtime_state=state), state
            else:
                assert context.marker is not None
                at_point = abs(point["x"] - context.marker[0]) <= tolerance
                if at_point:
                    state["phase"] = phase = "collect"
                    state.setdefault("collection_started_at", context.now)
                elif phase == "collect":
                    # 被击退离开目标点后重新计本段去程，不能复活最早出发的超时。
                    state["phase"] = phase = "outbound"
                    state["outbound_started_at"] = context.now
                    state.pop("dwell_tick_at", None)
                collection_timeout = max(
                    bounded_number(context.settings.get("route_pickup_collect_timeout_seconds"), 60., 10., 600.),
                    bounded_number(context.settings.get("route_pickup_dwell_seconds"), 1.5, .3, 10.) + 1.,
                )
                if ("collection_started_at" in state
                        and context.now - state["collection_started_at"] >= collection_timeout):
                    reason = "collection_timeout"
                elif (phase == "outbound" and context.now - state.get(
                        "outbound_started_at", state["route_started_at"]) >= timeout):
                    reason = "outbound_timeout"
                if reason:
                    state.update(phase="returning", return_reason=reason)
                    phase = "returning"
        if phase == "returning":
            # 返程清怪时短暂丢视觉也先停走；不能绕过去程已有的丢失宽限，
            # 一帧小地图补位就插入反向回家键，随后又继续攻击。
            if (context.minimap_only and enabled and valid and same_level
                    and state.get("combat_pending")
                    and state.get("return_reason") not in {
                        "disabled", "point_invalid", "off_platform", "localization_timeout"}
                    and context.localization_lost_seconds < bounded_number(
                        context.settings.get("route_pickup_visual_grace_seconds"), 1., .2, 3.)):
                return StrategyDecision("stop", "PICKUP_WAIT_LOCALIZATION", runtime_state=state), state
            # 正常完成和计时到期的返程仍先清怪；取消/定位异常/掉层等安全返程不拦截。
            if (enabled and valid and same_level and not context.minimap_only
                    and state.get("return_reason") not in {
                        "disabled", "point_invalid", "off_platform", "localization_timeout"}):
                combat = self._route_combat(context, player_x, state)
                if combat is not None:
                    return combat, state
            if home:
                return StrategyDecision(
                    "stop", "PICKUP_RETURNED", reset_periodic_step=bool(state.get("departed")),
                    runtime_state={"phase": "idle", "last_completed_at": context.now,
                                   "return_reason": state.get("return_reason", "collected")},
                ), state
            return None, state  # 清怪完成或安全中止后，复用平台回位（包括掉层）。
        assert context.marker is not None
        combat = self._route_combat(context, player_x, state)
        if combat is not None:
            return combat, state
        delta = point["x"] - context.marker[0]
        if abs(delta) > tolerance:
            state["phase"] = "outbound"
            state.pop("dwell_tick_at", None)
            direction = "right" if delta > 0 else "left"
            return StrategyDecision(
                "move", f"PICKUP_OUTBOUND_{direction.upper()}", direction=direction,
                runtime_state=state, pickup_interval_seconds=0.15, cooperative_movement=True,
            ), state
        state["phase"] = "collect"
        tick = state.get("dwell_tick_at")
        elapsed = float(state.get("dwell_elapsed", 0.0))
        # 只累计连续、确实发过拾取键的有效画面；上游药/Buff/定位中断不计入。
        if (tick is not None and not context.action_interrupted
                and 0.0 <= context.now - tick <= 0.5 and context.last_pickup >= tick - 0.2):
            elapsed += context.now - tick
        state.update(dwell_elapsed=elapsed, dwell_tick_at=context.now)
        dwell = bounded_number(context.settings.get("route_pickup_dwell_seconds"), 1.5, 0.3, 10.)
        if elapsed >= dwell:
            state.update(phase="returning", return_reason="collected")
            return StrategyDecision("stop", "PICKUP_START_RETURN", runtime_state=state), state
        return StrategyDecision("pickup", "PICKUP_COLLECT", runtime_state=state,
                                pickup_interval_seconds=0.15), state

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
        eligible = ()
        if context.player_box is not None:
            anchor = context.player_anchor or player_anchor_center(
                context.player_box, context.player_raw_box)
            rects = [attack_rect_from_player(anchor, context.scene_width, context.scene_height,
                                            context.target_area, side) for side in ("left", "right")]
            eligible = tuple(d for d in context.detections if any(
                point_in_attack_rect(d.box[0] + d.box[2] / 2, d.box[1] + d.box[3] / 2, rect)
                for rect in rects))
        return TargetSelection(target=target, chase_target=None, eligible_detections=eligible)

    def decide(self, context: StrategyActionContext) -> StrategyDecision:
        if context.marker is None or (context.player_box is None and not context.minimap_only):
            return self._normal_decide(context)
        center = context.recognition.get("platform_center", {})
        horizontal = bounded_number(context.settings.get("platform_center_tolerance"), .015, .005, .2)
        vertical = bounded_number(context.settings.get("platform_center_vertical_tolerance"), .06, .01, .5)
        same_level = abs(context.marker[1] - center.get("y", .5)) <= vertical
        home = same_level and abs(context.marker[0] - center.get("x", .5)) <= horizontal
        anchor_x = context.player_anchor[0] if context.player_anchor is not None else (
            context.player_box[0] + context.player_box[2] / 2 if context.player_box else 0.0
        )
        route, state = self._route(context, anchor_x / max(1, context.combat_width),
                                   home, same_level, horizontal)
        if route is not None:
            return route
        decision = self._normal_decide(context)
        if state.get("phase") == "returning":
            return replace(
                decision, runtime_state=state, cooperative_movement=True,
                pickup_interval_seconds=(0.15 if decision.action == "move"
                    and not context.minimap_only and context.settings.get("route_pickup_enabled") else None),
            )
        return decision

    def _periodic_step_enabled(self, context: StrategyActionContext) -> bool:
        return True

    def _normal_decide(self, context: StrategyActionContext) -> StrategyDecision:
        if context.player_box is None and not context.minimap_only:
            return StrategyDecision("stop", "PLAYER_SCREEN_LOST")

        anchor_x = context.player_anchor[0] if context.player_anchor is not None else (
            context.player_box[0] + context.player_box[2] / 2 if context.player_box else 0.0
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

        if context.minimap_only:
            return StrategyDecision("stop", "MINIMAP_WAITING_VISUAL")

        step_interval = max(
            5.0,
            min(
                600.0,
                float(context.settings.get("periodic_step_interval_seconds", 45.0)),
            ),
        )
        if self._periodic_step_enabled(context) and context.now - context.last_periodic_step >= step_interval:
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
            return self._attack(context, player_x)
        if context.has_monster_candidates:
            return StrategyDecision("stop", "TARGET_OUT_OF_RANGE", player_x=player_x)
        return StrategyDecision("stop", "SCANNING", player_x=player_x)
