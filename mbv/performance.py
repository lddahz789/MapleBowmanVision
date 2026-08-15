from __future__ import annotations

from collections import deque
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import math
import os
import threading
import time
from typing import Callable, Mapping


STATE_WARMING_UP = "warming_up"
STATE_RUNNING = "running"
STATE_SUSPENDED = "suspended"
STATE_CAPTURE_ERROR = "capture_error"
STATE_ERROR = "error"
STATE_STOPPED = "stopped"

STAGE_ORDER = (
    "capture",
    "preprocess",
    "monster",
    "player",
    "targeting",
    "action",
    "hud",
)
STAGE_LABELS = {
    "capture": "截图",
    "preprocess": "预处理",
    "monster": "怪物",
    "player": "玩家",
    "targeting": "选敌",
    "action": "决策/输入",
    "hud": "HUD",
}


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("page_fault_count", ctypes.c_ulong),
        ("peak_working_set_size", ctypes.c_size_t),
        ("working_set_size", ctypes.c_size_t),
        ("quota_peak_paged_pool_usage", ctypes.c_size_t),
        ("quota_paged_pool_usage", ctypes.c_size_t),
        ("quota_peak_non_paged_pool_usage", ctypes.c_size_t),
        ("quota_non_paged_pool_usage", ctypes.c_size_t),
        ("pagefile_usage", ctypes.c_size_t),
        ("peak_pagefile_usage", ctypes.c_size_t),
    ]


