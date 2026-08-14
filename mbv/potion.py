from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class PotionAction:
    kind: Literal["hp", "mp"]
    key: str
    fill: float
    state: str


class AutoPotionController:
    """独立维护补药冷却和会话状态，不直接操作键盘或窗口。"""

    def __init__(self) -> None:
        self.standalone_enabled = False
        self.last_hp_at = float("-inf")
        self.last_mp_at = float("-inf")
        self.last_action = ""
        self.last_action_at = 0.0
        self.waiting_foreground = False
        self.unavailable_reason = ""

    def set_standalone_enabled(self, enabled: bool) -> None:
        if enabled and not self.standalone_enabled:
            self.last_action = ""
            self.last_action_at = 0.0
        self.standalone_enabled = bool(enabled)
        self.waiting_foreground = False
        self.unavailable_reason = ""

    def set_unavailable(self, reason: str) -> None:
        if self.standalone_enabled:
            self.unavailable_reason = str(reason).strip()
            self.waiting_foreground = False

    def decide(
        self,
        hp: float,
        mp: float,
        now: float,
        behavior: dict[str, Any],
        keys: dict[str, str],
    ) -> PotionAction | None:
        cooldown = max(0.0, float(behavior["potion_cooldown_seconds"]))
        if hp <= float(behavior["hp_threshold"]) and now - self.last_hp_at >= cooldown:
            return PotionAction("hp", str(keys["hp_potion"]), float(hp), "HP_POTION")
        if mp <= float(behavior["mp_threshold"]) and now - self.last_mp_at >= cooldown:
            return PotionAction("mp", str(keys["mp_potion"]), float(mp), "MP_POTION")
        return None

    def record(self, action: PotionAction, now: float) -> None:
        if action.kind == "hp":
            self.last_hp_at = now
        else:
            self.last_mp_at = now
        self.last_action = action.state
        self.last_action_at = now
        self.waiting_foreground = False

    def display_state(self, now: float) -> str:
        if not self.standalone_enabled:
            return "关闭"
        if self.unavailable_reason:
            return self.unavailable_reason
        if self.waiting_foreground:
            return "等待游戏前台"
        if self.last_action and now - self.last_action_at <= 0.8:
            return "正在回血" if self.last_action == "HP_POTION" else "正在回蓝"
        return "运行中"
