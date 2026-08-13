from __future__ import annotations

from datetime import datetime
import json
import time
from pathlib import Path
from typing import Any

from mbv.input import input_delivery
from mbv.paths import LOG_DIR, PLAYER_ASSET_DIR, PLAYER_HEAD_ASSET_DIR, PLAYER_TITLE_ASSET_DIR
from mbv.strategies import normalize_strategy_config
from mbv.template_store import list_monster_categories
from mbv.vision import attack_box_from_config

class SessionLog:
    def __init__(self) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.path = LOG_DIR / f"session-{stamp}.jsonl"

    def write(self, event: str, **data: Any) -> None:
        record = {"ts": time.time(), "event": event, **data}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def png_count(directory: Path) -> int:
    if not directory.is_dir():
        return 0
    return sum(1 for path in directory.glob("*.png"))


def template_counts() -> dict[str, int]:
    categories = list_monster_categories()
    return {
        "monster": sum(item.monster_count for item in categories),
        "filter": sum(item.filter_count for item in categories),
        "category": len(categories),
        "player": png_count(PLAYER_ASSET_DIR),
        "head": png_count(PLAYER_HEAD_ASSET_DIR),
        "title": png_count(PLAYER_TITLE_ASSET_DIR),
    }


def _normalized_target_box(value: Any) -> dict[str, float] | None:
    if not isinstance(value, dict):
        return None
    try:
        return {
            "forward": float(value["forward"]),
            "back": float(value["back"]),
            "up": float(value["up"]),
            "down": float(value["down"]),
        }
    except (KeyError, TypeError, ValueError):
        return None


def _resized_legacy_target_box(box: dict[str, float]) -> dict[str, float]:
    """最老配置迁移一次：沿用此前总宽 -10%、总高 +20% 的调整。"""
    return {
        "forward": round(float(box["forward"]) * 0.9, 6),
        "back": round(float(box["back"]) * 0.9, 6),
        "up": round(float(box["up"]) * 1.2, 6),
        "down": round(float(box["down"]) * 1.2, 6),
    }


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        config = json.load(handle)
    if config.get("version") != 1:
        raise RuntimeError("不支持的配置文件版本")
    config.setdefault("input", {})
    config["input"].setdefault("delivery", "foreground")
    input_delivery(config)
    config.setdefault("behavior", {})
    legacy_bow_attack_box = attack_box_from_config(config["behavior"])
    had_strategy_section = isinstance(config.get("strategy"), dict)
    old_strategy_box = None
    if had_strategy_section:
        options = config["strategy"].get("options", {})
        bowman_settings = options.get("bowman_dynamic", {}) if isinstance(options, dict) else {}
        old_strategy_box = _normalized_target_box(
            bowman_settings.get("attack_box") if isinstance(bowman_settings, dict) else None
        )
    config.setdefault("targeting", {})
    target_box = _normalized_target_box(config["targeting"].get("box"))
    if target_box is None:
        if old_strategy_box is not None:
            target_box = old_strategy_box
        elif had_strategy_section:
            target_box = legacy_bow_attack_box
        else:
            target_box = _resized_legacy_target_box(legacy_bow_attack_box)
    config["targeting"]["box"] = target_box
    normalize_strategy_config(config)
    # 旧字段只参与上面的迁移；运行时和后续保存统一使用 targeting.box。
    config["behavior"].pop("bow_attack_box", None)
    config["behavior"].pop("bow_attack_range", None)
    config["behavior"].pop("bow_vertical_tolerance", None)
    config.setdefault("recognition", {})
    config["recognition"].setdefault("platform_center", {"x": 0.5, "y": 0.5})
    config["recognition"].setdefault("platform_center_captured", False)
    calibration = config.setdefault("calibration", {})
    legacy_calibrated = bool(config.get("calibrated"))
    calibration.setdefault("status_regions_complete", legacy_calibrated)
    calibration.setdefault("recognition_region_complete", legacy_calibrated)
    config["calibrated"] = bool(
        calibration["status_regions_complete"] and calibration["recognition_region_complete"]
    )
    config.setdefault("vision", {})
    monster_threshold = float(config["vision"].get("monster_template_threshold", 0.79))
    config["vision"].setdefault("active_monster_category", "")
    config["vision"].setdefault("monster_filter_threshold", max(monster_threshold, 0.84))
    config["vision"].setdefault("monster_filter_overlap", 0.5)
    config["vision"].setdefault("player_anchor_smoothing_alpha", 0.25)
    config["vision"].setdefault("player_anchor_smoothing_snap", 0.08)
    return config


def refresh_calibrated(config: dict[str, Any]) -> bool:
    calibration = config.setdefault("calibration", {})
    complete = bool(
        calibration.get("status_regions_complete")
        and calibration.get("recognition_region_complete")
    )
    config["calibrated"] = complete
    return complete


def save_config(path: Path, config: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
