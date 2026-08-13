from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np

from mbv.paths import ASSET_DIR

def roi_pixels(shape: tuple[int, ...], roi: dict[str, float]) -> tuple[int, int, int, int]:
    height, width = shape[:2]
    x = int(round(float(roi["x"]) * width))
    y = int(round(float(roi["y"]) * height))
    w = int(round(float(roi["w"]) * width))
    h = int(round(float(roi["h"]) * height))
    x = max(0, min(x, width - 1))
    y = max(0, min(y, height - 1))
    w = max(1, min(w, width - x))
    h = max(1, min(h, height - y))
    return x, y, w, h


def crop(frame: np.ndarray, roi: dict[str, float]) -> tuple[np.ndarray, tuple[int, int, int, int]]:
    x, y, w, h = roi_pixels(frame.shape, roi)
    return frame[y : y + h, x : x + w], (x, y, w, h)


def normalized_roi(selected: tuple[int, int, int, int], shape: tuple[int, ...]) -> dict[str, float]:
    x, y, w, h = selected
    height, width = shape[:2]
    return {
        "x": round(x / width, 6),
        "y": round(y / height, 6),
        "w": round(w / width, 6),
        "h": round(h / height, 6),
    }


def hsv_mask(image: np.ndarray, ranges: list[dict[str, list[int]]]) -> np.ndarray:
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    result = np.zeros(hsv.shape[:2], dtype=np.uint8)
    for item in ranges:
        lower = np.array(item["lower"], dtype=np.uint8)
        upper = np.array(item["upper"], dtype=np.uint8)
        result = cv2.bitwise_or(result, cv2.inRange(hsv, lower, upper))
    return result


def bar_fill(image: np.ndarray, ranges: list[dict[str, list[int]]]) -> float:
    if image.size == 0:
        return 0.0
    mask = hsv_mask(image, ranges)
    # A filled bar colors most pixels in a column; text/glints should not count as fill.
    column_coverage = np.mean(mask > 0, axis=0)
    return float(np.mean(column_coverage >= 0.28))


def player_marker(
    image: np.ndarray,
    ranges: list[dict[str, list[int]]],
    min_area: int,
    max_area: int,
    previous: tuple[float, float] | None,
) -> tuple[tuple[float, float] | None, np.ndarray]:
    mask = hsv_mask(image, ranges)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    count, _labels, stats, centers = cv2.connectedComponentsWithStats(mask, 8)
    choices: list[tuple[float, float, int]] = []
    height, width = image.shape[:2]
    for index in range(1, count):
        area = int(stats[index, cv2.CC_STAT_AREA])
        if min_area <= area <= max_area:
            cx, cy = centers[index]
            choices.append((float(cx / width), float(cy / height), area))
    if not choices:
        return None, mask
    if previous is None:
        chosen = max(choices, key=lambda item: item[2])
    else:
        chosen = min(choices, key=lambda item: (item[0] - previous[0]) ** 2 + (item[1] - previous[1]) ** 2)
    return (chosen[0], chosen[1]), mask


@dataclass
class Template:
    name: str
    image: np.ndarray
    foreground_mask: np.ndarray | None = None
    _feature_cache: dict[float, tuple[np.ndarray, np.ndarray, np.ndarray]] = field(
        default_factory=dict, repr=False, compare=False
    )
    _edge_cache: dict[float, np.ndarray] = field(default_factory=dict, repr=False, compare=False)

    def scaled_features(self, scale: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """模板是静态的：按缩放比例缓存 (图像, 前景蒙版, 颜色对立通道)。"""
        cached = self._feature_cache.get(scale)
        if cached is None:
            image = self.image
            mask = self.foreground_mask if self.foreground_mask is not None else template_foreground_mask(image)
            if scale < 0.999:
                image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
                mask = cv2.resize(mask, (image.shape[1], image.shape[0]), interpolation=cv2.INTER_NEAREST)
            cached = (image, mask, opponent_colors(image))
            self._feature_cache[scale] = cached
        return cached

    def scaled_edges(self, scale: float) -> np.ndarray:
        edges = self._edge_cache.get(scale)
        if edges is None:
            image, _mask, _opponent = self.scaled_features(scale)
            edges = cv2.Canny(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), 55, 140)
            self._edge_cache[scale] = edges
        return edges


