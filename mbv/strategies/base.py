from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Literal, Protocol

from mbv.vision import Detection


def valid_point(value: Any) -> bool:
    return isinstance(value, dict) and all(
        isinstance(value.get(axis), (int, float)) and not isinstance(value.get(axis), bool)
        and math.isfinite(value[axis]) and 0.0 <= value[axis] <= 1.0
        for axis in ("x", "y")
    )


def horizontal_overlap_ratio(
    player_box: tuple[int, int, int, int] | None,
    target_box: tuple[int, int, int, int] | None,
) -> float:
    """水平重叠宽度除以两者较小宽度；不受垂直层级影响。"""
    if player_box is None or target_box is None:
        return 0.0
    player_left, _py, player_width, _ph = player_box
    target_left, _ty, target_width, _th = target_box
    overlap = max(
        0.0,
        min(player_left + player_width, target_left + target_width) - max(player_left, target_left),
    )
    return overlap / max(1.0, min(float(player_width), float(target_width)))


@dataclass(frozen=True)
class StrategySettingField:
    """面板可编辑的策略参数；path 相对于该策略的 settings。"""

    path: str
    label: str
    step: float | None = 0.01
    minimum: float | None = 0.0
    maximum: float | None = 1.0
    capture_key: bool = False
    direct_numeric_input: bool = False


@dataclass(frozen=True)
class StrategyToggleField:
    """面板可切换并持久化的策略布尔参数。"""

    path: str
    label: str
    live_preview: bool = False


@dataclass(frozen=True)
class StrategyChoiceField:
    """面板可选择的策略枚举参数。"""

    path: str
    label: str
    choices: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class StrategyCaptureField:
    """面板按策略生成的矩形或点位采集入口。"""

    recognition_key: str
    button_label: str
    prompt: str
    debug_label: str
    enable_setting: str | None = None
    settings_path: str | None = None
    multiple: bool = False
    coordinate_space: str = "combat"
    capture_kind: Literal["rectangle", "point"] = "rectangle"
    required: bool = False


@dataclass(frozen=True)
class TargetSelectionContext:
    detections: list[Detection]
    player_box: tuple[int, int, int, int] | None
    player_raw_box: tuple[int, int, int, int] | None
    player_anchor: tuple[float, float] | None
    scene_width: int
    scene_height: int
    facing: str | None
    target_area: dict[str, float]
    settings: dict[str, Any]
    previous_attack_skill: str | None = None
    # False 表示 detections 仅为视觉短暂保留值，不能用于精确数量触发。
    detections_fresh: bool = True
    # 可选连续施法证据；策略实例无状态，由 Bot 按会话传回。
    now: float = 0.0
    continuity_state: Any = None
    allow_target_hold: bool = False
    player_visual_fresh: bool = False
    marker_pixels: tuple[float, float] | None = None
    marker_size: tuple[int, int] | None = None


@dataclass(frozen=True)
class TargetSelection:
    target: Detection | None
    chase_target: Detection | None
    eligible_candidate_count: int | None = None
    eligible_detections: tuple[Detection, ...] | None = None
    uses_common_target_area: bool = True
    # 策略按像素计算的攻击区可复用公共 HUD；不覆盖持久化通用索敌区。
    attack_area_override: dict[str, float] | None = None
    # 施法距离与索敌/追踪区域分开，避免像素射程冒充用户框选的索敌区。
    skill_area_override: dict[str, float] | None = None
    skill_area_label: str = "技能施放范围"
    continuity_state: Any = None
    diagnostic: dict[str, Any] | None = None


@dataclass(frozen=True)
class StrategyActionContext:
    marker: tuple[float, float] | None
    player_box: tuple[int, int, int, int] | None
    player_anchor: tuple[float, float] | None
    target_box: tuple[int, int, int, int] | None
    chase_box: tuple[int, int, int, int] | None
    combat_width: int
    has_monster_candidates: bool
    now: float
    last_target_seen: float
    last_pickup: float
    direction: str | None
    behavior: dict[str, Any]
    settings: dict[str, Any]
    recognition: dict[str, Any]
    combat_height: int = 1
    last_jump: float = 0.0
    last_jump_attack: float = 0.0
    last_periodic_step: float = 0.0
    periodic_step_pending_return: bool = False
    eligible_detections: tuple[Detection, ...] = ()
    previous_attack_skill: str | None = None
    # 运行层已确认唯一实时小地图标记；只能回安全位置或等待，不得攻击/追怪。
    minimap_only: bool = False
    started_at: float = 0.0
    runtime_state: dict[str, Any] = field(default_factory=dict)
    localization_lost_seconds: float = 0.0
    action_interrupted: bool = False
    # 公共攻击键供无方向策略显式构造 cast，不改变 cast 空键禁止发送的约定。
    default_attack_key: str = ""


@dataclass(frozen=True)
class StrategyDecision:
    action: Literal[
        "stop",
        "face",
        "attack",
        "cast",
        "chase",
        "move",
        "jump",
        "down_jump",
        "jump_attack",
        "pickup",
        "step",
    ]
    state: str
    direction: str | None = None
    target_x: float | None = None
    player_x: float | None = None
    target_seen: bool = False
    face_each_attack: bool = True
    face_tap_seconds: float | None = None
    close_overlap_ratio: float | None = None
    move_seconds: float | None = None
    periodic_step_return_complete: bool = False
    attack_key: str | None = None
    attack_skill: str | None = None
    # 会话状态由运行层保存，不在全局注册的策略实例中保存。
    runtime_state: dict[str, Any] | None = None
    reset_periodic_step: bool = False
    pickup_interval_seconds: float | None = None
    cooperative_movement: bool = False
    # 无方向施法的独立最短间隔，由公共执行器按实际发送时间限频。
    attack_interval_seconds: float | None = None
    # 默认仍从上次抬键完成计时；连续施法策略可显式选择按下起点。
    cast_interval_from_start: bool = False


class CombatStrategy(Protocol):
    key: str
    display_name: str
    profession: str
    description: str
    required_recognition_data: tuple[str, ...]
    setting_fields: tuple[StrategySettingField, ...]
    toggle_fields: tuple[StrategyToggleField, ...]
    choice_fields: tuple[StrategyChoiceField, ...]
    capture_fields: tuple[StrategyCaptureField, ...]
    default_settings: dict[str, Any]
    def select_targets(self, context: TargetSelectionContext) -> TargetSelection:
        ...

    def decide(self, context: StrategyActionContext) -> StrategyDecision:
        ...
