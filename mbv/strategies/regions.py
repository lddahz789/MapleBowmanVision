from __future__ import annotations

import math
from typing import Any

from mbv.vision import PLAYER_RELATIVE_REGION_SPACE


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