@dataclass(frozen=True)
class Detection:
    box: tuple[int, int, int, int]
    score: float
    name: str


@dataclass(frozen=True)
class PlayerAnchor:
    """玩家统一定位锚点：box 顶边代表脚底高度，中心代表玩家水平位置。"""
    box: tuple[int, int, int, int]
    score: float
    source: str
    raw_box: tuple[int, int, int, int]


def template_foreground_mask(image: np.ndarray) -> np.ndarray:
    """保留怪物的高饱和度颜色，尽量排除木板、墙面等模板背景。"""
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    hue, saturation, value = cv2.split(hsv)
    brown_map_background = (hue >= 5) & (hue <= 25) & (saturation >= 65)
    mask = (
        (saturation >= 65)
        & (value >= 35)
        & ~brown_map_background
    ).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    if int(np.count_nonzero(mask)) < image.shape[0] * image.shape[1] * 0.08:
        return np.full(image.shape[:2], 255, dtype=np.uint8)
    return mask


def opponent_colors(image: np.ndarray) -> np.ndarray:
    """将亮度与颜色分离，避免灰色木板仅因亮度相似而匹配成彩色怪物。"""
    values = image.astype(np.float32) / 255.0
    blue, green, red = cv2.split(values)
    return np.dstack((blue - green, red - green)).astype(np.float32)


def load_templates(directory: Path = ASSET_DIR) -> list[Template]:
    templates: list[Template] = []
    directory.mkdir(parents=True, exist_ok=True)
    for path in sorted(directory.glob("*.png")):
        data = np.fromfile(path, dtype=np.uint8)
        decoded = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
        if decoded is None:
            continue
        alpha = decoded[:, :, 3] if decoded.ndim == 3 and decoded.shape[2] == 4 else None
        image = decoded[:, :, :3] if decoded.ndim == 3 and decoded.shape[2] == 4 else decoded
        if image is not None and image.shape[0] >= 4 and image.shape[1] >= 4:
            mask = alpha if alpha is not None and int(np.count_nonzero(alpha)) > 0 else template_foreground_mask(image)
            templates.append(Template(path.name, image, mask))
    return templates


