from __future__ import annotations

import ctypes
from ctypes import wintypes
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
import ntpath
import os
import re
import time
from typing import Any, Iterator

import mss
import numpy as np

from mbv.win32 import (
    HWND_NOTOPMOST,
    HWND_TOPMOST,
    SWP_NOACTIVATE,
    SWP_NOMOVE,
    SWP_NOSIZE,
    SW_RESTORE,
    user32,
    window_process_path,
)

@dataclass
class WindowInfo:
    hwnd: int
    title: str
    left: int
    top: int
    width: int
    height: int
    pid: int = 0


@dataclass(frozen=True)
class WindowTarget:
    hwnd: int
    pid: int
    title: str
    process_path: str
    score: int = 0


_selected_window: ContextVar[WindowTarget | None] = ContextVar("selected_game_window", default=None)


def window_choice_labels(targets: list[WindowTarget]) -> dict[str, WindowTarget]:
    """同一批窗口在不同面板中编号一致，不受枚举顺序或首选排序影响。"""
    groups: dict[str, list[WindowTarget]] = {}
    for target in targets:
        groups.setdefault(target.title or "无标题窗口", []).append(target)
    names = set(groups)
    labels: dict[tuple[int, int], str] = {}
    for name, group in groups.items():
        for index, target in enumerate(sorted(group, key=lambda item: (item.pid, item.hwnd)), 1):
            label = name if len(group) == 1 else f"{name}（窗口 {index}）"
            while label in labels.values() or (label in names and label != name):
                label += "（同名）"
            labels[target.hwnd, target.pid] = label
    return {labels[target.hwnd, target.pid]: target for target in targets}


def identify_window(target: WindowTarget) -> None:
    """有限次闪烁标题栏和任务栏，不切前台、不发键、不修改窗口标题。"""
    class FlashInfo(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("hwnd", wintypes.HWND),
                    ("dwFlags", wintypes.DWORD), ("uCount", wintypes.UINT),
                    ("dwTimeout", wintypes.DWORD)]

    validate_window_target(target)
    info = FlashInfo(ctypes.sizeof(FlashInfo), target.hwnd, 3, 6, 250)
    # 返回值表示调用前的活动状态，不是成功标志。
    user32.FlashWindowEx(ctypes.byref(info))


@contextmanager
def selected_window(target: WindowTarget) -> Iterator[None]:
    """让当前采集操作沿用面板的精确目标，结束后恢复原来的选择作用域。"""
    token = _selected_window.set(target)
    try:
        yield
    finally:
        _selected_window.reset(token)


def _validate_window_identity(hwnd: int, pid: int) -> None:
    if not hwnd or pid <= 0 or not user32.IsWindow(hwnd):
        raise RuntimeError("所选窗口已关闭或失效，请重新选择作用窗口。")
    current_pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(current_pid))
    if int(current_pid.value) != pid:
        raise RuntimeError("所选窗口的进程已变化，请重新选择作用窗口。")


def validate_window_target(target: WindowTarget) -> None:
    """只验证窗口身份；同标题或复用的句柄都不能替代原目标。"""
    _validate_window_identity(target.hwnd, target.pid)


def resolve_window_target(target: WindowTarget) -> WindowInfo:
    validate_window_target(target)
    if user32.IsIconic(target.hwnd):
        raise RuntimeError("所选窗口已最小化，请恢复窗口后重试。")
    window = client_window(target.hwnd, target.title)
    validate_window_target(target)
    return replace(window, pid=target.pid)


