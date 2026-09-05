from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any


BUFF_SLOT_KEYS = ("buff_1", "buff_2", "buff_3")
BUFF_PRE_CAST_SECONDS = 0.45
BUFF_KEY_HOLD_SECONDS = 0.18
BUFF_CAST_GUARD_SECONDS = 1.2


@dataclass(frozen=True)
class BuffAction:
    slot: str
    key: str
    interval_seconds: float


class AutoBuffController:
    """在挂机运行期间按三个独立间隔调度 Buff，不直接发送按键。"""

    def __init__(self) -> None:
        self.last_cast_at: dict[str, float] = {}
        self.last_action_at = float("-inf")

    def reset(self) -> None:
        self.last_cast_at.clear()
        self.last_action_at = float("-inf")

    def casting_guard_active(self, now: float) -> bool:
        """施放 Buff 后短暂停止其他动作，避免下一技能或普通攻击打断它。"""
        return now - self.last_action_at < BUFF_CAST_GUARD_SECONDS

    def decide(self, buffs: dict[str, Any], now: float) -> BuffAction | None:
        if self.casting_guard_active(now):
            return None
        due: list[tuple[float, BuffAction]] = []
        for slot in BUFF_SLOT_KEYS:
            item = buffs.get(slot, {})
            if not isinstance(item, dict):
                continue
            # 未带 enabled 的调用按旧配置兼容；经 load_config 归一化后始终是布尔值。
            if item.get("enabled") is False:
                continue
            key = str(item.get("key", "")).strip().casefold()
            try:
                interval = float(item.get("interval_seconds", 0.0))
            except (TypeError, ValueError):
                continue
            if not key or not math.isfinite(interval) or interval <= 0.0:
                continue
            last_cast = self.last_cast_at.get(slot, float("-inf"))
            if now - last_cast >= interval:
                due.append((last_cast + interval, BuffAction(slot, key, interval)))
        # 从未施放的项目先执行，同到期按槽位稳定排序；短间隔不能饿死后面的项目。
        return min(due, key=lambda item: item[0])[1] if due else None

    def record(self, action: BuffAction, now: float) -> None:
        self.last_cast_at[action.slot] = now
        self.last_action_at = now