class SceneFeatures:
    """同一帧的场景特征按缩放比例缓存，供多组模板检测复用，避免重复 resize / 颜色转换 / Canny。"""

    def __init__(self, scene: np.ndarray) -> None:
        self.scene = scene
        self._scaled: dict[float, np.ndarray] = {}
        self._opponent: dict[float, np.ndarray] = {}
        self._edges: dict[float, np.ndarray] = {}

    def scaled(self, scale: float) -> np.ndarray:
        cached = self._scaled.get(scale)
        if cached is None:
            if scale < 0.999:
                cached = cv2.resize(self.scene, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            else:
                cached = self.scene
            self._scaled[scale] = cached
        return cached

    def opponent(self, scale: float) -> np.ndarray:
        cached = self._opponent.get(scale)
        if cached is None:
            cached = opponent_colors(self.scaled(scale))
            self._opponent[scale] = cached
        return cached

    def edges(self, scale: float) -> np.ndarray:
        cached = self._edges.get(scale)
        if cached is None:
            cached = cv2.Canny(cv2.cvtColor(self.scaled(scale), cv2.COLOR_BGR2GRAY), 55, 140)
            self._edges[scale] = cached
        return cached


def box_iou(first: tuple[int, int, int, int], second: tuple[int, int, int, int]) -> float:
    ax, ay, aw, ah = first
    bx, by, bw, bh = second
    left = max(ax, bx)
    top = max(ay, by)
    right = min(ax + aw, bx + bw)
    bottom = min(ay + ah, by + bh)
    intersection = max(0, right - left) * max(0, bottom - top)
    if intersection <= 0:
        return 0.0
    union = aw * ah + bw * bh - intersection
    return float(intersection / max(1, union))


def find_detections(
    scene: np.ndarray | SceneFeatures,
    templates: list[Template],
    threshold: float,
    detection_scale: float = 1.0,
    max_per_template: int = 40,
    nms_iou: float = 0.38,
    max_detections: int = 24,
    structure_weight: float = 0.0,
) -> tuple[list[Detection], float, str | None]:
    """返回画面中的全部模板目标，并通过 NMS 合并同一目标的重复框。

    scene 可传 SceneFeatures，让同一帧的多组检测（怪物、姓名板、头部、称号）复用场景特征。
    """
    features = scene if isinstance(scene, SceneFeatures) else SceneFeatures(scene)
    scale = max(0.4, min(1.0, float(detection_scale)))
    source = features.scaled(scale)
    source_opponent = features.opponent(scale)
    structure_weight = max(0.0, min(0.9, float(structure_weight)))
    source_edges = features.edges(scale) if structure_weight > 0 else None
    best_score = -1.0
    best_name = None
    candidates: list[Detection] = []
    for template in templates:
        image, mask, template_opponent = template.scaled_features(scale)
        th, tw = image.shape[:2]
        if th > source.shape[0] or tw > source.shape[1] or th < 4 or tw < 4:
            continue
        color_result = cv2.matchTemplate(
            source_opponent,
            template_opponent,
            cv2.TM_CCORR_NORMED,
            mask=mask,
        )
        if structure_weight > 0 and source_edges is not None:
            edge_result = cv2.matchTemplate(
                source_edges,
                template.scaled_edges(scale),
                cv2.TM_CCORR_NORMED,
                mask=mask,
            )
            result = cv2.addWeighted(
                color_result,
                1.0 - structure_weight,
                edge_result,
                structure_weight,
                0.0,
                dst=color_result,
            )
        else:
            result = color_result
        result = np.nan_to_num(result, copy=False, nan=-1.0, posinf=-1.0, neginf=-1.0)
        _min_value, max_value, _min_pos, max_pos = cv2.minMaxLoc(result)
        if float(max_value) > best_score:
            best_score = float(max_value)
            best_name = template.name
        if float(max_value) < threshold:
            continue
        peak_kernel = max(5, int(round(min(th, tw) * 0.35)))
        if peak_kernel % 2 == 0:
            peak_kernel += 1
        local_max = cv2.dilate(result, np.ones((peak_kernel, peak_kernel), dtype=np.uint8))
        ys, xs = np.where((result >= threshold) & (result >= local_max - 1e-6))
        if len(xs) > max_per_template:
            scores = result[ys, xs]
            indexes = np.argpartition(scores, -max_per_template)[-max_per_template:]
            xs, ys = xs[indexes], ys[indexes]
        for x, y in zip(xs.tolist(), ys.tolist()):
            candidates.append(
                Detection(
                    (
                        int(round(x / scale)),
                        int(round(y / scale)),
                        template.image.shape[1],
                        template.image.shape[0],
                    ),
                    float(result[y, x]),
                    template.name,
                )
            )

    kept: list[Detection] = []
    for candidate in sorted(candidates, key=lambda item: item.score, reverse=True):
        if all(box_iou(candidate.box, existing.box) < nms_iou for existing in kept):
            kept.append(candidate)
            if len(kept) >= max_detections:
                break
    return kept, best_score, best_name


def find_monster(
    combat: np.ndarray,
    templates: list[Template],
    threshold: float,
    detection_scale: float = 1.0,
) -> tuple[tuple[int, int, int, int] | None, float, str | None]:
    detections, best_score, best_name = find_detections(
        combat,
        templates,
        threshold,
        detection_scale,
    )
    if not detections:
        return None, best_score, best_name
    return detections[0].box, best_score, detections[0].name


def normalize_facing(facing: str | None) -> str:
    return "left" if str(facing or "").strip().lower() == "left" else "right"


def player_anchor_center(
    player_box: tuple[int, int, int, int],
    raw_box: tuple[int, int, int, int] | None = None,
) -> tuple[float, float]:
    """攻击框锚在角色中心：有原始检测框时用它的中心，否则用定位框几何中心。"""
    if raw_box is not None:
        rx, ry, rw, rh = raw_box
        return rx + rw / 2.0, ry + rh / 2.0
    px, py, pw, ph = player_box
    return px + pw / 2.0, py + ph / 2.0


def attack_box_from_config(behavior: dict[str, Any]) -> dict[str, float]:
    box = behavior.get("bow_attack_box")
    if isinstance(box, dict):
        try:
            return {
                "forward": float(box["forward"]),
                "back": float(box["back"]),
                "up": float(box["up"]),
                "down": float(box["down"]),
            }
        except (KeyError, TypeError, ValueError):
            pass
    rng = float(behavior.get("bow_attack_range", 0.312))
    vert = float(behavior.get("bow_vertical_tolerance", 0.12))
    return {"forward": rng, "back": rng, "up": vert, "down": vert}


def attack_rect_from_player(
    player_center: tuple[float, float],
    scene_width: int,
    scene_height: int,
    attack_box: dict[str, float],
    facing: str | None,
) -> tuple[float, float, float, float]:
    """把相对角色中心的前后上下，按当前面向还原成战斗区矩形（左、上、右、下）。"""
    cx, cy = player_center
    width = max(1.0, float(scene_width))
    height = max(1.0, float(scene_height))
    forward = float(attack_box["forward"]) * width
    back = float(attack_box["back"]) * width
    up = float(attack_box["up"]) * height
    down = float(attack_box["down"]) * height
    if normalize_facing(facing) == "left":
        left, right = cx - forward, cx + back
    else:
        left, right = cx - back, cx + forward
    return left, cy - up, right, cy + down


def _ordered_rect(left: float, top: float, right: float, bottom: float) -> tuple[float, float, float, float]:
    if left > right:
        left, right = right, left
    if top > bottom:
        top, bottom = bottom, top
    return left, top, right, bottom


def point_in_attack_rect(x: float, y: float, rect: tuple[float, float, float, float]) -> bool:
    left, top, right, bottom = _ordered_rect(*rect)
    return left <= x <= right and top <= y <= bottom


def _choose_nearest_eligible(
    detections: list[Detection],
    player_x: float,
    player_y: float,
    include: Callable[[float, float], bool],
) -> Detection | None:
    eligible: list[tuple[float, float, float, Detection]] = []
    for detection in detections:
        x, y, w, h = detection.box
        mx, my = x + w / 2.0, y + h / 2.0
        if not include(mx, my):
            continue
        eligible.append((abs(mx - player_x), abs(my - player_y), -detection.score, detection))
    if not eligible:
        return None
    eligible.sort(key=lambda item: (item[0], item[1], item[2]))
    return eligible[0][3]


def choose_nearest_target(
    detections: list[Detection],
    player_box: tuple[int, int, int, int] | None,
    scene_width: int,
    scene_height: int,
    attack_box: dict[str, float],
    facing: str | None = "right",
    raw_box: tuple[int, int, int, int] | None = None,
) -> Detection | None:
    """只在框选的攻击区内，选择离玩家水平距离最近的怪物。"""
    if player_box is None:
        return None
    player_x, player_y = player_anchor_center(player_box, raw_box)
    rect = attack_rect_from_player((player_x, player_y), scene_width, scene_height, attack_box, facing)
    return _choose_nearest_eligible(
        detections,
        player_x,
        player_y,
        lambda mx, my: point_in_attack_rect(mx, my, rect),
    )


def choose_nearest_same_level_target(
    detections: list[Detection],
    player_box: tuple[int, int, int, int] | None,
    scene_width: int,
    scene_height: int,
    attack_box: dict[str, float],
    raw_box: tuple[int, int, int, int] | None = None,
) -> Detection | None:
    """高度仍用框选的上/下范围，水平不限制，供追踪移动使用。"""
    if player_box is None:
        return None
    player_x, player_y = player_anchor_center(player_box, raw_box)
    _left, top, _right, bottom = _ordered_rect(
        *attack_rect_from_player((player_x, player_y), scene_width, scene_height, attack_box, "right")
    )
    return _choose_nearest_eligible(
        detections,
        player_x,
        player_y,
        lambda _mx, my: top <= my <= bottom,
    )


def attack_box_from_rectangle(
    rectangle: tuple[int, int, int, int],
    combat_rect: tuple[int, int, int, int],
    player_box: tuple[int, int, int, int],
    raw_box: tuple[int, int, int, int] | None = None,
    facing: str | None = None,
) -> dict[str, float]:
    """把客户区矩形原样换成相对角色中心的前/后/上/下，不做对称或取最大修正。"""
    rx, ry, rw, rh = rectangle
    cx, cy, cw, ch = combat_rect
    left, top = float(rx), float(ry)
    right, bottom = float(rx + rw), float(ry + rh)
    local_x, local_y = player_anchor_center(player_box, raw_box)
    player_x = cx + local_x
    player_y = cy + local_y
    width = max(1.0, float(cw))
    height = max(1.0, float(ch))
    left_ext = (player_x - left) / width
    right_ext = (right - player_x) / width
    up_ext = (player_y - top) / height
    down_ext = (bottom - player_y) / height
    inferred = "left" if left_ext > right_ext else "right"
    if normalize_facing(facing if facing is not None else inferred) == "left":
        forward, back = left_ext, right_ext
    else:
        forward, back = right_ext, left_ext
    return {
        "forward": round(forward, 6),
        "back": round(back, 6),
        "up": round(up_ext, 6),
        "down": round(down_ext, 6),
    }


def player_anchor_from_detection(
    detection: Detection,
    source: str,
    scene_height: int,
    head_feet_offset: float,
    title_feet_offset: float,
) -> PlayerAnchor:
    x, y, w, h = detection.box
    if source == "姓名板":
        feet_y = y
    elif source == "头部":
        feet_y = y + h + int(round(float(head_feet_offset) * scene_height))
    elif source == "称号勋章":
        feet_y = y - int(round(float(title_feet_offset) * scene_height))
    else:
        raise ValueError(f"未知玩家定位来源：{source}")
    feet_y = max(0, min(scene_height - 1, feet_y))
    return PlayerAnchor((x, feet_y, w, 1), detection.score, source, detection.box)


def choose_fused_player_anchor(
    groups: list[tuple[str, list[Detection]]],
    previous: PlayerAnchor | None,
    scene_width: int,
    scene_height: int,
    head_feet_offset: float,
    title_feet_offset: float,
    max_jump: float,
    agreement_distance: float = 0.07,
) -> PlayerAnchor | None:
    """融合三路锚点：先看跨来源相互支持度，再看来源优先级和上帧距离。"""
    previous_center = None
    if previous is not None:
        previous_center = (
            previous.box[0] + previous.box[2] / 2.0,
            previous.box[1],
        )
    max_distance = max(20.0, float(max_jump) * max(scene_width, scene_height))
    source_rank = {"姓名板": 0, "头部": 1, "称号勋章": 2}
    candidates: list[tuple[int, float, PlayerAnchor]] = []
    for source, detections in groups:
        for detection in detections:
            anchor = player_anchor_from_detection(
                detection,
                source,
                scene_height,
                head_feet_offset,
                title_feet_offset,
            )
            center = (anchor.box[0] + anchor.box[2] / 2.0, anchor.box[1])
            distance = 0.0
            if previous_center is not None:
                distance = ((center[0] - previous_center[0]) ** 2 + (center[1] - previous_center[1]) ** 2) ** 0.5
                if distance > max_distance:
                    continue
            candidates.append((source_rank.get(source, 9), distance, anchor))
    if not candidates:
        return None
    agreement_pixels = max(16.0, float(agreement_distance) * max(scene_width, scene_height))
    ranked: list[tuple[int, int, float, float, PlayerAnchor]] = []
    for rank, distance, anchor in candidates:
        center_x = anchor.box[0] + anchor.box[2] / 2.0
        feet_y = anchor.box[1]
        supporting_sources = {anchor.source}
        for _other_rank, _other_distance, other in candidates:
            if other.source == anchor.source:
                continue
            other_x = other.box[0] + other.box[2] / 2.0
            other_y = other.box[1]
            separation = ((center_x - other_x) ** 2 + (feet_y - other_y) ** 2) ** 0.5
            if separation <= agreement_pixels:
                supporting_sources.add(other.source)
        ranked.append((-len(supporting_sources), rank, distance, -anchor.score, anchor))
    ranked.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
    return ranked[0][4]
