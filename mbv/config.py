from __future__ import annotations

from datetime import datetime
import json
import math
import time
from pathlib import Path
from typing import Any

from mbv.input import input_delivery
from mbv.paths import LOG_DIR, PLAYER_ASSET_DIR, PLAYER_HEAD_ASSET_DIR, PLAYER_TITLE_ASSET_DIR
from mbv.strategies import normalize_strategy_config
from mbv.template_store import list_monster_categories
from mbv.vision import MINIMAP_REGION_SPACE, attack_box_from_config

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
    config.setdefault("window", {})
    config["window"].setdefault("topmost_while_armed", True)
    performance_monitor = config.get("performance_monitor")
    if not isinstance(performance_monitor, dict):
        performance_monitor = {}
        config["performance_monitor"] = performance_monitor
    if not isinstance(performance_monitor.get("visible"), bool):
        performance_monitor["visible"] = True
    try:
        refresh_interval = float(performance_monitor.get("refresh_interval_seconds", 1.0))
    except (TypeError, ValueError):
        refresh_interval = 1.0
    if refresh_interval != refresh_interval:  # NaN
        refresh_interval = 1.0
    performance_monitor["refresh_interval_seconds"] = max(0.5, min(5.0, refresh_interval))
    config.setdefault("input", {})
    config["input"].setdefault("delivery", "foreground")
    input_delivery(config)
    config.setdefault("keys", {})
    config["keys"].setdefault("jump", "alt")
    config["keys"].setdefault("down", "down")
    config.setdefault("behavior", {})
    behavior = config["behavior"]
    if not isinstance(behavior.get("player_lost_recovery_enabled"), bool):
        behavior["player_lost_recovery_enabled"] = True
    raw_player_lost_move_seconds = behavior.get("player_lost_move_seconds", 0.25)
    try:
        if isinstance(raw_player_lost_move_seconds, bool):
            raise TypeError
        player_lost_move_seconds = float(raw_player_lost_move_seconds)
    except (TypeError, ValueError):
        player_lost_move_seconds = 0.25
    if not math.isfinite(player_lost_move_seconds):
        player_lost_move_seconds = 0.25
    behavior["player_lost_move_seconds"] = max(
        0.05,
        min(2.0, player_lost_move_seconds),
    )
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
    recognition = config["recognition"]
    recognition.setdefault("platform_center", {"x": 0.5, "y": 0.5})
    legacy_platform_center = recognition.get("platform_center_space") != "minimap"
    if legacy_platform_center:
        # 旧值相对战斗画面，不能无损换算成地图坐标；显式失效并要求重新采集。
        recognition["platform_center_captured"] = False
    else:
        recognition.setdefault("platform_center_captured", False)
    recognition["platform_center_space"] = "minimap"
    default_safe_output_area = {
        "x": 0.45,
        "y": 0.35,
        "w": 0.1,
        "h": 0.1,
        "space": MINIMAP_REGION_SPACE,
    }
    safe_output_area = recognition.get("throwing_star_safe_output_area")
    legacy_safe_output_area = not (
        isinstance(safe_output_area, dict)
        and safe_output_area.get("space") == MINIMAP_REGION_SPACE
    )
    if legacy_safe_output_area:
        # 旧值相对战斗画面，镜头滚动后会漂移，不能无损迁移到小地图。
        recognition["throwing_star_safe_output_area"] = default_safe_output_area
        recognition["throwing_star_safe_output_area_captured"] = False
    else:
        recognition.setdefault("throwing_star_safe_output_area_captured", False)
    calibration = config.setdefault("calibration", {})
    legacy_calibrated = bool(config.get("calibrated"))
    calibration.setdefault("status_regions_complete", legacy_calibrated)
    calibration.setdefault("recognition_region_complete", legacy_calibrated)
    items = calibration.setdefault("items", {})
    legacy_status_complete = bool(calibration["status_regions_complete"])
    legacy_recognition_complete = bool(calibration["recognition_region_complete"])
    for key in ("hp_bar", "mp_bar", "minimap", "player_marker"):
        items.setdefault(key, {"complete": legacy_status_complete})
    items.setdefault("combat_region", {"complete": legacy_recognition_complete})
    items.setdefault(
        "platform_center",
        {"complete": bool(recognition.get("platform_center_captured"))},
    )
    if legacy_platform_center:
        previous = items.get("platform_center", {})
        previous_timestamp = previous.get("timestamp") if isinstance(previous, dict) else None
        items["platform_center"] = {"complete": False}
        if previous_timestamp:
            items["platform_center"]["previous_timestamp"] = previous_timestamp
    items.setdefault("targeting_range", {"complete": legacy_calibrated})
    items.setdefault(
        "throwing_star_safe_output_area",
        {"complete": bool(config["recognition"].get("throwing_star_safe_output_area_captured"))},
    )
    if legacy_safe_output_area:
        previous = items.get("throwing_star_safe_output_area", {})
        previous_timestamp = previous.get("timestamp") if isinstance(previous, dict) else None
        items["throwing_star_safe_output_area"] = {"complete": False}
        if previous_timestamp:
            items["throwing_star_safe_output_area"]["previous_timestamp"] = previous_timestamp
    throwing_star_regions = config["strategy"]["options"]["throwing_star_safe"].get(
        "target_regions",
        [],
    )
    target_regions_complete = bool(
        isinstance(throwing_star_regions, list)
        and any(
            isinstance(item, dict) and bool(item.get("enabled", True))
            for item in throwing_star_regions
        )
    )
    target_regions_item = items.setdefault("throwing_star_target_regions", {})
    if not isinstance(target_regions_item, dict):
        items["throwing_star_target_regions"] = target_regions_item = {}
    target_regions_item["complete"] = target_regions_complete
    refresh_calibrated(config)
    config.setdefault("vision", {})
    monster_threshold = float(config["vision"].get("monster_template_threshold", 0.79))
    config["vision"].setdefault("active_monster_category", "")
    config["vision"].setdefault("monster_filter_threshold", max(monster_threshold, 0.84))
    config["vision"].setdefault("monster_filter_overlap", 0.5)
    config["vision"].setdefault("monster_structure_weight", 0.15)
    config["vision"].setdefault("player_anchor_smoothing_alpha", 0.25)
    config["vision"].setdefault("player_anchor_smoothing_snap", 0.08)
    config["vision"].setdefault("player_local_roi_width", 0.36)
    config["vision"].setdefault("player_local_roi_up", 0.24)
    config["vision"].setdefault("player_local_roi_down", 0.18)
    config["vision"].setdefault("player_local_miss_limit", 2)
    config["vision"].setdefault("player_global_verify_interval_seconds", 1.5)
    config["vision"].setdefault("player_prediction_horizon_seconds", 0.2)
    config["vision"].setdefault("player_velocity_alpha", 0.35)
    config["vision"].setdefault("player_name_identity_threshold", 0.50)
    config["vision"].setdefault("player_name_identity_margin", 0.08)
    config["vision"].setdefault("player_reacquire_confirm_frames", 2)
    config["vision"].setdefault("player_auxiliary_max_jump", 0.06)
    config["vision"].setdefault("player_auxiliary_identity_threshold", 0.90)
    config["vision"].setdefault("player_auxiliary_reacquire_confirm_frames", 3)
    if not isinstance(config["vision"].get("player_minimap_assist_enabled"), bool):
        config["vision"]["player_minimap_assist_enabled"] = True
    raw_minimap_assist_seconds = config["vision"].get("player_minimap_assist_max_seconds", 0.2)
    try:
        if isinstance(raw_minimap_assist_seconds, bool):
            raise TypeError
        minimap_assist_seconds = float(raw_minimap_assist_seconds)
    except (TypeError, ValueError):
        minimap_assist_seconds = 0.2
    if not math.isfinite(minimap_assist_seconds):
        minimap_assist_seconds = 0.2
    config["vision"]["player_minimap_assist_max_seconds"] = max(
        0.1,
        min(0.2, minimap_assist_seconds),
    )
    return config


def refresh_calibrated(config: dict[str, Any]) -> bool:
    calibration = config.setdefault("calibration", {})
    items = calibration.get("items", {})
    if isinstance(items, dict) and items:
        def item_complete(key: str) -> bool:
            value = items.get(key, {})
            if isinstance(value, dict):
                return bool(value.get("complete"))
            return bool(value)

        calibration["status_regions_complete"] = all(
            item_complete(key) for key in ("hp_bar", "mp_bar", "minimap", "player_marker")
        )
        calibration["recognition_region_complete"] = item_complete("combat_region")
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


def create_config_from_example(path: Path, example_path: Path) -> bool:
    """Create a personal config once without overwriting an existing one."""
    if path.exists():
        return False
    with example_path.open("r", encoding="utf-8") as handle:
        config = json.load(handle)
    if config.get("version") != 1:
        raise RuntimeError("示例配置文件版本无效")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as handle:
            json.dump(config, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    except FileExistsError:
        return False
    return True
