from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from mbv.vision import Detection


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


@dataclass(frozen=True)
class StrategyToggleField:
    """面板可切换并持久化的策略布尔参数。"""

    path: str
    label: str


@dataclass(frozen=True)
class StrategyCaptureField:
    """面板按策略生成的矩形采集入口。"""

    recognition_key: str
    button_label: str
    prompt: str
    debug_label: str
    enable_setting: str | None = None


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


@dataclass(frozen=True)
class TargetSelection:
    target: Detection | None
    chase_target: Detection | None


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


@dataclass(frozen=True)
class StrategyDecision:
    action: Literal["stop", "attack", "chase", "move", "jump", "jump_attack", "pickup"]
    state: str
    direction: str | None = None
    target_x: float | None = None
    player_x: float | None = None
    target_seen: bool = False
    face_each_attack: bool = True
    close_overlap_ratio: float | None = None


class CombatStrategy(Protocol):
    key: str
    display_name: str
    profession: str
    description: str
    required_recognition_data: tuple[str, ...]
    setting_fields: tuple[StrategySettingField, ...]
    toggle_fields: tuple[StrategyToggleField, ...]
    capture_fields: tuple[StrategyCaptureField, ...]
    default_settings: dict[str, Any]
    def select_targets(self, context: TargetSelectionContext) -> TargetSelection:
        ...

    def decide(self, context: StrategyActionContext) -> StrategyDecision:
        ...