def window_target_from_handle(value: str) -> WindowTarget:
    """按明确的 HWND 建立当前会话身份；标题仅用于显示，不参与查找。"""
    text = value.strip()
    if len(text) > 20 or not re.fullmatch(r"(?:0[xX][0-9a-fA-F]+|[0-9]+)", text):
        raise ValueError("请输入十进制句柄，或以 0x 开头的十六进制句柄。")
    hwnd = int(text, 16 if text.lower().startswith("0x") else 10)
    if not 0 < hwnd < 1 << (ctypes.sizeof(ctypes.c_void_p) * 8):
        raise ValueError("窗口句柄必须是有效的正整数。")
    if not user32.IsWindow(hwnd):
        raise RuntimeError("该句柄不存在或窗口已关闭，请重新获取句柄。")
    pid, process_path = window_process_path(hwnd)
    if pid == os.getpid():
        raise RuntimeError("不能将助手自身窗口作为作用窗口。")
    _validate_window_identity(hwnd, pid)
    native_hwnd = wintypes.HWND(hwnd)
    length = user32.GetWindowTextLengthW(native_hwnd)
    buffer = ctypes.create_unicode_buffer(max(1, length + 1))
    user32.GetWindowTextW(native_hwnd, buffer, len(buffer))
    target = WindowTarget(hwnd, pid, buffer.value.strip(), process_path)
    resolve_window_target(target)
    return target


def _window_match_score(config: dict[str, Any], title: str, process_path: str) -> int:
    window_config = config["window"]
    folded = title.casefold()
    process_folded = process_path.casefold()
    score = 0
    if folded in [str(value).casefold() for value in window_config.get("exact_titles", [])]:
        score += 100
    if any(str(value).casefold() in process_folded for value in window_config.get("executable_contains", [])):
        score += 200
    if any(str(value).casefold() in folded for value in window_config.get("title_contains", [])):
        score += 10
    preferred_title = str(window_config.get("preferred_title", "")).strip().casefold()
    preferred_executable = str(window_config.get("preferred_executable", "")).strip().casefold()
    if (
        preferred_title
        and preferred_executable
        and folded == preferred_title
        and ntpath.basename(process_path).casefold() == preferred_executable
    ):
        score += 1000
    return score


def window_candidates(config: dict[str, Any]) -> list[WindowTarget]:
    """枚举可选窗口；旧匹配规则和上次首选只影响排序，不限制手动选择。"""
    found: list[WindowTarget] = []
    own_pid = os.getpid()
    for hwnd, title in visible_windows():
        try:
            pid, process_path = window_process_path(hwnd)
            if not pid or pid == own_pid:
                continue
            target = WindowTarget(hwnd, pid, title, process_path, _window_match_score(config, title, process_path))
            resolve_window_target(target)
        except (OSError, RuntimeError):
            continue
        found.append(target)
    return sorted(found, key=lambda target: target.score, reverse=True)


def visible_windows() -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value.strip()
        if title:
            found.append((int(hwnd), title))
        return True

    cb = callback_type(callback)
    user32.EnumWindows(cb, 0)
    return found


def client_window(hwnd: int, title: str) -> WindowInfo:
    rect = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        raise OSError("无法获取游戏客户区尺寸")
    origin = wintypes.POINT(0, 0)
    if not user32.ClientToScreen(hwnd, ctypes.byref(origin)):
        raise OSError("无法获取游戏客户区屏幕坐标")
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width < 320 or height < 240:
        raise RuntimeError(f"游戏客户区尺寸过小：{width}x{height}")
    return WindowInfo(hwnd, title, origin.x, origin.y, width, height)


def find_game_window(config: dict[str, Any]) -> WindowInfo:
    selected = _selected_window.get()
    if selected is not None:
        return resolve_window_target(selected)
    window_config = config["window"]
    candidates: list[tuple[int, int, str, str]] = []
    for hwnd, title in visible_windows():
        pid, process_path = window_process_path(hwnd)
        if pid == os.getpid():
            continue
        score = _window_match_score(config, title, process_path)
        if score:
            candidates.append((score, hwnd, title, process_path))
    if candidates:
        candidates.sort(key=lambda item: item[0], reverse=True)
        best = candidates[0]
        # 宽松标题命中不能胜过明确的游戏进程或精确标题。
        if best[0] >= 100:
            last_error: BaseException | None = None
            for _attempt in range(30):
                try:
                    return client_window(best[1], best[2])
                except RuntimeError as exc:
                    last_error = exc
                    if "客户区尺寸过小" not in str(exc):
                        raise
                    time.sleep(0.1)
            assert last_error is not None
            raise RuntimeError("游戏窗口当前处于最小化或客户区不可见，请恢复游戏窗口后重试。") from last_error
    wanted = "、".join(window_config.get("exact_titles", window_config.get("title_contains", [])))
    raise RuntimeError(f"没有找到可见的游戏窗口。窗口标题需要包含：{wanted}")


