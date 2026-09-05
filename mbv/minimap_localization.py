"""小地图遮挡补位的背景稳定校验，不把地图坐标直接换成屏幕坐标。"""
from __future__ import annotations

import cv2
import numpy as np

from mbv.vision import SceneFeatures


def background_snapshot(scene: SceneFeatures) -> np.ndarray:
    scale = min(1.0, 320.0 / max(1, scene.scene.shape[1]))
    return cv2.cvtColor(scene.scaled(scale), cv2.COLOR_BGR2GRAY)


def background_is_stable(reference: np.ndarray | None, current: np.ndarray) -> bool:
    """固定基准的 4×3 分块，多处分散的纹理必须仍在原位；不滚动更新基准。

    只作保守的沿用门禁，不估算镜头位移。大片特效、空白、切图或镜头滚动
    导致证据不足时拒绝延长补位；客户端返回缓存截图仍无法由此排除。
    """
    if reference is None or reference.shape != current.shape or min(current.shape) < 24:
        return False
    height, width = current.shape
    matches: list[tuple[int, int]] = []
    for row in range(3):
        for column in range(4):
            region = np.s_[row * height // 3:(row + 1) * height // 3,
                           column * width // 4:(column + 1) * width // 4]
            before = reference[region].astype(np.float32)
            after = current[region].astype(np.float32)
            if float(before.std()) < 10.0 or float(after.std()) < 10.0:
                continue
            if float(np.abs(before - after).mean()) > 6.0:
                continue
            correlation = float(cv2.matchTemplate(after, before, cv2.TM_CCOEFF_NORMED)[0, 0])
            if correlation >= 0.98:
                matches.append((row, column))
    return (len(matches) >= 7 and len({row for row, _ in matches}) >= 2
            and len({column for _, column in matches}) >= 3)