def current_process_memory_bytes() -> int | None:
    """返回当前进程工作集字节数；API 不可用时不影响视觉主循环。"""
    try:
        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return None
        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        get_current_process = windll.kernel32.GetCurrentProcess
        get_current_process.argtypes = []
        get_current_process.restype = wintypes.HANDLE
        get_process_memory_info = windll.psapi.GetProcessMemoryInfo
        get_process_memory_info.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ProcessMemoryCounters),
            wintypes.DWORD,
        ]
        get_process_memory_info.restype = wintypes.BOOL
        process = get_current_process()
        if not get_process_memory_info(process, ctypes.byref(counters), counters.cb):
            return None
        return int(counters.working_set_size)
    except (AttributeError, OSError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class StageSnapshot:
    name: str
    average_ms: float
    max_ms: float
    sample_count: int


@dataclass(frozen=True, slots=True)
class PerformanceSnapshot:
    state: str
    target_fps: float
    fps: float
    sample_count: int
    total_frames: int
    frame_ms: float
    frame_p95_ms: float
    frame_max_ms: float
    over_budget_ratio: float
    stages: tuple[StageSnapshot, ...]
    cpu_percent: float | None
    memory_mb: float | None
    total_capture_failures: int
    consecutive_failures: int
    error: str
    mode: str
    last_frame_age_seconds: float | None


@dataclass(frozen=True, slots=True)
class _FrameSample:
    started_ns: int
    finished_ns: int
    stages_ns: tuple[tuple[str, int], ...]
    mode: str
    target_fps: float

    @property
    def duration_ns(self) -> int:
        return self.finished_ns - self.started_ns


def _positive_fps(value: float) -> float:
    fps = float(value)
    if not math.isfinite(fps) or fps <= 0.0:
        raise ValueError("目标 FPS 必须是有限正数")
    return fps


def _nanoseconds(value: int | float, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} 必须是非负纳秒数")
    if isinstance(value, int):
        result = value
    elif isinstance(value, float) and math.isfinite(value) and value.is_integer():
        result = int(value)
    else:
        raise ValueError(f"{label} 必须是非负纳秒数")
    if result < 0:
        raise ValueError(f"{label} 必须是非负纳秒数")
    return result


def _nearest_rank_p95(values: list[int]) -> int:
    ordered = sorted(values)
    rank = max(1, math.ceil(len(ordered) * 0.95))
    return ordered[rank - 1]


class PerformanceMonitor:
    """视觉线程写入、Tk 主线程读取的有界性能统计器。"""

    def __init__(
        self,
        target_fps: float,
        *,
        window_size: int = 120,
        clock_ns: Callable[[], int] | None = None,
        cpu_clock: Callable[[], float] | None = None,
        memory_reader: Callable[[], float | int | None] | None = None,
        logical_cpu_count: int | None = None,
    ) -> None:
        if int(window_size) < 2:
            raise ValueError("性能统计窗口至少需要 2 帧")
        self._lock = threading.Lock()
        self._samples: deque[_FrameSample] = deque(maxlen=int(window_size))
        self._clock_ns = clock_ns or time.perf_counter_ns
        self._cpu_clock = cpu_clock or time.process_time
        self._memory_reader = memory_reader or current_process_memory_bytes
        cpu_count_value = (os.cpu_count() or 1) if logical_cpu_count is None else logical_cpu_count
        self._logical_cpu_count = int(cpu_count_value)
        if self._logical_cpu_count < 1:
            raise ValueError("逻辑处理器数量至少为 1")
        self._target_fps = _positive_fps(target_fps)
        self._state = STATE_WARMING_UP
        self._total_frames = 0
        self._total_capture_failures = 0
        self._consecutive_failures = 0
        self._error = ""
        self._mode = "full"
        self._last_started_ns: int | None = None
        try:
            initial_wall_ns = _nanoseconds(self._clock_ns(), "性能时钟")
        except (TypeError, ValueError):
            initial_wall_ns = 0
        try:
            initial_cpu_seconds = float(self._cpu_clock())
        except (OSError, TypeError, ValueError):
            initial_cpu_seconds = math.nan
        self._resource_wall_ns: int | None = initial_wall_ns
        self._resource_cpu_seconds: float | None = (
            initial_cpu_seconds if math.isfinite(initial_cpu_seconds) else None
        )
        self._cpu_percent: float | None = 0.0
        self._memory_mb: float | None = self._read_memory()

    def record_frame(
        self,
        *,
        started_ns: int,
        finished_ns: int,
        stages_ns: Mapping[str, int],
        mode: str = "full",
        target_fps: float | None = None,
    ) -> bool:
        start = _nanoseconds(started_ns, "帧开始时间")
        finish = _nanoseconds(finished_ns, "帧结束时间")
        if finish < start:
            raise ValueError("帧结束时间不能早于开始时间")
        fps = self._target_fps if target_fps is None else _positive_fps(target_fps)
        normalized_stages: list[tuple[str, int]] = []
        for raw_name, raw_value in stages_ns.items():
            name = str(raw_name).strip()
            if name not in STAGE_ORDER:
                raise ValueError(f"未知性能阶段：{name or '(空)'}")
            normalized_stages.append((name, _nanoseconds(raw_value, f"{name} 阶段耗时")))
        normalized_stages.sort(
            key=lambda item: (
                STAGE_ORDER.index(item[0]) if item[0] in STAGE_ORDER else len(STAGE_ORDER),
                item[0],
            )
        )
        normalized_mode = str(mode).strip()
        if normalized_mode not in ("full", "lightweight", "potion_only"):
            raise ValueError(f"未知性能模式：{normalized_mode or '(空)'}")
        sample = _FrameSample(start, finish, tuple(normalized_stages), normalized_mode, fps)

        with self._lock:
            if self._state in (STATE_SUSPENDED, STATE_ERROR, STATE_STOPPED):
                return False
            if self._last_started_ns is not None and start < self._last_started_ns:
                raise ValueError("帧开始时间不能倒退")
            if self._samples and sample.mode != self._mode:
                # 完整识别与独立喝药的工作量不可直接比较；切换时重新建立节奏窗口。
                self._samples.clear()
                self._last_started_ns = None
            self._target_fps = fps
            self._samples.append(sample)
            self._last_started_ns = start
            self._total_frames += 1
            self._consecutive_failures = 0
            self._error = ""
            self._mode = sample.mode
            self._state = STATE_RUNNING
            self._sample_resources_locked(finish)
        return True

    def record_capture_failure(self, error: BaseException | str, *, now_ns: int | None = None) -> None:
        now = _nanoseconds(self._clock_ns() if now_ns is None else now_ns, "截图失败时间")
        with self._lock:
            if self._state in (STATE_SUSPENDED, STATE_ERROR, STATE_STOPPED):
                return
            self._total_capture_failures += 1
            self._consecutive_failures += 1
            self._error = _error_summary(error)
            self._state = STATE_CAPTURE_ERROR
            self._sample_resources_locked(now)

    def set_suspended(self, suspended: bool, *, now_ns: int | None = None) -> None:
        _nanoseconds(self._clock_ns() if now_ns is None else now_ns, "状态更新时间")
        with self._lock:
            if self._state == STATE_STOPPED:
                return
            if suspended:
                if self._state != STATE_ERROR:
                    self._state = STATE_SUSPENDED
                return
            if self._state == STATE_ERROR:
                return
            self._samples.clear()
            self._last_started_ns = None
            self._consecutive_failures = 0
            self._error = ""
            self._state = STATE_WARMING_UP

    def mark_error(self, error: BaseException | str, *, now_ns: int | None = None) -> None:
        _nanoseconds(self._clock_ns() if now_ns is None else now_ns, "异常时间")
        with self._lock:
            if self._state == STATE_STOPPED:
                return
            self._state = STATE_ERROR
            self._error = _error_summary(error)

    def stop(self, *, now_ns: int | None = None) -> None:
        _nanoseconds(self._clock_ns() if now_ns is None else now_ns, "停止时间")
        with self._lock:
            if self._state != STATE_ERROR:
                self._state = STATE_STOPPED

    def update_target_fps(self, target_fps: float) -> None:
        fps = _positive_fps(target_fps)
        with self._lock:
            self._target_fps = fps

    def snapshot(self) -> PerformanceSnapshot:
        now_ns = _nanoseconds(self._clock_ns(), "快照时间")
        with self._lock:
            self._sample_resources_locked(now_ns)
            samples = tuple(self._samples)
            state = self._state
            target_fps = self._target_fps
            total_frames = self._total_frames
            total_capture_failures = self._total_capture_failures
            consecutive_failures = self._consecutive_failures
            error = self._error
            mode = self._mode
            cpu_percent = self._cpu_percent
            memory_mb = self._memory_mb
        durations = [sample.duration_ns for sample in samples]
        sample_count = len(samples)
        frame_ms = (sum(durations) / sample_count / 1_000_000.0) if durations else 0.0
        frame_p95_ms = (_nearest_rank_p95(durations) / 1_000_000.0) if durations else 0.0
        frame_max_ms = (max(durations) / 1_000_000.0) if durations else 0.0
        fps = 0.0
        if sample_count >= 2:
            span_ns = samples[-1].started_ns - samples[0].started_ns
            if span_ns > 0:
                fps = (sample_count - 1) * 1_000_000_000.0 / span_ns
        over_budget = sum(
            sample.duration_ns > 1_000_000_000.0 / sample.target_fps
            for sample in samples
        )
        over_budget_ratio = over_budget / sample_count if sample_count else 0.0

        stage_values: dict[str, list[int]] = {}
        for sample in samples:
            for name, value in sample.stages_ns:
                stage_values.setdefault(name, []).append(value)
        ordered_names = [name for name in STAGE_ORDER if name in stage_values]
        ordered_names.extend(sorted(name for name in stage_values if name not in STAGE_ORDER))
        stages = tuple(
            StageSnapshot(
                name=name,
                average_ms=sum(stage_values[name]) / len(stage_values[name]) / 1_000_000.0,
                max_ms=max(stage_values[name]) / 1_000_000.0,
                sample_count=len(stage_values[name]),
            )
            for name in ordered_names
        )
        last_frame_age_seconds = None
        if samples:
            last_frame_age_seconds = max(
                0.0,
                (now_ns - samples[-1].finished_ns) / 1_000_000_000.0,
            )
        return PerformanceSnapshot(
            state=state,
            target_fps=target_fps,
            fps=fps,
            sample_count=sample_count,
            total_frames=total_frames,
            frame_ms=frame_ms,
            frame_p95_ms=frame_p95_ms,
            frame_max_ms=frame_max_ms,
            over_budget_ratio=over_budget_ratio,
            stages=stages,
            cpu_percent=cpu_percent,
            memory_mb=memory_mb,
            total_capture_failures=total_capture_failures,
            consecutive_failures=consecutive_failures,
            error=error,
            mode=mode,
            last_frame_age_seconds=last_frame_age_seconds,
        )

    def _sample_resources_locked(self, now_ns: int) -> None:
        try:
            cpu_seconds = float(self._cpu_clock())
        except (OSError, TypeError, ValueError):
            cpu_seconds = math.nan
        if self._resource_wall_ns is None:
            self._resource_wall_ns = now_ns
            self._resource_cpu_seconds = cpu_seconds if math.isfinite(cpu_seconds) else None
            self._memory_mb = self._read_memory()
            return
        elapsed_ns = now_ns - self._resource_wall_ns
        if elapsed_ns < 1_000_000_000:
            return
        if self._resource_cpu_seconds is not None and math.isfinite(cpu_seconds) and elapsed_ns > 0:
            cpu_delta = max(0.0, cpu_seconds - self._resource_cpu_seconds)
            total_capacity_seconds = (
                elapsed_ns / 1_000_000_000.0 * self._logical_cpu_count
            )
            self._cpu_percent = min(100.0, cpu_delta * 100.0 / total_capacity_seconds)
        self._resource_wall_ns = now_ns
        self._resource_cpu_seconds = cpu_seconds if math.isfinite(cpu_seconds) else None
        self._memory_mb = self._read_memory()

    def _read_memory(self) -> float | None:
        try:
            value_bytes = self._memory_reader()
            if value_bytes is None:
                return None
            memory_bytes = float(value_bytes)
            if not math.isfinite(memory_bytes) or memory_bytes < 0.0:
                return None
            return memory_bytes / (1024.0 * 1024.0)
        except (OSError, TypeError, ValueError):
            return None


def _error_summary(error: BaseException | str) -> str:
    if isinstance(error, BaseException):
        return f"{type(error).__name__}: {error}"
    return str(error)


def format_performance_summary(snapshot: PerformanceSnapshot) -> str:
    if snapshot.state == STATE_SUSPENDED:
        return "已暂停（采集工具打开）"
    if snapshot.state == STATE_ERROR:
        detail = snapshot.error or "未知异常"
        return f"视觉线程异常：{detail}"
    if snapshot.state == STATE_STOPPED:
        return "已停止"
    stale_after = max(1.5, 3.0 / snapshot.target_fps)
    if (
        snapshot.state == STATE_RUNNING
        and snapshot.last_frame_age_seconds is not None
        and snapshot.last_frame_age_seconds > stale_after
    ):
        return f"主循环停滞 {snapshot.last_frame_age_seconds:.1f} 秒，正在等待新帧"
    if snapshot.sample_count < 2:
        if snapshot.state == STATE_CAPTURE_ERROR:
            return f"截图暂时不可用 × {snapshot.consecutive_failures}"
        return "等待性能数据…"

    cpu = "—" if snapshot.cpu_percent is None else f"{snapshot.cpu_percent:.0f}%"
    memory = "—" if snapshot.memory_mb is None else f"{snapshot.memory_mb:.0f} MB"
    prefix = ""
    if snapshot.state == STATE_CAPTURE_ERROR:
        prefix = f"截图异常 × {snapshot.consecutive_failures}｜"
    first = (
        f"{prefix}FPS {snapshot.fps:.1f}/{snapshot.target_fps:g}｜"
        f"帧 {snapshot.frame_ms:.1f} ms（P95 {snapshot.frame_p95_ms:.1f}）｜"
        f"超预算 {snapshot.over_budget_ratio:.0%}｜CPU {cpu}｜内存 {memory}"
    )
    by_name = {stage.name: stage for stage in snapshot.stages}
    stage_parts = []
    for name in STAGE_ORDER:
        stage = by_name.get(name)
        value = "—" if stage is None else f"{stage.average_ms:.1f}"
        stage_parts.append(f"{STAGE_LABELS[name]} {value}")
    mode = "轻量喝药" if snapshot.mode in ("lightweight", "potion_only") else "完整识别"
    return first + "\n" + "｜".join(stage_parts) + f" ms｜{mode}"
