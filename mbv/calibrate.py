from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
import mss
import numpy as np

from mbv.config import load_config, refresh_calibrated, save_config
from mbv.input import VK
from mbv.overlay import interactive_overlay
from mbv.strategies import list_strategies
from mbv.template_store import monster_template_directory, template_roots_from_config
from mbv.vision import (
    MINIMAP_REGION_SPACE,
    attack_box_from_rectangle,
    monster_template_image,
    nameplate_identity_mask,
    normalize_facing,
    normalized_roi,
    player_attack_anchor,
    player_marker,
    player_relative_region_from_rectangle,
    roi_pixels,
    template_foreground_mask,
)
from mbv.window import WindowInfo, capture_client, find_game_window, focus_game_window


STATUS_ITEM_KEYS = ("hp_bar", "mp_bar", "minimap", "player_marker")
WINDOW_ASPECT_RATIO_TOLERANCE = 0.03
MINIMAP_MARKER_MAX_ZOOM = 8.0
MINIMAP_MARKER_PREVIEW_TOP = 54
MINIMAP_MARKER_PREVIEW_MARGIN = 24


def _mark_calibration_item(config: dict[str, Any], key: str) -> None:
    calibration = config.setdefault("calibration", {})
    items = calibration.setdefault("items", {})
    items[key] = {
        "complete": True,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }


def _invalidate_calibration_item(config: dict[str, Any], key: str) -> None:
    calibration = config.setdefault("calibration", {})
    items = calibration.setdefault("items", {})
    previous = items.get(key, {})
    timestamp = previous.get("timestamp") if isinstance(previous, dict) else None
    items[key] = {"complete": False}
    if timestamp:
        items[key]["previous_timestamp"] = timestamp


def _strategy_settings_value(
    config: dict[str, Any],
    strategy_key: str,
    settings_path: str,
    value: Any | None = None,
) -> Any:
    settings = config["strategy"]["options"][strategy_key]
    parts = settings_path.split(".")
    cursor = settings
    for part in parts[:-1]:
        child = cursor.get(part)
        if not isinstance(child, dict):
            cursor[part] = child = {}
        cursor = child
    if value is None:
        return cursor.get(parts[-1])
    cursor[parts[-1]] = value
    return value


def _invalidate_strategy_combat_areas(config: dict[str, Any]) -> None:
    """按策略采集元数据失效所有依赖战斗识别区的策略列表，不写职业分支。"""
    for strategy in list_strategies():
        for field in strategy.capture_fields:
            if not field.settings_path:
                continue
            _strategy_settings_value(config, strategy.key, field.settings_path, [])
            _invalidate_calibration_item(config, field.recognition_key)


def _invalidate_combat_dependents(config: dict[str, Any]) -> None:
    _invalidate_calibration_item(config, "targeting_range")
    _invalidate_strategy_combat_areas(config)


def _prepare_window_calibration(config: dict[str, Any], window: WindowInfo) -> None:
    calibration = config.setdefault("calibration", {})
    previous = calibration.get("window_size")
    current = [window.width, window.height]
    if isinstance(previous, list) and len(previous) == 2 and list(previous) != current:
        previous_width, previous_height = (int(previous[0]), int(previous[1]))
        previous_ratio = previous_width / max(1, previous_height)
        current_ratio = window.width / max(1, window.height)
        aspect_ratio_drift = abs(current_ratio / max(previous_ratio, 1e-9) - 1.0)
        # Every captured rectangle/point is stored as normalized coordinates.
        # A proportional resize (including the control panel docking the game)
        # therefore must not erase already completed independent capture items.
        # Only a material aspect-ratio change can alter the game's layout enough
        # to make those normalized coordinates unsafe to reuse.
        if aspect_ratio_drift > WINDOW_ASPECT_RATIO_TOLERANCE:
            for key in (
                "hp_bar",
                "mp_bar",
                "minimap",
                "player_marker",
                "combat_region",
                "platform_center",
                "targeting_range",
                "throwing_star_safe_output_area",
            ):
                _invalidate_calibration_item(config, key)
            recognition = config.setdefault("recognition", {})
            recognition["platform_center_captured"] = False
            recognition["throwing_star_safe_output_area_captured"] = False
            _invalidate_strategy_combat_areas(config)
    calibration["window_size"] = current


def capture_status_region(
    config_path: Path,
    region_key: str,
    label: str,
    parent: Any = None,
) -> dict[str, float]:
    """独立框选一个状态区域；失败时不影响其它已采集项目。"""
    key = str(region_key).strip()
    if key not in {"hp_bar", "mp_bar", "minimap"}:
        raise ValueError(f"不支持的状态区域：{key}")
    config = load_config(config_path)
    window = find_game_window(config)
    _prepare_window_calibration(config, window)
    focus_game_window(window)
    shape = (window.height, window.width, 3)
    with mss.MSS() as sct:
        frozen_frame = capture_client(sct, window)
    result = interactive_overlay(
        window,
        f"框选{label}，回车确认",
        "rectangle",
        parent=parent,
        frozen_frame=frozen_frame,
    )
    if result.cancelled or result.rectangle is None:
        raise RuntimeError(f"已取消{label}采集")
    region = normalized_roi(result.rectangle, shape)
    config.setdefault("regions", {})[key] = region
    if key == "minimap":
        _invalidate_calibration_item(config, "player_marker")
        _invalidate_calibration_item(config, "platform_center")
        _invalidate_calibration_item(config, "throwing_star_safe_output_area")
        recognition = config.setdefault("recognition", {})
        recognition["platform_center_captured"] = False
        recognition["throwing_star_safe_output_area_captured"] = False
    _mark_calibration_item(config, key)
    calibration = config.setdefault("calibration", {})
    calibration["window_size"] = [window.width, window.height]
    refresh_calibrated(config)
    save_config(config_path, config)
    return region


