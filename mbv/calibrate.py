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
from mbv.paths import PLAYER_ASSET_DIR, PLAYER_HEAD_ASSET_DIR, PLAYER_TITLE_ASSET_DIR
from mbv.template_store import monster_template_directory
from mbv.vision import (
    attack_box_from_rectangle,
    normalize_facing,
    normalized_roi,
    player_attack_anchor,
    roi_pixels,
    template_foreground_mask,
)
from mbv.window import WindowInfo, capture_client, find_game_window, focus_game_window


STATUS_ITEM_KEYS = ("hp_bar", "mp_bar", "minimap", "player_marker")
WINDOW_ASPECT_RATIO_TOLERANCE = 0.03


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
    _mark_calibration_item(config, key)
    calibration = config.setdefault("calibration", {})
    calibration["window_size"] = [window.width, window.height]
    refresh_calibrated(config)
    save_config(config_path, config)
    return region


def capture_player_marker(config_path: Path, parent: Any = None) -> tuple[int, int, int]:
    """在已采集小地图中独立取玩家标记颜色，并用同一冻结帧完成取样。"""
    config = load_config(config_path)
    window = find_game_window(config)
    _prepare_window_calibration(config, window)
    focus_game_window(window)
    shape = (window.height, window.width, 3)
    minimap_rect = roi_pixels(shape, config["regions"]["minimap"])
    with mss.MSS() as sct:
        frozen_frame = capture_client(sct, window)
    result = interactive_overlay(
        window,
        "点击自己的小地图标记中心，回车确认",
        "point",
        guide_rect=minimap_rect,
        parent=parent,
        frozen_frame=frozen_frame,
    )
    if result.cancelled or result.point is None:
        raise RuntimeError("已取消小地图玩家标记采集")
    px, py = result.point
    mx, my, mw, mh = minimap_rect
    if not (mx <= px <= mx + mw and my <= py <= my + mh):
        raise RuntimeError("玩家标记采样点必须位于小地图区域内")
    hue, saturation, value = hsv_median_at(frozen_frame, px, py)
    config.setdefault("vision", {})["player_hsv_ranges"] = hue_ranges(hue, saturation, value)
    _mark_calibration_item(config, "player_marker")
    calibration = config.setdefault("calibration", {})
    calibration["player_hsv_sample"] = [hue, saturation, value]
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
    for dependent in ("platform_center", "targeting_range", "throwing_star_safe_output_area"):
        _invalidate_calibration_item(config, dependent)
    recognition = config.setdefault("recognition", {})
    recognition["platform_center_captured"] = False
    recognition["throwing_star_safe_output_area_captured"] = False
    _mark_calibration_item(config, "combat_region")
    calibration = config.setdefault("calibration", {})
    calibration["window_size"] = [window.width, window.height]
    refresh_calibrated(config)
    save_config(config_path, config)
    return region


def capture_platform_center(config_path: Path, parent: Any = None) -> dict[str, float]:
    """在已有战斗识别区内独立记录平台中心。"""
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
        "点击当前平台中心，回车确认",
        "point",
        guide_rect=combat_rect,
        parent=parent,
        frozen_frame=frozen_frame,
    )
    if result.cancelled or result.point is None:
        raise RuntimeError("已取消平台中心采集")
    px, py = result.point
    cx, cy, cw, ch = combat_rect
    if not (cx <= px <= cx + cw and cy <= py <= cy + ch):
        raise RuntimeError("平台中心必须位于战斗识别区域内")
    center = {
        "x": round((px - cx) / max(1, cw), 6),
        "y": round((py - cy) / max(1, ch), 6),
    }
    recognition = config.setdefault("recognition", {})
    recognition["platform_center"] = center
    recognition["platform_center_captured"] = True
    _mark_calibration_item(config, "platform_center")
    refresh_calibrated(config)
    save_config(config_path, config)
    return center

def hue_ranges(hue: int, saturation: int, value: int) -> list[dict[str, list[int]]]:
    delta_h = 9
    low_s = max(30, saturation - 70)
    low_v = max(35, value - 70)
    high_s = min(255, saturation + 70)
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
    point_result = interactive_overlay(
        window,
        "第 4 步：点击自己的小地图标记中心",
        "point",
        minimap_rect,
        parent=parent,
    )
    if point_result.cancelled or point_result.point is None:
        raise RuntimeError("已取消玩家标记颜色选择")
    focus_game_window(window, settle_seconds=0.15)
    with mss.MSS() as sct:
        frame = capture_client(sct, window)
    px, py = point_result.point
    hue, saturation, value = hsv_median_at(frame, px, py)
    config["vision"]["player_hsv_ranges"] = hue_ranges(hue, saturation, value)
    calibration = config.setdefault("calibration", {})
    for key in STATUS_ITEM_KEYS:
        _mark_calibration_item(config, key)
    calibration.update(
        {
            "status_regions_complete": True,
            "window_size": [window.width, window.height],
            "status_timestamp": datetime.now().isoformat(timespec="seconds"),
            "player_hsv_sample": [hue, saturation, value],
        }
    )
    refresh_calibrated(config)
    save_config(config_path, config)
    print(f"状态栏与小地图校准完成，配置已保存到：{config_path}")


