from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, replace
from pathlib import Path
from pathlib import PurePosixPath
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


def clipped_search_roi(
    search_roi: tuple[int, int, int, int] | None,
    shape: tuple[int, ...],
) -> tuple[int, int, int, int] | None:
    if search_roi is None:
        return None
    height, width = shape[:2]
    x, y, w, h = (int(value) for value in search_roi)
    left = max(0, min(width, x))
    top = max(0, min(height, y))
    right = max(left, min(width, x + max(0, w)))
    bottom = max(top, min(height, y + max(0, h)))
    return left, top, right - left, bottom - top


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


@dataclass(frozen=True)
class PlayerMarkerObservation:
    """小地图玩家色块的本帧观测；候选不唯一时不得用于屏幕位置辅助。"""

    point: tuple[float, float] | None
    candidate_count: int
    distance_from_previous: float | None

    @property
    def unambiguous(self) -> bool:
        return self.point is not None and self.candidate_count == 1


def player_marker_observation(
    image: np.ndarray,
    ranges: list[dict[str, list[int]]],
    min_area: int,
    max_area: int,
    previous: tuple[float, float] | None,
) -> tuple[PlayerMarkerObservation, np.ndarray]:
    """返回玩家标记及候选数量，保留原有按上一位置消歧的显示行为。"""
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
        return PlayerMarkerObservation(None, 0, None), mask
    if previous is None:
        chosen = max(choices, key=lambda item: item[2])
        distance = None
    else:
        chosen = min(
            choices,
            key=lambda item: (item[0] - previous[0]) ** 2 + (item[1] - previous[1]) ** 2,
        )
        distance = math.hypot(chosen[0] - previous[0], chosen[1] - previous[1])
    return PlayerMarkerObservation((chosen[0], chosen[1]), len(choices), distance), mask


def player_marker(
    image: np.ndarray,
    ranges: list[dict[str, list[int]]],
    min_area: int,
    max_area: int,
    previous: tuple[float, float] | None,
) -> tuple[tuple[float, float] | None, np.ndarray]:
    observation, mask = player_marker_observation(
        image,
        ranges,
        min_area,
        max_area,
        previous,
    )
    return observation.point, mask


@dataclass
class Template:
    name: str
    image: np.ndarray
    foreground_mask: np.ndarray | None = None
    anchor_offset: tuple[float, float] | None = None
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
    identity_score: float | None = None
    anchor_offset: tuple[float, float] | None = None


@dataclass(frozen=True)
class PlayerAnchor:
    """玩家统一定位锚点：box 顶边代表脚底高度，中心代表玩家水平位置。"""
    box: tuple[int, int, int, int]
    score: float
    source: str
    raw_box: tuple[int, int, int, int]