def magnified_roi_preview(
    frame: np.ndarray,
    source_rect: tuple[int, int, int, int],
    *,
    max_zoom: float = MINIMAP_MARKER_MAX_ZOOM,
    top_inset: int = MINIMAP_MARKER_PREVIEW_TOP,
    margin: int = MINIMAP_MARKER_PREVIEW_MARGIN,
) -> tuple[np.ndarray, tuple[int, int, int, int], float]:
    """在同尺寸暗色画布中央放大 ROI，供冻结帧精确点选。"""
    frame_height, frame_width = frame.shape[:2]
    source_x, source_y, source_width, source_height = source_rect
    if source_width < 1 or source_height < 1:
        raise ValueError("放大采集区域尺寸无效")
    if not (
        0 <= source_x < frame_width
        and 0 <= source_y < frame_height
        and source_x + source_width <= frame_width
        and source_y + source_height <= frame_height
    ):
        raise ValueError("放大采集区域超出冻结画面")

    usable_width = max(1, frame_width - margin * 2)
    usable_height = max(1, frame_height - top_inset - margin)
    zoom = min(
        max(1.0, float(max_zoom)),
        usable_width / source_width,
        usable_height / source_height,
    )
    display_width = max(1, min(usable_width, int(round(source_width * zoom))))
    display_height = max(1, min(usable_height, int(round(source_height * zoom))))
    display_x = max(0, (frame_width - display_width) // 2)
    display_y = top_inset + max(0, (usable_height - display_height) // 2)

    preview = np.clip(frame.astype(np.float32) * 0.18, 0, 255).astype(np.uint8)
    source = frame[
        source_y : source_y + source_height,
        source_x : source_x + source_width,
    ]
    enlarged = cv2.resize(
        source,
        (display_width, display_height),
        interpolation=cv2.INTER_NEAREST,
    )
    preview[
        display_y : display_y + display_height,
        display_x : display_x + display_width,
    ] = enlarged
    return preview, (display_x, display_y, display_width, display_height), zoom


def map_magnified_point(
    point: tuple[int, int],
    display_rect: tuple[int, int, int, int],
    source_rect: tuple[int, int, int, int],
) -> tuple[int, int]:
    """把放大预览中的点映射回原始冻结帧像素。"""
    point_x, point_y = point
    display_x, display_y, display_width, display_height = display_rect
    source_x, source_y, source_width, source_height = source_rect
    if not (
        display_x <= point_x < display_x + display_width
        and display_y <= point_y < display_y + display_height
    ):
        raise ValueError("玩家标记采样点必须位于放大的小地图区域内")
    relative_x = min(
        source_width - 1,
        int((point_x - display_x) * source_width / max(1, display_width)),
    )
    relative_y = min(
        source_height - 1,
        int((point_y - display_y) * source_height / max(1, display_height)),
    )
    return source_x + relative_x, source_y + relative_y


def sample_player_marker_hsv(
    minimap: np.ndarray,
    point: tuple[int, int],
    radius: int = 3,
) -> tuple[int, int, int]:
    """只从点击附近同色且明亮的像素取样，避免小标记被地图背景中位数淹没。"""
    height, width = minimap.shape[:2]
    point_x, point_y = point
    if not (0 <= point_x < width and 0 <= point_y < height):
        raise RuntimeError("玩家标记取样点超出小地图")
    hsv = cv2.cvtColor(minimap, cv2.COLOR_BGR2HSV)
    sample_radius = max(1, int(radius))
    left = max(0, point_x - sample_radius)
    top = max(0, point_y - sample_radius)
    right = min(width, point_x + sample_radius + 1)
    bottom = min(height, point_y + sample_radius + 1)
    patch = hsv[top:bottom, left:right]
    seed_hue, seed_saturation, seed_value = (int(value) for value in hsv[point_y, point_x])
    hue_delta = np.abs(patch[:, :, 0].astype(np.int16) - seed_hue)
    hue_delta = np.minimum(hue_delta, 180 - hue_delta)
    value_floor = max(120, int(np.percentile(patch[:, :, 2], 75)))
    saturation_floor = max(30, seed_saturation - 120)
    selected = (
        (hue_delta <= 10)
        & (patch[:, :, 1] >= saturation_floor)
        & (patch[:, :, 2] >= value_floor)
    )
    pixels = patch[selected]
    if pixels.shape[0] < 2:
        pixels = np.asarray([[seed_hue, seed_saturation, seed_value]], dtype=np.uint8)

    hue_offsets = (pixels[:, 0].astype(np.int16) - seed_hue + 90) % 180 - 90
    hue = int(round((seed_hue + float(np.median(hue_offsets))) % 180))
    saturation = int(round(float(np.median(pixels[:, 1]))))
    value = int(round(float(np.median(pixels[:, 2]))))
    return hue, saturation, value


def analyze_player_marker_sample(
    minimap: np.ndarray,
    point: tuple[int, int],
    min_area: int,
    max_area: int,
) -> tuple[tuple[int, int, int], list[dict[str, list[int]]], tuple[float, float]]:
    """生成颜色范围并在同一冻结小地图上验证它只命中点击处。"""
    hue, saturation, value = sample_player_marker_hsv(minimap, point)
    ranges = hue_ranges(hue, saturation, value)
    height, width = minimap.shape[:2]
    clicked = (point[0] / max(1, width), point[1] / max(1, height))
    detected, mask = player_marker(minimap, ranges, min_area, max_area, clicked)
    count, _labels, stats, centers = cv2.connectedComponentsWithStats(mask, 8)
    candidates = [
        centers[index]
        for index in range(1, count)
        if min_area <= int(stats[index, cv2.CC_STAT_AREA]) <= max_area
    ]
    if detected is None:
        raise RuntimeError("点击位置没有形成可识别的玩家标记，请点在标记最亮的中心像素")
    distance = float(
        np.hypot(detected[0] * width - point[0], detected[1] * height - point[1])
    )
    if distance > max(3.0, min(width, height) * 0.04):
        raise RuntimeError("取样结果偏离点击位置，请重新点击玩家标记中心")
    if len(candidates) > 1:
        raise RuntimeError("该颜色在小地图中命中多个位置，请点击玩家标记更明亮的中心像素")
    return (hue, saturation, value), ranges, detected


def capture_player_marker(config_path: Path, parent: Any = None) -> tuple[int, int, int]:
    """放大小地图后独立取玩家标记颜色，并映射回同一冻结帧取样。"""
    config = load_config(config_path)
    window = find_game_window(config)
    _prepare_window_calibration(config, window)
    focus_game_window(window)
    shape = (window.height, window.width, 3)
    minimap_rect = roi_pixels(shape, config["regions"]["minimap"])
    with mss.MSS() as sct:
        frozen_frame = capture_client(sct, window)
    preview_frame, preview_rect, zoom = magnified_roi_preview(frozen_frame, minimap_rect)
    result = interactive_overlay(
        window,
        f"小地图已放大 {zoom:.1f} 倍，点击自己的玩家标记中心",
        "point",
        guide_rect=preview_rect,
        parent=parent,
        frozen_frame=preview_frame,
    )
    if result.cancelled or result.point is None:
        raise RuntimeError("已取消小地图玩家标记采集")
    try:
        px, py = map_magnified_point(result.point, preview_rect, minimap_rect)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    mx, my, mw, mh = minimap_rect
    minimap = frozen_frame[my : my + mh, mx : mx + mw]
    sample, ranges, marker_position = analyze_player_marker_sample(
        minimap,
        (px - mx, py - my),
        int(config["vision"].get("player_blob_min_area", 2)),
        int(config["vision"].get("player_blob_max_area", 180)),
    )
    hue, saturation, value = sample
    config.setdefault("vision", {})["player_hsv_ranges"] = ranges
    _mark_calibration_item(config, "player_marker")
    calibration = config.setdefault("calibration", {})
    calibration["player_hsv_sample"] = [hue, saturation, value]
    calibration["player_marker_position"] = [
        round(marker_position[0], 6),
        round(marker_position[1], 6),
    ]
    calibration["window_size"] = [window.width, window.height]
    refresh_calibrated(config)
    save_config(config_path, config)
    return hue, saturation, value


def capture_combat_region(config_path: Path, parent: Any = None) -> dict[str, float]:
    """独立采集战斗识别区，不强制同时采集平台中心。"""
    config = load_config(config_path)
    window = find_game_window(config)
    _prepare_window_calibration(config, window)
    focus_game_window(window)
    shape = (window.height, window.width, 3)
    with mss.MSS() as sct:
        frozen_frame = capture_client(sct, window)
    result = interactive_overlay(
        window,
        "框选整个可见战斗识别区（排除底部状态栏），回车确认",
        "rectangle",
        parent=parent,
        frozen_frame=frozen_frame,
    )
    if result.cancelled or result.rectangle is None:
        raise RuntimeError("已取消战斗识别区域采集")
    region = normalized_roi(result.rectangle, shape)
    config.setdefault("regions", {})["combat"] = region
    _invalidate_combat_dependents(config)
    _mark_calibration_item(config, "combat_region")
    calibration = config.setdefault("calibration", {})
    calibration["window_size"] = [window.width, window.height]
    refresh_calibrated(config)
    save_config(config_path, config)
    return region


def capture_platform_center(config_path: Path, parent: Any = None) -> dict[str, float]:
    """放大小地图并独立记录目标平台的安全中心点。"""
    config = load_config(config_path)
    window = find_game_window(config)
    _prepare_window_calibration(config, window)
    focus_game_window(window)
    shape = (window.height, window.width, 3)
    minimap_rect = roi_pixels(shape, config["regions"]["minimap"])
    with mss.MSS() as sct:
        frozen_frame = capture_client(sct, window)
    preview_frame, preview_rect, zoom = magnified_roi_preview(frozen_frame, minimap_rect)
    result = interactive_overlay(
        window,
        f"小地图已放大 {zoom:.1f} 倍，点击目标平台的安全中心点",
        "point",
        guide_rect=preview_rect,
        parent=parent,
        frozen_frame=preview_frame,
    )
    if result.cancelled or result.point is None:
        raise RuntimeError("已取消平台中心采集")
    try:
        px, py = map_magnified_point(result.point, preview_rect, minimap_rect)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    mx, my, mw, mh = minimap_rect
    center = {
        "x": round(max(0.0, min(1.0, (px - mx) / max(1, mw))), 6),
        "y": round(max(0.0, min(1.0, (py - my) / max(1, mh))), 6),
    }
    recognition = config.setdefault("recognition", {})
    recognition["platform_center"] = center
    recognition["platform_center_space"] = "minimap"
    recognition["platform_center_captured"] = True
    _mark_calibration_item(config, "platform_center")
    refresh_calibrated(config)
    save_config(config_path, config)
    return center

def hue_ranges(hue: int, saturation: int, value: int) -> list[dict[str, list[int]]]:
    delta_h = 9
    # 黄色玩家点同时包含低饱和中心和高饱和描边；上限必须覆盖 255，
    # 下限则限制在 110 以内，避免只采到某一种抗锯齿像素后下一帧丢失。
    low_s = max(30, min(110, saturation - 70))
    low_v = max(35, value - 70)
    high_s = 255
    high_v = min(255, value + 70)
    low_h = hue - delta_h
    high_h = hue + delta_h
    if low_h < 0:
        return [
            {"lower": [0, low_s, low_v], "upper": [high_h, high_s, high_v]},
            {"lower": [180 + low_h, low_s, low_v], "upper": [179, high_s, high_v]},
        ]
    if high_h > 179:
        return [
            {"lower": [low_h, low_s, low_v], "upper": [179, high_s, high_v]},
            {"lower": [0, low_s, low_v], "upper": [high_h - 180, high_s, high_v]},
        ]
    return [{"lower": [low_h, low_s, low_v], "upper": [high_h, high_s, high_v]}]


def hsv_median_at(frame: np.ndarray, x: int, y: int, radius: int = 2) -> tuple[int, int, int]:
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    patch = hsv[max(0, y - radius) : y + radius + 1, max(0, x - radius) : x + radius + 1]
    if patch.size == 0:
        raise RuntimeError("取样点超出画面")
    median = np.median(patch.reshape(-1, 3), axis=0).astype(int)
    return int(median[0]), int(median[1]), int(median[2])


def calibrate(config_path: Path, parent: Any = None) -> None:
    """采集状态栏和小地图；战斗识别区由 capture_recognition_region 独立采集。"""
    config = load_config(config_path)
    window = find_game_window(config)
    _prepare_window_calibration(config, window)
    print(f"正在校准状态栏与小地图：{window.title}（{window.width}×{window.height}）")
    print("提示会直接叠加在游戏画面上。拖动框选后按回车或空格确认。")
    focus_game_window(window)
    shape = (window.height, window.width, 3)

    def choose_rectangle(title: str) -> dict[str, float]:
        result = interactive_overlay(window, title, "rectangle", parent=parent)
        if result.cancelled or result.rectangle is None:
            raise RuntimeError(f"已取消校准：{title}")
        return normalized_roi(result.rectangle, shape)

    config["regions"]["hp_bar"] = choose_rectangle("第 1 步：框选血条")
    config["regions"]["mp_bar"] = choose_rectangle("第 2 步：框选蓝条")
    config["regions"]["minimap"] = choose_rectangle("第 3 步：框选整个小地图内部画面")
    minimap_rect = roi_pixels(shape, config["regions"]["minimap"])
    focus_game_window(window, settle_seconds=0.15)
    with mss.MSS() as sct:
        frame = capture_client(sct, window)
    preview_frame, preview_rect, zoom = magnified_roi_preview(frame, minimap_rect)
    point_result = interactive_overlay(
        window,
        f"第 4 步：小地图已放大 {zoom:.1f} 倍，点击自己的玩家标记中心",
        "point",
        preview_rect,
        parent=parent,
        frozen_frame=preview_frame,
    )
    if point_result.cancelled or point_result.point is None:
        raise RuntimeError("已取消玩家标记颜色选择")
    try:
        px, py = map_magnified_point(point_result.point, preview_rect, minimap_rect)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    mx, my, mw, mh = minimap_rect
    minimap = frame[my : my + mh, mx : mx + mw]
    sample, ranges, marker_position = analyze_player_marker_sample(
        minimap,
        (px - mx, py - my),
        int(config["vision"].get("player_blob_min_area", 2)),
        int(config["vision"].get("player_blob_max_area", 180)),
    )
    hue, saturation, value = sample
    config["vision"]["player_hsv_ranges"] = ranges
    calibration = config.setdefault("calibration", {})
    _invalidate_calibration_item(config, "platform_center")
    _invalidate_calibration_item(config, "throwing_star_safe_output_area")
    config.setdefault("recognition", {})["platform_center_captured"] = False
    config["recognition"]["throwing_star_safe_output_area_captured"] = False
    for key in STATUS_ITEM_KEYS:
        _mark_calibration_item(config, key)
    calibration.update(
        {
            "status_regions_complete": True,
            "window_size": [window.width, window.height],
            "status_timestamp": datetime.now().isoformat(timespec="seconds"),
            "player_hsv_sample": [hue, saturation, value],
            "player_marker_position": [
                round(marker_position[0], 6),
                round(marker_position[1], 6),
            ],
        }
    )
    refresh_calibrated(config)
    save_config(config_path, config)
    print(f"状态栏与小地图校准完成，配置已保存到：{config_path}")


def capture_recognition_region(config_path: Path, parent: Any = None) -> dict[str, float]:
    """兼容入口：采集战斗识别区，并在放大的小地图上记录平台安全点。"""
    config = load_config(config_path)
    window = find_game_window(config)
    _prepare_window_calibration(config, window)
    focus_game_window(window)
    shape = (window.height, window.width, 3)
    with mss.MSS() as sct:
        frozen_frame = capture_client(sct, window)
    region_result = interactive_overlay(
        window,
        "第 1 步：框选整个可见战斗识别区（排除底部状态栏）",
        "rectangle",
        parent=parent,
        frozen_frame=frozen_frame,
    )
    if region_result.cancelled or region_result.rectangle is None:
        raise RuntimeError("已取消识别区域采集")
    combat_roi = normalized_roi(region_result.rectangle, shape)
    minimap_rect = roi_pixels(shape, config["regions"]["minimap"])
    preview_frame, preview_rect, zoom = magnified_roi_preview(frozen_frame, minimap_rect)
    center_result = interactive_overlay(
        window,
        f"第 2 步：小地图已放大 {zoom:.1f} 倍，点击目标平台的安全中心点",
        "point",
        guide_rect=preview_rect,
        parent=parent,
        frozen_frame=preview_frame,
    )
    if center_result.cancelled or center_result.point is None:
        raise RuntimeError("已取消平台中心采集")
    try:
        px, py = map_magnified_point(center_result.point, preview_rect, minimap_rect)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    mx, my, mw, mh = minimap_rect
    platform_center = {
        "x": round(max(0.0, min(1.0, (px - mx) / max(1, mw))), 6),
        "y": round(max(0.0, min(1.0, (py - my) / max(1, mh))), 6),
    }
    config["regions"]["combat"] = combat_roi
    _invalidate_combat_dependents(config)
    config.setdefault("recognition", {})["platform_center"] = platform_center
    config["recognition"]["platform_center_space"] = "minimap"
    config["recognition"]["platform_center_captured"] = True
    _mark_calibration_item(config, "combat_region")
    _mark_calibration_item(config, "platform_center")
    calibration = config.setdefault("calibration", {})
    calibration.update(
        {
            "recognition_region_complete": True,
            "window_size": [window.width, window.height],
            "recognition_timestamp": datetime.now().isoformat(timespec="seconds"),
        }
    )
    refresh_calibrated(config)
    save_config(config_path, config)
    print(
        f"识别区域与小地图平台安全点已保存：x={platform_center['x']:.3f}, "
        f"y={platform_center['y']:.3f}（相对小地图）"
    )
    return platform_center


def capture_strategy_area(
    config_path: Path,
    recognition_key: str,
    prompt: str,
    parent: Any = None,
    coordinate_space: str = "combat",
) -> dict[str, Any]:
    """在战斗画面或放大小地图上框选并保存归一化策略矩形。"""
    key = str(recognition_key).strip()
    if not key or not key.replace("_", "").isalnum():
        raise ValueError("策略采集键无效")
    config = load_config(config_path)
    window = find_game_window(config)
    _prepare_window_calibration(config, window)
    focus_game_window(window)
    shape = (window.height, window.width, 3)
    with mss.MSS() as sct:
        frozen_frame = capture_client(sct, window)
    space = str(coordinate_space).strip().lower()
    if space == MINIMAP_REGION_SPACE:
        source_rect = roi_pixels(shape, config["regions"]["minimap"])
        display_frame, display_rect, zoom = magnified_roi_preview(frozen_frame, source_rect)
        overlay_prompt = f"小地图已放大 {zoom:.1f} 倍，{prompt}"
    elif space == "combat":
        display_frame = frozen_frame
        display_rect = roi_pixels(shape, config["regions"]["combat"])
        overlay_prompt = prompt
    else:
        raise ValueError(f"不支持的策略采集坐标空间：{coordinate_space}")
    result = interactive_overlay(
        window,
        overlay_prompt,
        "rectangle",
        guide_rect=display_rect,
        parent=parent,
        frozen_frame=display_frame,
    )
    if result.cancelled or result.rectangle is None:
        raise RuntimeError("已取消安全输出位置框选")
    rx, ry, rw, rh = result.rectangle
    dx, dy, dw, dh = display_rect
    if rx < dx or ry < dy or rx + rw > dx + dw or ry + rh > dy + dh:
        location = "放大的小地图" if space == MINIMAP_REGION_SPACE else "战斗识别区域"
        raise RuntimeError(f"安全输出位置必须完整位于{location}内")
    area = {
        "x": round((rx - dx) / max(1, dw), 6),
        "y": round((ry - dy) / max(1, dh), 6),
        "w": round(rw / max(1, dw), 6),
        "h": round(rh / max(1, dh), 6),
    }
    if space == MINIMAP_REGION_SPACE:
        area["space"] = MINIMAP_REGION_SPACE
    recognition = config.setdefault("recognition", {})
    recognition[key] = area
    recognition[f"{key}_captured"] = True
    _mark_calibration_item(config, key)
    refresh_calibrated(config)
    save_config(config_path, config)
    print(
        f"安全输出位置已保存：x={area['x']:.3f}, y={area['y']:.3f}, "
        f"w={area['w']:.3f}, h={area['h']:.3f}"
        f"（相对{'小地图' if space == MINIMAP_REGION_SPACE else '战斗识别区'}）"
    )
    return area


def capture_strategy_region(
    config_path: Path,
    strategy_key: str,
    settings_path: str,
    calibration_key: str,
    prompt: str,
    parent: Any = None,
    region_id: str | None = None,
    player_box: tuple[int, int, int, int] | None = None,
    raw_box: tuple[int, int, int, int] | None = None,
    player_anchor: tuple[float, float] | None = None,
) -> dict[str, Any]:
    """新增或重框一个跟随稳定战斗锚点、但不随面向翻转的策略矩形。"""
    key = str(strategy_key).strip()
    path = str(settings_path).strip()
    item_key = str(calibration_key).strip()
    if not key or not path or not item_key:
        raise ValueError("策略索敌区配置无效")
    if player_anchor is None and player_box is None:
        raise RuntimeError("尚未识别到角色，请先让姓名板出现在画面上再框选标飞索敌区")
    config = load_config(config_path)
    window = find_game_window(config)
    _prepare_window_calibration(config, window)
    focus_game_window(window)
    shape = (window.height, window.width, 3)
    combat_rect = roi_pixels(shape, config["regions"]["combat"])
    with mss.MSS() as sct:
        frozen_frame = capture_client(sct, window)
    result = interactive_overlay(
        window,
        prompt,
        "rectangle",
        guide_rect=combat_rect,
        parent=parent,
        frozen_frame=frozen_frame,
    )
    if result.cancelled or result.rectangle is None:
        raise RuntimeError("已取消标飞索敌区框选")
    rx, ry, rw, rh = result.rectangle
    cx, cy, cw, ch = combat_rect
    if rx < cx or ry < cy or rx + rw > cx + cw or ry + rh > cy + ch:
        raise RuntimeError("标飞索敌区必须完整位于战斗识别区域内")
    if player_anchor is not None:
        anchor = player_anchor
    elif player_box is not None:
        anchor = player_attack_anchor(player_box, raw_box)
    else:  # 已在打开采集窗口前拦截；保留分支帮助类型检查器收窄。
        raise RuntimeError("尚未识别到角色")
    area = player_relative_region_from_rectangle(
        result.rectangle,
        combat_rect,
        anchor,
    )
    current = _strategy_settings_value(config, key, path)
    regions = [dict(item) for item in current if isinstance(item, dict)] if isinstance(current, list) else []
    selected_id = str(region_id or "").strip()
    existing = next((item for item in regions if str(item.get("id")) == selected_id), None)
    if existing is not None:
        existing.update(area)
        saved = existing
    else:
        used_ids = {str(item.get("id", "")) for item in regions}
        suffix = len(regions) + 1
        selected_id = f"region_{suffix}"
        while selected_id in used_ids:
            suffix += 1
            selected_id = f"region_{suffix}"
        priorities = []
        for item in regions:
            try:
                priorities.append(int(item.get("priority", 0)))
            except (TypeError, ValueError):
                pass
        saved = {
            "id": selected_id,
            "name": f"索敌区 {suffix}",
            "enabled": True,
            "priority": max(priorities, default=0) + 1,
            **area,
        }
        regions.append(saved)
    _strategy_settings_value(config, key, path, regions)
    _mark_calibration_item(config, item_key)
    refresh_calibrated(config)
    save_config(config_path, config)
    print(
        f"标飞索敌区已保存：{saved['name']}，"
        f"相对角色 x={saved['offset_x']:.3f}, y={saved['offset_y']:.3f}, "
        f"w={saved['w']:.3f}, h={saved['h']:.3f}"
    )
    return saved


def _capture_frozen_selection_details(
    window: WindowInfo,
    title: str,
    cancel_message: str,
    parent: Any = None,
) -> tuple[np.ndarray, np.ndarray, tuple[int, int, int, int]]:
    """截图一次并在该静态帧上框选，保证显示内容与保存内容完全一致。"""
    focus_game_window(window)
    with mss.MSS() as sct:
        frame = capture_client(sct, window)
    result = interactive_overlay(
        window,
        title,
        "rectangle",
        parent=parent,
        frozen_frame=frame,
    )
    if result.cancelled or result.rectangle is None:
        raise RuntimeError(cancel_message)
    x, y, w, h = result.rectangle
    frame_height, frame_width = frame.shape[:2]
    left = max(0, x)
    top = max(0, y)
    right = min(frame_width, x + w)
    bottom = min(frame_height, y + h)
    if right - left < 4 or bottom - top < 4:
        raise RuntimeError("静态帧框选区域过小，请重新采集")
    rectangle = (left, top, right - left, bottom - top)
    return frame[top:bottom, left:right].copy(), frame, rectangle


def capture_frozen_selection(
    window: WindowInfo,
    title: str,
    cancel_message: str,
    parent: Any = None,
) -> np.ndarray:
    image, _frame, _rectangle = _capture_frozen_selection_details(
        window,
        title,
        cancel_message,
        parent=parent,
    )
    return image


def _capture_player_template_with_anchor(
    window: WindowInfo,
    title: str,
    cancel_message: str,
    parent: Any = None,
) -> tuple[np.ndarray, tuple[float, float]]:
    image, frame, rectangle = _capture_frozen_selection_details(
        window,
        title,
        cancel_message,
        parent=parent,
    )
    result = interactive_overlay(
        window,
        "在同一静态帧上点击自己双脚落点的中心，回车确认",
        "point",
        guide_rect=rectangle,
        parent=parent,
        frozen_frame=frame,
    )
    if result.cancelled or result.point is None:
        raise RuntimeError("已取消角色脚底锚点采集")
    left, top, _width, _height = rectangle
    return image, (float(result.point[0] - left), float(result.point[1] - top))


def _save_captured_template(
    image: np.ndarray,
    directory: Path,
    prefix: str,
    label: str,
    anchor_offset: tuple[float, float] | None = None,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    stem = f"{prefix}-{stamp}"
    path = directory / f"{stem}.png"
    sequence = 2
    while path.exists():
        path = directory / f"{stem}-{sequence}.png"
        sequence += 1
    ok, encoded = cv2.imencode(".png", image)
    if not ok:
        raise RuntimeError(f"无法保存{label}模板图片")
    encoded.tofile(path)
    if anchor_offset is not None:
        metadata = {
            "version": 1,
            "anchor_offset": [float(anchor_offset[0]), float(anchor_offset[1])],
        }
        try:
            path.with_suffix(".anchor.json").write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            path.unlink(missing_ok=True)
            raise
    print(f"{label}模板已保存：{path}")
    return path


def capture_template(config_path: Path, parent: Any = None, category: str = "") -> Path:
    config = load_config(config_path)
    roots = template_roots_from_config(config)
    window = find_game_window(config)
    image = capture_frozen_selection(
        window,
        "框选一只清晰、无遮挡的怪物，四周保留一些背景；程序会自动分离并紧裁主体",
        "已取消怪物模板框选",
        parent=parent,
    )
    bgra = monster_template_image(image)
    directory = monster_template_directory(
        category,
        "monster",
        monster_root=roots.monster,
        filter_root=roots.filter,
        create=True,
    )
    return _save_captured_template(bgra, directory, "monster", "怪物")


def capture_monster_filter(config_path: Path, parent: Any = None, category: str = "") -> Path:
    config = load_config(config_path)
    roots = template_roots_from_config(config)
    window = find_game_window(config)
    image = capture_frozen_selection(
        window,
        "紧贴框选不应被识别为怪物的场景物体或特效",
        "已取消过滤项框选",
        parent=parent,
    )
    # 过滤项需要保留完整矩形结构；透明度全满可阻止加载器套用怪物前景蒙版。
    bgra = cv2.cvtColor(image, cv2.COLOR_BGR2BGRA)
    bgra[:, :, 3] = 255
    directory = monster_template_directory(
        category,
        "filter",
        monster_root=roots.monster,
        filter_root=roots.filter,
        create=True,
    )
    return _save_captured_template(bgra, directory, "filter", "怪物过滤项")


def player_template_alpha(image: np.ndarray) -> np.ndarray:
    """用紧框四周作为背景种子，生成玩家模板的前景透明蒙版。"""
    height, width = image.shape[:2]
    if height < 8 or width < 8:
        return np.full((height, width), 255, dtype=np.uint8)
    grab_mask = np.full((height, width), cv2.GC_PR_BGD, dtype=np.uint8)
    margin = max(1, min(4, min(width, height) // 8))
    grab_mask[:margin, :] = cv2.GC_BGD
    grab_mask[-margin:, :] = cv2.GC_BGD
    grab_mask[:, :margin] = cv2.GC_BGD
    grab_mask[:, -margin:] = cv2.GC_BGD
    center_x1, center_x2 = width // 4, max(width // 4 + 1, width * 3 // 4)
    center_y1, center_y2 = height // 8, max(height // 8 + 1, height * 7 // 8)
    grab_mask[center_y1:center_y2, center_x1:center_x2] = cv2.GC_PR_FGD
    background_model = np.zeros((1, 65), dtype=np.float64)
    foreground_model = np.zeros((1, 65), dtype=np.float64)
    try:
        cv2.grabCut(image, grab_mask, None, background_model, foreground_model, 4, cv2.GC_INIT_WITH_MASK)
        alpha = np.where(
            (grab_mask == cv2.GC_FGD) | (grab_mask == cv2.GC_PR_FGD),
            255,
            0,
        ).astype(np.uint8)
    except cv2.error:
        alpha = template_foreground_mask(image)
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    hue, saturation, value = cv2.split(hsv)
    # 玩家红发、绿色服装和武器颜色与本地图的棕色木板明显不同；
    # 只在 GrabCut 前景附近补回这些高饱和像素，避免头发被误判成背景。
    distinctive = (
        (saturation >= 55)
        & (value >= 30)
        & ((hue <= 8) | (hue >= 26))
    ).astype(np.uint8) * 255
    nearby = cv2.dilate(alpha, np.ones((21, 21), np.uint8))
    alpha = cv2.bitwise_or(alpha, cv2.bitwise_and(distinctive, nearby))
    alpha = cv2.morphologyEx(alpha, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    if int(np.count_nonzero(alpha)) < width * height * 0.06:
        return template_foreground_mask(image)
    return alpha


def nameplate_template_alpha(image: np.ndarray) -> np.ndarray:
    """姓名板应紧框采集；只去掉最外圈，保留文字、边框和底色结构。"""
    height, width = image.shape[:2]
    alpha = np.full((height, width), 255, dtype=np.uint8)
    margin = max(1, min(3, min(width, height) // 10))
    alpha[:margin, :] = 0
    alpha[-margin:, :] = 0
    alpha[:, :margin] = 0
    alpha[:, -margin:] = 0
    return alpha


def capture_player_template(config_path: Path, parent: Any = None) -> Path:
    config = load_config(config_path)
    roots = template_roots_from_config(config)
    window = find_game_window(config)
    image, anchor_offset = _capture_player_template_with_anchor(
        window,
        "紧贴框选自己姓名板的第一行蓝色板（含名字），不要包含角色、宠物或怪物",
        "已取消玩家模板框选",
        parent=parent,
    )
    alpha = nameplate_template_alpha(image)
    if int(np.count_nonzero(nameplate_identity_mask(image, alpha))) < 6:
        raise RuntimeError("姓名板中没有提取到足够的名字字形，请紧贴第一行蓝色姓名板重新框选")
    bgra = cv2.cvtColor(image, cv2.COLOR_BGR2BGRA)
    bgra[:, :, 3] = alpha
    return _save_captured_template(
        bgra,
        roots.player,
        "nameplate",
        "玩家姓名板",
        anchor_offset=anchor_offset,
    )


def capture_player_aux_template(config_path: Path, kind: str, parent: Any = None) -> Path:
    if kind not in {"head", "title"}:
        raise ValueError(f"未知辅助模板类型：{kind}")
    config = load_config(config_path)
    roots = template_roots_from_config(config)
    specs = {
        "head": (
            "紧贴框选自己的头部（头发和脸），不要包含身体、姓名板或其他玩家",
            roots.head,
            "head",
            "玩家头部",
        ),
        "title": (
            "紧贴框选姓名板下方的称号勋章整行，不要包含姓名板、宠物或怪物",
            roots.title,
            "title",
            "玩家称号勋章",
        ),
    }
    prompt, directory, prefix, label = specs[kind]
    window = find_game_window(config)
    image, anchor_offset = _capture_player_template_with_anchor(
        window,
        prompt,
        f"已取消{label}模板框选",
        parent=parent,
    )
    alpha = player_template_alpha(image) if kind == "head" else nameplate_template_alpha(image)
    bgra = cv2.cvtColor(image, cv2.COLOR_BGR2BGRA)
    bgra[:, :, 3] = alpha
    return _save_captured_template(bgra, directory, prefix, label, anchor_offset=anchor_offset)


def capture_key_name(config: dict[str, Any], parent: Any = None) -> str:
    window = find_game_window(config)
    focus_game_window(window, settle_seconds=0.2)
    result = interactive_overlay(
        window,
        "请按下要绑定的键，Esc 取消",
        "key",
        parent=parent,
        vk_map=dict(VK),
    )
    if result.cancelled or not result.key:
        raise RuntimeError("已取消按键采集")
    return result.key


def capture_target_range(
    config_path: Path,
    parent: Any = None,
    player_box: tuple[int, int, int, int] | None = None,
    raw_box: tuple[int, int, int, int] | None = None,
    player_anchor: tuple[float, float] | None = None,
    facing: str | None = None,
) -> dict[str, float]:
    if player_box is None:
        raise RuntimeError("尚未识别到角色，请先让姓名板出现在画面上再框选攻击范围")
    config = load_config(config_path)
    window = find_game_window(config)
    _prepare_window_calibration(config, window)
    focus_game_window(window)
    shape = (window.height, window.width, 3)
    combat_rect = roi_pixels(shape, config["regions"]["combat"])
    result = interactive_overlay(
        window,
        "以角色位置和面向拖框，框多大就是通用索敌区多大，回车确认",
        "rectangle",
        guide_rect=combat_rect,
        parent=parent,
    )
    if result.cancelled or result.rectangle is None:
        raise RuntimeError("已取消索敌范围框选")
    attack_box = attack_box_from_rectangle(
        result.rectangle,
        combat_rect,
        player_box,
        raw_box=raw_box,
        facing=facing,
        player_anchor=player_anchor or player_attack_anchor(player_box, raw_box),
    )
    config.setdefault("targeting", {})["box"] = attack_box
    _mark_calibration_item(config, "targeting_range")
    refresh_calibrated(config)
    save_config(config_path, config)
    print(
        "通用索敌范围已保存："
        f"前 {attack_box['forward']:.4f}，后 {attack_box['back']:.4f}，"
        f"上 {attack_box['up']:.4f}，下 {attack_box['down']:.4f}"
        f"（相对角色中心，面向{normalize_facing(facing) if facing else '由框推断'}）"
    )
    return attack_box


# 兼容旧 import；新代码统一使用通用命名。
capture_attack_range = capture_target_range