def capture_recognition_region(config_path: Path, parent: Any = None) -> dict[str, float]:
    """独立采集战斗识别区，并在区内记录策略可复用的平台中心锚点。"""
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
    combat_rect = roi_pixels(shape, combat_roi)
    center_result = interactive_overlay(
        window,
        "第 2 步：点击当前平台的中心位置",
        "point",
        guide_rect=combat_rect,
        parent=parent,
        frozen_frame=frozen_frame,
    )
    if center_result.cancelled or center_result.point is None:
        raise RuntimeError("已取消平台中心采集")
    px, py = center_result.point
    cx, cy, cw, ch = combat_rect
    if not (cx <= px <= cx + cw and cy <= py <= cy + ch):
        raise RuntimeError("平台中心必须位于战斗识别区域内")
    platform_center = {
        "x": round(max(0.0, min(1.0, (px - cx) / max(1, cw))), 6),
        "y": round(max(0.0, min(1.0, (py - cy) / max(1, ch))), 6),
    }
    config["regions"]["combat"] = combat_roi
    config.setdefault("recognition", {})["platform_center"] = platform_center
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
        f"识别区域与平台中心已保存：中心 x={platform_center['x']:.3f}, "
        f"y={platform_center['y']:.3f}（相对识别区）"
    )
    return platform_center


def capture_strategy_area(
    config_path: Path,
    recognition_key: str,
    prompt: str,
    parent: Any = None,
) -> dict[str, float]:
    """框选并保存相对战斗识别区归一化的策略矩形。"""
    key = str(recognition_key).strip()
    if not key or not key.replace("_", "").isalnum():
        raise ValueError("策略采集键无效")
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
        raise RuntimeError("已取消安全输出位置框选")
    rx, ry, rw, rh = result.rectangle
    cx, cy, cw, ch = combat_rect
    if rx < cx or ry < cy or rx + rw > cx + cw or ry + rh > cy + ch:
        raise RuntimeError("安全输出位置必须完整位于战斗识别区域内")
    area = {
        "x": round((rx - cx) / max(1, cw), 6),
        "y": round((ry - cy) / max(1, ch), 6),
        "w": round(rw / max(1, cw), 6),
        "h": round(rh / max(1, ch), 6),
    }
    recognition = config.setdefault("recognition", {})
    recognition[key] = area
    recognition[f"{key}_captured"] = True
    _mark_calibration_item(config, key)
    refresh_calibrated(config)
    save_config(config_path, config)
    print(
        f"安全输出位置已保存：x={area['x']:.3f}, y={area['y']:.3f}, "
        f"w={area['w']:.3f}, h={area['h']:.3f}（相对战斗识别区）"
    )
    return area


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
    window = find_game_window(config)
    image = capture_frozen_selection(
        window,
        "框选一只清晰可见、没有遮挡的怪物",
        "已取消怪物模板框选",
        parent=parent,
    )
    directory = monster_template_directory(category, "monster", create=True)
    return _save_captured_template(image, directory, "monster", "怪物")


def capture_monster_filter(config_path: Path, parent: Any = None, category: str = "") -> Path:
    config = load_config(config_path)
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
    directory = monster_template_directory(category, "filter", create=True)
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
    window = find_game_window(config)
    image, anchor_offset = _capture_player_template_with_anchor(
        window,
        "紧贴框选自己姓名板的第一行蓝色板（含名字），不要包含角色、宠物或怪物",
        "已取消玩家模板框选",
        parent=parent,
    )
    alpha = nameplate_template_alpha(image)
    bgra = cv2.cvtColor(image, cv2.COLOR_BGR2BGRA)
    bgra[:, :, 3] = alpha
    return _save_captured_template(
        bgra,
        PLAYER_ASSET_DIR,
        "nameplate",
        "玩家姓名板",
        anchor_offset=anchor_offset,
    )


def capture_player_aux_template(config_path: Path, kind: str, parent: Any = None) -> Path:
    specs = {
        "head": (
            "紧贴框选自己的头部（头发和脸），不要包含身体、姓名板或其他玩家",
            PLAYER_HEAD_ASSET_DIR,
            "head",
            "玩家头部",
        ),
        "title": (
            "紧贴框选姓名板下方的称号勋章整行，不要包含姓名板、宠物或怪物",
            PLAYER_TITLE_ASSET_DIR,
            "title",
            "玩家称号勋章",
        ),
    }
    if kind not in specs:
        raise ValueError(f"未知辅助模板类型：{kind}")
    prompt, directory, prefix, label = specs[kind]
    config = load_config(config_path)
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