def monster_template_alpha(
    image: np.ndarray,
    *,
    minimum_ratio: float = 0.01,
    maximum_ratio: float = 0.92,
) -> np.ndarray:
    """从框选边缘估计背景，提取与颜色无关的怪物主体 Alpha。"""
    if image.ndim != 3 or image.shape[2] < 3:
        raise ValueError("怪物模板必须是彩色图片")
    height, width = image.shape[:2]
    if height < 8 or width < 8:
        raise ValueError("怪物模板过小，请重新框选")

    grab_mask = np.full((height, width), cv2.GC_PR_BGD, dtype=np.uint8)
    margin = max(1, min(4, min(width, height) // 10))
    grab_mask[:margin, :] = cv2.GC_BGD
    grab_mask[-margin:, :] = cv2.GC_BGD
    grab_mask[:, :margin] = cv2.GC_BGD
    grab_mask[:, -margin:] = cv2.GC_BGD
    center_x1 = width // 6
    center_x2 = max(center_x1 + 1, width * 5 // 6)
    center_y1 = height // 6
    center_y2 = max(center_y1 + 1, height * 5 // 6)
    grab_mask[center_y1:center_y2, center_x1:center_x2] = cv2.GC_PR_FGD

    def run_grabcut(mask: np.ndarray, mode: int, rect: tuple[int, int, int, int] | None = None) -> np.ndarray | None:
        background_model = np.zeros((1, 65), dtype=np.float64)
        foreground_model = np.zeros((1, 65), dtype=np.float64)
        try:
            cv2.grabCut(
                image[:, :, :3],
                mask,
                rect,
                background_model,
                foreground_model,
                5,
                mode,
            )
        except cv2.error:
            return None
        alpha = np.where(
            (mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD),
            255,
            0,
        ).astype(np.uint8)
        alpha = cv2.morphologyEx(alpha, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
        component_count, labels, stats, _centroids = cv2.connectedComponentsWithStats(
            (alpha > 0).astype(np.uint8),
            connectivity=8,
        )
        if component_count <= 1:
            return None
        largest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        return np.where(labels == largest, 255, 0).astype(np.uint8)

    alpha = run_grabcut(grab_mask, cv2.GC_INIT_WITH_MASK)
    if alpha is None:
        # 中心种子不适合偏离中心或动作伸展的怪物时，用整个内框做第二次估计。
        rect_margin = max(1, min(3, min(width, height) // 12))
        rect = (
            rect_margin,
            rect_margin,
            width - rect_margin * 2,
            height - rect_margin * 2,
        )
        alpha = run_grabcut(
            np.zeros((height, width), dtype=np.uint8),
            cv2.GC_INIT_WITH_RECT,
            rect,
        )
    if alpha is None:
        raise ValueError("没有提取到怪物主体，请缩小框选范围并保留少量背景")

    ratio = float(np.count_nonzero(alpha)) / max(1, width * height)
    if ratio < max(0.01, float(minimum_ratio)):
        raise ValueError("怪物前景占比过小，请紧贴怪物重新框选")
    if ratio > min(0.99, float(maximum_ratio)):
        raise ValueError("框选中无法区分怪物与背景，请在怪物四周保留少量背景后重试")
    return alpha


def monster_template_image(image: np.ndarray) -> np.ndarray:
    """生成带 Alpha 的怪物模板，并自动裁掉分割后多余的背景。"""
    alpha = monster_template_alpha(image)
    ys, xs = np.where(alpha > 0)
    if xs.size == 0 or ys.size == 0:
        raise ValueError("没有提取到怪物主体，请重新框选")
    subject_left = int(xs.min())
    subject_top = int(ys.min())
    subject_right = int(xs.max()) + 1
    subject_bottom = int(ys.max()) + 1
    subject_width = subject_right - subject_left
    subject_height = subject_bottom - subject_top
    if subject_width < 4 or subject_height < 4:
        raise ValueError("怪物主体过小，请重新框选")
    padding = max(2, int(round(max(subject_width, subject_height) * 0.05)))
    left = max(0, subject_left - padding)
    top = max(0, subject_top - padding)
    right = min(image.shape[1], subject_right + padding)
    bottom = min(image.shape[0], subject_bottom + padding)
    cropped = image[top:bottom, left:right, :3]
    cropped_alpha = alpha[top:bottom, left:right]
    bgra = cv2.cvtColor(cropped, cv2.COLOR_BGR2BGRA)
    bgra[:, :, 3] = cropped_alpha
    return bgra


def template_foreground_mask(image: np.ndarray) -> np.ndarray:
    """为没有 Alpha 的旧模板即时生成前景；异常旧模板保持兼容。"""
    try:
        return monster_template_alpha(image)
    except ValueError:
        return np.full(image.shape[:2], 255, dtype=np.uint8)


def opponent_colors(image: np.ndarray) -> np.ndarray:
    """将亮度与颜色分离，避免灰色木板仅因亮度相似而匹配成彩色怪物。"""
    values = image.astype(np.float32) / 255.0
    blue, green, red = cv2.split(values)
    return np.dstack((blue - green, red - green)).astype(np.float32)


def nameplate_identity_mask(
    image: np.ndarray,
    foreground_mask: np.ndarray | None = None,
) -> np.ndarray:
    """提取姓名板中亮、低饱和度的名字字形，排除共用蓝板和两端装饰。"""
    if image.size == 0:
        return np.zeros(image.shape[:2], dtype=np.uint8)
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    _hue, saturation, value = cv2.split(hsv)
    height, width = image.shape[:2]
    if foreground_mask is not None:
        valid = cv2.resize(
            foreground_mask,
            (width, height),
            interpolation=cv2.INTER_NEAREST,
        ) > 0
    else:
        valid = np.ones((height, width), dtype=bool)
    valid_values = value[valid]
    if valid_values.size == 0:
        return np.zeros((height, width), dtype=np.uint8)
    # 透明边缘的 RGB 往往保留为纯白；百分位只使用 Alpha 有效区，避免阈值被推到 255。
    bright_threshold = max(145, int(np.percentile(valid_values, 82)))
    mask = (
        (value >= bright_threshold)
        & (saturation <= 115)
        & valid
    ).astype(np.uint8) * 255
    # 宽姓名板的两端通常是所有玩家共用的装饰；短姓名板本身已接近名字区域。
    margin_ratio = 0.18 if width >= height * 2.1 else 0.05
    margin_x = min(width // 3, int(round(width * margin_ratio)))
    margin_y = min(height // 4, max(1, int(round(height * 0.06))))
    if margin_x:
        mask[:, :margin_x] = 0
        mask[:, width - margin_x :] = 0
    if margin_y:
        mask[:margin_y, :] = 0
        mask[height - margin_y :, :] = 0
    return mask


def nameplate_identity_similarity(
    template: np.ndarray,
    candidate: np.ndarray,
    foreground_mask: np.ndarray | None = None,
) -> float:
    """比较姓名字形的重合度；共享蓝板本身不会贡献身份分。"""
    if template.size == 0 or candidate.size == 0:
        return 0.0
    if candidate.shape[:2] != template.shape[:2]:
        candidate = cv2.resize(candidate, (template.shape[1], template.shape[0]), interpolation=cv2.INTER_AREA)
    expected = nameplate_identity_mask(template, foreground_mask) > 0
    actual = nameplate_identity_mask(candidate, foreground_mask) > 0
    expected_count = int(np.count_nonzero(expected))
    if expected_count < 3 or int(np.count_nonzero(actual)) < 3:
        return 0.0
    best = 0.0
    for shift_y in (-1, 0, 1):
        for shift_x in (-1, 0, 1):
            shifted = np.zeros_like(actual)
            source_y1 = max(0, -shift_y)
            source_y2 = actual.shape[0] - max(0, shift_y)
            source_x1 = max(0, -shift_x)
            source_x2 = actual.shape[1] - max(0, shift_x)
            target_y1 = max(0, shift_y)
            target_y2 = target_y1 + max(0, source_y2 - source_y1)
            target_x1 = max(0, shift_x)
            target_x2 = target_x1 + max(0, source_x2 - source_x1)
            shifted[target_y1:target_y2, target_x1:target_x2] = actual[
                source_y1:source_y2,
                source_x1:source_x2,
            ]
            shifted_count = int(np.count_nonzero(shifted))
            intersection = int(np.count_nonzero(expected & shifted))
            score = (2.0 * intersection) / max(1, expected_count + shifted_count)
            best = max(best, float(score))
    return best


def verify_nameplate_identities(
    scene: np.ndarray,
    detections: list[Detection],
    templates: list[Template],
) -> list[Detection]:
    """在原分辨率上为姓名板候选补充名字字形分和模板脚底元数据。"""
    by_name = {template.name: template for template in templates}
    height, width = scene.shape[:2]
    verified: list[Detection] = []
    for detection in detections:
        template = by_name.get(detection.name)
        if template is None:
            verified.append(replace(detection, identity_score=0.0))
            continue
        x, y, w, h = detection.box
        left, top = max(0, x), max(0, y)
        right, bottom = min(width, x + w), min(height, y + h)
        if right - left != template.image.shape[1] or bottom - top != template.image.shape[0]:
            score = 0.0
        else:
            score = nameplate_identity_similarity(
                template.image,
                scene[top:bottom, left:right],
                template.foreground_mask,
            )
        verified.append(
            replace(
                detection,
                identity_score=score,
                anchor_offset=template.anchor_offset,
            )
        )
    return verified


def deduplicate_nameplate_detections(
    detections: list[Detection],
    nms_iou: float = 0.35,
    max_detections: int = 8,
) -> list[Detection]:
    """优先保留姓名字形分高的候选，再跨模板合并同一姓名板。"""
    kept: list[Detection] = []
    ordered = sorted(
        detections,
        key=lambda item: (float(item.identity_score or 0.0), item.score),
        reverse=True,
    )
    for candidate in ordered:
        if all(box_iou(candidate.box, existing.box) < nms_iou for existing in kept):
            kept.append(candidate)
            if len(kept) >= max(1, int(max_detections)):
                break
    return kept


def _load_template_anchor(path: Path) -> tuple[float, float] | None:
    metadata_path = path.with_suffix(".anchor.json")
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        offset = payload.get("anchor_offset")
        if payload.get("version") != 1 or not isinstance(offset, list) or len(offset) != 2:
            return None
        return float(offset[0]), float(offset[1])
    except (OSError, TypeError, ValueError):
        return None


def load_templates(directory: Path = ASSET_DIR, *, recursive: bool = False) -> list[Template]:
    templates: list[Template] = []
    directory.mkdir(parents=True, exist_ok=True)
    paths = directory.rglob("*.png") if recursive else directory.glob("*.png")
    for path in sorted(paths, key=lambda item: item.as_posix().casefold()):
        try:
            data = np.fromfile(path, dtype=np.uint8)
            decoded = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
        except (OSError, ValueError, cv2.error):
            continue
        if decoded is None:
            continue
        alpha = decoded[:, :, 3] if decoded.ndim == 3 and decoded.shape[2] == 4 else None
        image = decoded[:, :, :3] if decoded.ndim == 3 and decoded.shape[2] == 4 else decoded
        if image is not None and image.shape[0] >= 4 and image.shape[1] >= 4:
            mask = alpha if alpha is not None and int(np.count_nonzero(alpha)) > 0 else template_foreground_mask(image)
            name = path.relative_to(directory).as_posix() if recursive else path.name
            templates.append(Template(name, image, mask, _load_template_anchor(path)))
    return templates


class SceneFeatures:
    """同一帧的场景特征按缩放比例缓存，供多组模板检测复用，避免重复 resize / 颜色转换 / Canny。"""

    def __init__(self, scene: np.ndarray) -> None:
        self.scene = scene
        self._scaled: dict[tuple[float, tuple[int, int, int, int] | None], np.ndarray] = {}
        self._opponent: dict[tuple[float, tuple[int, int, int, int] | None], np.ndarray] = {}
        self._edges: dict[tuple[float, tuple[int, int, int, int] | None], np.ndarray] = {}

    def _scaled_bounds(
        self,
        scale: float,
        search_roi: tuple[int, int, int, int],
    ) -> tuple[int, int, int, int]:
        full_height, full_width = self.scaled(scale).shape[:2]
        x, y, w, h = search_roi
        left = max(0, min(full_width, int(math.floor(x * scale))))
        top = max(0, min(full_height, int(math.floor(y * scale))))
        right = max(left, min(full_width, int(math.ceil((x + w) * scale))))
        bottom = max(top, min(full_height, int(math.ceil((y + h) * scale))))
        return left, top, right, bottom

    def scaled_origin(
        self,
        scale: float,
        search_roi: tuple[int, int, int, int] | None = None,
    ) -> tuple[int, int]:
        clipped_roi = clipped_search_roi(search_roi, self.scene.shape)
        if clipped_roi is None:
            return 0, 0
        left, top, _right, _bottom = self._scaled_bounds(scale, clipped_roi)
        return left, top

    def scaled(
        self,
        scale: float,
        search_roi: tuple[int, int, int, int] | None = None,
    ) -> np.ndarray:
        clipped_roi = clipped_search_roi(search_roi, self.scene.shape)
        key = (scale, clipped_roi)
        cached = self._scaled.get(key)
        if cached is None:
            if clipped_roi is not None:
                full = self.scaled(scale)
                left, top, right, bottom = self._scaled_bounds(scale, clipped_roi)
                cached = full[top:bottom, left:right]
            elif scale < 0.999:
                cached = cv2.resize(self.scene, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            else:
                cached = self.scene
            self._scaled[key] = cached
        return cached

    def opponent(
        self,
        scale: float,
        search_roi: tuple[int, int, int, int] | None = None,
    ) -> np.ndarray:
        clipped_roi = clipped_search_roi(search_roi, self.scene.shape)
        key = (scale, clipped_roi)
        cached = self._opponent.get(key)
        if cached is None:
            full_cached = self._opponent.get((scale, None))
            if clipped_roi is not None and full_cached is not None:
                left, top, right, bottom = self._scaled_bounds(scale, clipped_roi)
                cached = full_cached[top:bottom, left:right]
            else:
                cached = opponent_colors(self.scaled(scale, clipped_roi))
            self._opponent[key] = cached
        return cached

    def edges(
        self,
        scale: float,
        search_roi: tuple[int, int, int, int] | None = None,
    ) -> np.ndarray:
        clipped_roi = clipped_search_roi(search_roi, self.scene.shape)
        key = (scale, clipped_roi)
        cached = self._edges.get(key)
        if cached is None:
            full_cached = self._edges.get((scale, None))
            if clipped_roi is not None and full_cached is not None:
                left, top, right, bottom = self._scaled_bounds(scale, clipped_roi)
                cached = full_cached[top:bottom, left:right]
            else:
                cached = cv2.Canny(
                    cv2.cvtColor(self.scaled(scale, clipped_roi), cv2.COLOR_BGR2GRAY),
                    55,
                    140,
                )
            self._edges[key] = cached
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


def monster_template_category(name: str) -> str:
    """从递归模板名称中取得分类；根目录旧模板属于未分类。"""
    parts = PurePosixPath(str(name).replace("\\", "/")).parts
    return parts[0] if len(parts) > 1 else ""


def monster_templates_for_category(
    templates: list[Template],
    category: str,
) -> list[Template]:
    """只保留指定怪物分类；空字符串表示根目录中的“未分类”。"""
    selected = str(category).strip().casefold()
    return [
        template
        for template in templates
        if monster_template_category(template.name).casefold() == selected
    ]


def _intersection_over_smaller_box(
    first: tuple[int, int, int, int],
    second: tuple[int, int, int, int],
) -> float:
    ax, ay, aw, ah = first
    bx, by, bw, bh = second
    left = max(ax, bx)
    top = max(ay, by)
    right = min(ax + aw, bx + bw)
    bottom = min(ay + ah, by + bh)
    intersection = max(0, right - left) * max(0, bottom - top)
    return float(intersection / max(1, min(aw * ah, bw * bh)))


def suppress_monster_detections(
    detections: list[Detection],
    filters: list[Detection],
    min_overlap: float = 0.5,
    center_margin: float = 0.15,
) -> list[Detection]:
    """只用同分类、同位置的过滤项抑制怪物候选，避免一处负样本误伤全屏。"""
    overlap_threshold = max(0.0, min(1.0, float(min_overlap)))
    margin = max(0.0, float(center_margin))
    kept: list[Detection] = []
    for detection in detections:
        category = monster_template_category(detection.name).casefold()
        center_x = detection.box[0] + detection.box[2] / 2.0
        center_y = detection.box[1] + detection.box[3] / 2.0
        suppressed = False
        for exclusion in filters:
            if monster_template_category(exclusion.name).casefold() != category:
                continue
            ex, ey, ew, eh = exclusion.box
            margin_x = ew * margin
            margin_y = eh * margin
            center_inside = (
                ex - margin_x <= center_x <= ex + ew + margin_x
                and ey - margin_y <= center_y <= ey + eh + margin_y
            )
            if center_inside and _intersection_over_smaller_box(detection.box, exclusion.box) >= overlap_threshold:
                suppressed = True
                break
        if not suppressed:
            kept.append(detection)
    return kept


def find_detections(
    scene: np.ndarray | SceneFeatures,
    templates: list[Template],
    threshold: float,
    detection_scale: float = 1.0,
    max_per_template: int = 40,
    nms_iou: float = 0.38,
    max_detections: int = 24,
    structure_weight: float = 0.0,
    search_roi: tuple[int, int, int, int] | None = None,
    nms_across_templates: bool = True,
) -> tuple[list[Detection], float, str | None]:
    """返回画面中的全部模板目标，并通过 NMS 合并同一目标的重复框。

    scene 可传 SceneFeatures，让同一帧的多组检测（怪物、姓名板、头部、称号）复用场景特征。
    search_roi 使用原场景像素坐标；局部匹配结果仍返回原场景坐标。
    """
    if not templates:
        return [], -1.0, None
    features = scene if isinstance(scene, SceneFeatures) else SceneFeatures(scene)
    clipped_roi = clipped_search_roi(search_roi, features.scene.shape)
    if clipped_roi is not None and (clipped_roi[2] <= 0 or clipped_roi[3] <= 0):
        return [], -1.0, None
    scale = max(0.4, min(1.0, float(detection_scale)))
    scaled_origin_x, scaled_origin_y = features.scaled_origin(scale, clipped_roi)
    source = features.scaled(scale, clipped_roi)
    source_opponent = features.opponent(scale, clipped_roi)
    structure_weight = max(0.0, min(0.9, float(structure_weight)))
    source_edges = features.edges(scale, clipped_roi) if structure_weight > 0 else None
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
                        int(round((scaled_origin_x + x) / scale)),
                        int(round((scaled_origin_y + y) / scale)),
                        template.image.shape[1],
                        template.image.shape[0],
                    ),
                    float(result[y, x]),
                    template.name,
                    anchor_offset=template.anchor_offset,
                )
            )

    kept: list[Detection] = []
    for candidate in sorted(candidates, key=lambda item: item.score, reverse=True):
        if all(
            (not nms_across_templates and candidate.name != existing.name)
            or box_iou(candidate.box, existing.box) < nms_iou
            for existing in kept
        ):
            kept.append(candidate)
            if len(kept) >= max_detections:
                break
    return kept, best_score, best_name


def player_tracking_roi(
    point: tuple[float, float],
    scene_width: int,
    scene_height: int,
    width_ratio: float = 0.36,
    up_ratio: float = 0.24,
    down_ratio: float = 0.18,
) -> tuple[int, int, int, int]:
    """围绕预测脚底点生成局部玩家搜索区，靠边时平移而不是缩小。"""
    width = max(1, int(scene_width))
    height = max(1, int(scene_height))
    roi_width = min(width, max(1, int(round(width * max(0.05, min(1.0, float(width_ratio)))))))
    up = max(1, int(round(height * max(0.02, min(1.0, float(up_ratio))))))
    down = max(1, int(round(height * max(0.02, min(1.0, float(down_ratio))))))
    roi_height = min(height, up + down)
    center_x, feet_y = point
    left = int(round(center_x - roi_width / 2.0))
    top = int(round(feet_y - up))
    left = max(0, min(width - roi_width, left))
    top = max(0, min(height - roi_height, top))
    return left, top, roi_width, roi_height


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


def player_attack_anchor(
    player_box: tuple[int, int, int, int],
    raw_box: tuple[int, int, int, int] | None = None,
) -> tuple[float, float]:
    """稳定战斗锚点：统一定位框中心提供 X，顶边提供脚底 Y。"""
    px, feet_y, pw, _ph = player_box
    return px + pw / 2.0, float(feet_y)


def smooth_player_attack_anchor(
    previous: tuple[float, float] | None,
    current: tuple[float, float] | None,
    alpha: float = 0.25,
    snap_distance: float = 60.0,
) -> tuple[float, float] | None:
    """小抖动做 EMA；明显换层、传送或重新定位时直接跳到新锚点。"""
    if current is None:
        return None
    if previous is None:
        return current
    distance = ((current[0] - previous[0]) ** 2 + (current[1] - previous[1]) ** 2) ** 0.5
    if distance >= max(1.0, float(snap_distance)):
        return current
    weight = max(0.0, min(1.0, float(alpha)))
    return (
        previous[0] + (current[0] - previous[0]) * weight,
        previous[1] + (current[1] - previous[1]) * weight,
    )


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
    player_anchor: tuple[float, float] | None = None,
) -> Detection | None:
    """只在框选的攻击区内，选择离玩家水平距离最近的怪物。"""
    if player_box is None:
        return None
    player_x, player_y = player_anchor or player_anchor_center(player_box, raw_box)
    rect = attack_rect_from_player((player_x, player_y), scene_width, scene_height, attack_box, facing)
    return _choose_nearest_eligible(
        detections,
        player_x,
        player_y,
        lambda mx, my: point_in_attack_rect(mx, my, rect),
    )


def choose_nearest_bidirectional_target(
    detections: list[Detection],
    player_box: tuple[int, int, int, int] | None,
    scene_width: int,
    scene_height: int,
    attack_box: dict[str, float],
    raw_box: tuple[int, int, int, int] | None = None,
    player_anchor: tuple[float, float] | None = None,
) -> Detection | None:
    """取左右两个面向索敌区的并集，供会主动向目标转身的原地攻击策略使用。"""
    if player_box is None:
        return None
    player_x, player_y = player_anchor or player_anchor_center(player_box, raw_box)
    left_rect = attack_rect_from_player(
        (player_x, player_y),
        scene_width,
        scene_height,
        attack_box,
        "left",
    )
    right_rect = attack_rect_from_player(
        (player_x, player_y),
        scene_width,
        scene_height,
        attack_box,
        "right",
    )
    return _choose_nearest_eligible(
        detections,
        player_x,
        player_y,
        lambda mx, my: point_in_attack_rect(mx, my, left_rect)
        or point_in_attack_rect(mx, my, right_rect),
    )


def choose_nearest_same_level_target(
    detections: list[Detection],
    player_box: tuple[int, int, int, int] | None,
    scene_width: int,
    scene_height: int,
    attack_box: dict[str, float],
    raw_box: tuple[int, int, int, int] | None = None,
    player_anchor: tuple[float, float] | None = None,
) -> Detection | None:
    """高度仍用框选的上/下范围，水平不限制，供追踪移动使用。"""
    if player_box is None:
        return None
    player_x, player_y = player_anchor or player_anchor_center(player_box, raw_box)
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
    player_anchor: tuple[float, float] | None = None,
) -> dict[str, float]:
    """把客户区矩形原样换成相对角色中心的前/后/上/下，不做对称或取最大修正。"""
    rx, ry, rw, rh = rectangle
    cx, cy, cw, ch = combat_rect
    left, top = float(rx), float(ry)
    right, bottom = float(rx + rw), float(ry + rh)
    local_x, local_y = player_anchor or player_anchor_center(player_box, raw_box)
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
    if detection.anchor_offset is not None:
        anchor_x = x + float(detection.anchor_offset[0])
        feet_y = y + int(round(float(detection.anchor_offset[1])))
    else:
        anchor_x = x + w / 2.0
        if source == "姓名板":
            feet_y = y
        elif source == "头部":
            feet_y = y + h + int(round(float(head_feet_offset) * scene_height))
        elif source == "称号勋章":
            feet_y = y - int(round(float(title_feet_offset) * scene_height))
        else:
            raise ValueError(f"未知玩家定位来源：{source}")
    feet_y = max(0, min(scene_height - 1, feet_y))
    anchor_left = int(round(anchor_x - w / 2.0))
    return PlayerAnchor((anchor_left, feet_y, w, 1), detection.score, source, detection.box)


def choose_fused_player_anchor(
    groups: list[tuple[str, list[Detection]]],
    previous: PlayerAnchor | None,
    scene_width: int,
    scene_height: int,
    head_feet_offset: float,
    title_feet_offset: float,
    max_jump: float,
    agreement_distance: float = 0.07,
    reference_point: tuple[float, float] | None = None,
) -> PlayerAnchor | None:
    """融合三路锚点：先看跨来源相互支持度，再看来源优先级和上帧距离。"""
    previous_center = (
        (float(reference_point[0]), float(reference_point[1]))
        if reference_point is not None
        else None
    )
    if previous_center is None and previous is not None:
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
