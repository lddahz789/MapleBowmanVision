from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import os
import time
from typing import Any

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
    window_config = config["window"]
    needles = [str(x).casefold() for x in window_config.get("title_contains", [])]
    exact_titles = [str(x).casefold() for x in window_config.get("exact_titles", [])]
    executable_needles = [str(x).casefold() for x in window_config.get("executable_contains", [])]
    candidates: list[tuple[int, int, str, str]] = []
    for hwnd, title in visible_windows():
        folded = title.casefold()
        pid, process_path = window_process_path(hwnd)
        if pid == os.getpid():
            continue
        process_folded = process_path.casefold()
        score = 0
        if folded in exact_titles:
            score += 100
        if any(needle in process_folded for needle in executable_needles):
            score += 200
        if any(needle in folded for needle in needles):
            score += 10
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
        if int(user32.GetForegroundWindow()) == window.hwnd:
            time.sleep(settle_seconds)
            return
        time.sleep(0.05)
    raise RuntimeError("无法将游戏切换到前台。请手动点一下游戏窗口后重试。")


def set_window_topmost(window: WindowInfo, enabled: bool) -> None:
    """Keep the game visible while armed without moving, resizing, or focusing it."""
    insert_after = HWND_TOPMOST if enabled else HWND_NOTOPMOST
    flags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
    if not user32.SetWindowPos(window.hwnd, insert_after, 0, 0, 0, 0, flags):
        action = "置顶" if enabled else "取消置顶"
        raise OSError(f"无法{action}游戏窗口")


def capture_client(sct: Any, window: WindowInfo, attempts: int = 3) -> np.ndarray:
    last_error: BaseException | None = None
    for attempt in range(max(1, attempts)):
        current = client_window(window.hwnd, window.title)
        monitor = {
            "left": current.left,
            "top": current.top,
            "width": current.width,
            "height": current.height,
        }
        try:
            shot = np.asarray(sct.grab(monitor), dtype=np.uint8)
            return np.ascontiguousarray(shot[:, :, :3])
        except mss.exception.ScreenShotError as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(0.04)
    assert last_error is not None
    raise last_error