def focus_game_window(window: WindowInfo, settle_seconds: float = 0.8) -> None:
    # Calibration must capture the unobstructed game, not the console that
    # launched this script. This changes focus only; it sends no game keys.
    if window.pid:
        _validate_window_identity(window.hwnd, window.pid)
    if user32.IsIconic(window.hwnd):
        user32.ShowWindow(window.hwnd, SW_RESTORE)
    foreground = int(user32.GetForegroundWindow() or 0)
    current_thread = int(ctypes.windll.kernel32.GetCurrentThreadId())
    foreground_pid = wintypes.DWORD()
    target_pid = wintypes.DWORD()
    foreground_thread = int(user32.GetWindowThreadProcessId(foreground, ctypes.byref(foreground_pid))) if foreground else 0
    target_thread = int(user32.GetWindowThreadProcessId(window.hwnd, ctypes.byref(target_pid)))
    attached_foreground = False
    attached_target = False
    try:
        if foreground_thread and foreground_thread != current_thread:
            attached_foreground = bool(user32.AttachThreadInput(current_thread, foreground_thread, True))
        if target_thread and target_thread != current_thread:
            attached_target = bool(user32.AttachThreadInput(current_thread, target_thread, True))
        if window.pid:
            _validate_window_identity(window.hwnd, window.pid)
        user32.BringWindowToTop(window.hwnd)
        user32.SetActiveWindow(window.hwnd)
        user32.SetForegroundWindow(window.hwnd)
        user32.SetFocus(window.hwnd)
        switch_to_window = getattr(user32, "SwitchToThisWindow", None)
        if switch_to_window is not None:
            switch_to_window(window.hwnd, True)
    finally:
        if attached_target:
            user32.AttachThreadInput(current_thread, target_thread, False)
        if attached_foreground:
            user32.AttachThreadInput(current_thread, foreground_thread, False)
    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline:
        if window.pid:
            _validate_window_identity(window.hwnd, window.pid)
        if int(user32.GetForegroundWindow()) == window.hwnd:
            time.sleep(settle_seconds)
            if window.pid:
                _validate_window_identity(window.hwnd, window.pid)
            return
        time.sleep(0.05)
    raise RuntimeError("无法将游戏切换到前台。请手动点一下游戏窗口后重试。")


def set_window_topmost(window: WindowInfo, enabled: bool) -> None:
    """Keep the game visible while armed without moving, resizing, or focusing it."""
    if window.pid:
        _validate_window_identity(window.hwnd, window.pid)
    insert_after = HWND_TOPMOST if enabled else HWND_NOTOPMOST
    flags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
    if not user32.SetWindowPos(window.hwnd, insert_after, 0, 0, 0, 0, flags):
        action = "置顶" if enabled else "取消置顶"
        raise OSError(f"无法{action}游戏窗口")


def capture_client(sct: Any, window: WindowInfo, attempts: int = 3) -> np.ndarray:
    last_error: BaseException | None = None
    for attempt in range(max(1, attempts)):
        if window.pid:
            _validate_window_identity(window.hwnd, window.pid)
            if user32.IsIconic(window.hwnd):
                raise RuntimeError("所选窗口已最小化，请恢复窗口后重试。")
        current = client_window(window.hwnd, window.title)
        monitor = {
            "left": current.left,
            "top": current.top,
            "width": current.width,
            "height": current.height,
        }
        try:
            if window.pid:
                _validate_window_identity(window.hwnd, window.pid)
            shot = np.asarray(sct.grab(monitor), dtype=np.uint8)
            if window.pid:
                _validate_window_identity(window.hwnd, window.pid)
            return np.ascontiguousarray(shot[:, :, :3])
        except mss.exception.ScreenShotError as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(0.04)
    assert last_error is not None
    raise last_error
