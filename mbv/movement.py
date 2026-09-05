from __future__ import annotations

from dataclasses import dataclass


@dataclass
class StepMotion:
    direction: str
    seconds: float
    started: float
    deadline: float
    phase: str = "prepare"
    attempts: int = 0
    baseline: float | None = None


@dataclass
class MoveProgress:
    direction: str
    baseline: float
    progress_at: float
    ready_at: float
    retried: bool = False


def directed_progress(before: float, after: float, direction: str, width: int) -> float:
    """唯一实时小地图标记的有向位移，单位为原始小地图像素。"""
    return (after - before) * max(1, width) * (1 if direction == "right" else -1)
