from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from mbv.vision import Detection


@dataclass(frozen=True)
class StrategySettingField:
    """面板可编辑的策略参数；path 相对于该策略的 settings。"""

    path: str
    label: str
    step: float | None = 0.01
    minimum: float | None = 0.0
    maximum: float | None = 1.0


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


@dataclass(frozen=True)
class StrategyDecision:
    action: Literal["stop", "attack", "chase", "move", "pickup"]
    state: str
    direction: str | None = None
    target_x: float | None = None
    player_x: float | None = None
    target_seen: bool = False
    face_each_attack: bool = True


class CombatStrategy(Protocol):
    key: str
    display_name: str
    profession: str
    description: str
    required_recognition_data: tuple[str, ...]
    setting_fields: tuple[StrategySettingField, ...]
    default_settings: dict[str, Any]
    def select_targets(self, context: TargetSelectionContext) -> TargetSelection:
        ...

    def decide(self, context: StrategyActionContext) -> StrategyDecision:
        ...
