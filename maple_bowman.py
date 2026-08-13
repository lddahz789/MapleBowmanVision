from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from game_overlay import RuntimeOverlay, interactive_overlay

if os.name == "nt":
    # 批处理和直接从终端启动时都统一使用 UTF-8，避免中文提示乱码。
    ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    ctypes.windll.kernel32.SetConsoleCP(65001)
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    import cv2
    import mss
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
except ImportError as exc:
    print("缺少运行依赖：", exc)
    print("请先运行 Setup.bat。")
    raise SystemExit(2)


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.json"
ASSET_DIR = ROOT / "assets" / "monsters"
PLAYER_ASSET_DIR = ROOT / "assets" / "player"
PLAYER_HEAD_ASSET_DIR = ROOT / "assets" / "player_head"
PLAYER_TITLE_ASSET_DIR = ROOT / "assets" / "player_title"
LOG_DIR = ROOT / "logs"

user32 = ctypes.windll.user32
try:
    user32.SetProcessDPIAware()
except Exception:
    pass


VK = {
    "backspace": 0x08,
    "tab": 0x09,
    "enter": 0x0D,
    "shift": 0x10,
    "ctrl": 0x11,
    "alt": 0x12,
    "esc": 0x1B,
    "space": 0x20,
    "pageup": 0x21,
    "pagedown": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "insert": 0x2D,
    "delete": 0x2E,
    "f8": 0x77,
    "f9": 0x78,
}
for _char in "abcdefghijklmnopqrstuvwxyz0123456789":
    VK[_char] = ord(_char.upper())


ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    # Included so INPUT has the exact Win32 union size even though this MVP only
    # emits keyboard events. SendInput rejects a shortened INPUT structure.
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", INPUTUNION)]


KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_SCANCODE = 0x0008
INPUT_KEYBOARD = 1
HWND_TOPMOST = wintypes.HWND(-1)
SW_RESTORE = 9
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_SHOWWINDOW = 0x0040
WM_QUIT = 0x0012
WM_HOTKEY = 0x0312
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000
HOTKEY_ID_F8 = 0x4D08
HOTKEY_ID_F9 = 0x4D09
HOTKEY_ID_EXIT = 0x4D0A


def vk_for(name: str) -> int:
    key = name.strip().lower()
    if key not in VK:
        raise ValueError(f"配置中存在不支持的按键：{name!r}")
    return VK[key]


class Keyboard:
    def __init__(self) -> None:
        self.held: set[int] = set()

    @staticmethod
    def _send(vk: int, key_up: bool) -> None:
        scan = int(user32.MapVirtualKeyW(vk, 0))
        if not scan:
            raise OSError(f"无法取得按键扫描码：0x{vk:02X}")
        flags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if key_up else 0)
        if vk in {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E}:
            flags |= KEYEVENTF_EXTENDEDKEY
        event = INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(0, scan, flags, 0, 0))
        sent = user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT))
        if sent != 1:
            raise OSError("键盘输入发送失败")

    def down(self, key: str) -> None:
        code = vk_for(key)
        if code not in self.held:
            self._send(code, False)
            self.held.add(code)

    def up(self, key: str) -> None:
        code = vk_for(key)
        if code in self.held:
            self._send(code, True)
            self.held.discard(code)

    def tap(self, key: str, seconds: float = 0.035) -> None:
        code = vk_for(key)
        self._send(code, False)
        time.sleep(max(0.01, seconds))
        self._send(code, True)

    def release_all(self) -> None:
        for code in list(self.held):
            try:
                self._send(code, True)
            finally:
                self.held.discard(code)


def key_is_down(name: str) -> bool:
    return bool(user32.GetAsyncKeyState(vk_for(name)) & 0x8000)


def rising_edge(name: str, previous: dict[str, bool]) -> bool:
    current = key_is_down(name)
    old = previous.get(name, False)
    previous[name] = current
    return current and not old


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


PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TOKEN_INTEGRITY_LEVEL = 25


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class TOKEN_MANDATORY_LABEL(ctypes.Structure):
    _fields_ = [("Label", SID_AND_ATTRIBUTES)]


def process_integrity_level(pid: int) -> int:
    advapi32 = ctypes.windll.advapi32
    kernel32 = ctypes.windll.kernel32
    advapi32.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
    advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
    advapi32.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    advapi32.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
    process = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not process:
        return -1
    token = wintypes.HANDLE()
    try:
        if not advapi32.OpenProcessToken(process, TOKEN_QUERY, ctypes.byref(token)):
            return -1
        needed = wintypes.DWORD()
        advapi32.GetTokenInformation(token, TOKEN_INTEGRITY_LEVEL, None, 0, ctypes.byref(needed))
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(
            token, TOKEN_INTEGRITY_LEVEL, buffer, needed, ctypes.byref(needed)
        ):
            return -1
        label = ctypes.cast(buffer, ctypes.POINTER(TOKEN_MANDATORY_LABEL)).contents
        count = advapi32.GetSidSubAuthorityCount(label.Label.Sid).contents.value
        return int(advapi32.GetSidSubAuthority(label.Label.Sid, count - 1).contents.value)
    finally:
        if token:
            kernel32.CloseHandle(token)
        kernel32.CloseHandle(process)


def window_process_path(hwnd: int) -> tuple[int, str]:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return 0, ""
    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return int(pid.value), ""
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if ctypes.windll.kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return int(pid.value), buffer.value
        return int(pid.value), ""
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


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
        user32.ShowWindow(window.hwnd, 9)  # SW_RESTORE
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
    scene: np.ndarray,
    templates: list[Template],
    threshold: float,
    detection_scale: float = 1.0,
    max_per_template: int = 40,
    nms_iou: float = 0.38,
    max_detections: int = 24,
    structure_weight: float = 0.0,
) -> tuple[list[Detection], float, str | None]:
    """返回画面中的全部模板目标，并通过 NMS 合并同一目标的重复框。"""
    scale = max(0.4, min(1.0, float(detection_scale)))
    source = scene
    if scale < 0.999:
        source = cv2.resize(scene, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    source_opponent = opponent_colors(source)
    structure_weight = max(0.0, min(0.9, float(structure_weight)))
    source_edges = None
    if structure_weight > 0:
        source_edges = cv2.Canny(cv2.cvtColor(source, cv2.COLOR_BGR2GRAY), 55, 140)
    best_score = -1.0
    best_name = None
    candidates: list[Detection] = []
    for template in templates:
        image = template.image
        mask = template.foreground_mask
        if mask is None:
            mask = template_foreground_mask(image)
        if scale < 0.999:
            image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            mask = cv2.resize(mask, (image.shape[1], image.shape[0]), interpolation=cv2.INTER_NEAREST)
        th, tw = image.shape[:2]
        if th > source.shape[0] or tw > source.shape[1] or th < 4 or tw < 4:
            continue
        template_opponent = opponent_colors(image)
        color_result = cv2.matchTemplate(
            source_opponent,
            template_opponent,
            cv2.TM_CCORR_NORMED,
            mask=mask,
        )
        if structure_weight > 0 and source_edges is not None:
            template_edges = cv2.Canny(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), 55, 140)
            edge_result = cv2.matchTemplate(
                source_edges,
                template_edges,
                cv2.TM_CCORR_NORMED,
                mask=mask,
            )
            result = color_result * (1.0 - structure_weight) + edge_result * structure_weight
        else:
            result = color_result
        result = np.nan_to_num(result, nan=-1.0, posinf=-1.0, neginf=-1.0)
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


def choose_nearest_target(
    detections: list[Detection],
    player_box: tuple[int, int, int, int] | None,
    scene_width: int,
    scene_height: int,
    attack_range: float,
    vertical_tolerance: float,
    player_feet_at_top: bool = False,
) -> Detection | None:
    """只在弓箭有效射程和同一平台高度内，选择离玩家水平距离最近的怪物。"""
    if player_box is None:
        return None
    px, py, pw, ph = player_box
    player_x = px + pw / 2.0
    player_feet = py if player_feet_at_top else py + ph
    max_horizontal = max(1.0, float(attack_range) * scene_width)
    max_vertical = max(1.0, float(vertical_tolerance) * scene_height)
    eligible: list[tuple[float, float, float, Detection]] = []
    for detection in detections:
        x, y, w, h = detection.box
        horizontal = abs((x + w / 2.0) - player_x)
        vertical = abs((y + h) - player_feet)
        if horizontal <= max_horizontal and vertical <= max_vertical:
            eligible.append((horizontal, vertical, -detection.score, detection))
    if not eligible:
        return None
    eligible.sort(key=lambda item: (item[0], item[1], item[2]))
    return eligible[0][3]


def choose_nearest_same_level_target(
    detections: list[Detection],
    player_box: tuple[int, int, int, int] | None,
    scene_height: int,
    vertical_tolerance: float,
    player_feet_at_top: bool = False,
) -> Detection | None:
    """从整个战斗区选择同层最近目标，不限制水平距离，供追踪移动使用。"""
    if player_box is None:
        return None
    px, py, pw, ph = player_box
    player_x = px + pw / 2.0
    player_feet = py if player_feet_at_top else py + ph
    max_vertical = max(1.0, float(vertical_tolerance) * scene_height)
    eligible: list[tuple[float, float, float, Detection]] = []
    for detection in detections:
        x, y, w, h = detection.box
        horizontal = abs((x + w / 2.0) - player_x)
        vertical = abs((y + h) - player_feet)
        if vertical <= max_vertical:
            eligible.append((horizontal, vertical, -detection.score, detection))
    if not eligible:
        return None
    eligible.sort(key=lambda item: (item[0], item[1], item[2]))
    return eligible[0][3]


def choose_player_detection(
    detections: list[Detection],
    previous: tuple[int, int, int, int] | None,
) -> Detection | None:
    if not detections:
        return None
    if previous is None:
        return max(detections, key=lambda item: item.score)
    px, py, pw, ph = previous
    previous_center = (px + pw / 2.0, py + ph / 2.0)
    return min(
        detections,
        key=lambda item: (
            (item.box[0] + item.box[2] / 2.0 - previous_center[0]) ** 2
            + (item.box[1] + item.box[3] / 2.0 - previous_center[1]) ** 2,
            -item.score,
        ),
    )


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


class SessionLog:
    def __init__(self) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.path = LOG_DIR / f"session-{stamp}.jsonl"

    def write(self, event: str, **data: Any) -> None:
        record = {"ts": time.time(), "event": event, **data}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        config = json.load(handle)
    if config.get("version") != 1:
        raise RuntimeError("不支持的配置文件版本")
    return config


def save_config(path: Path, config: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


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


_FONT_CACHE: dict[int, ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}


def chinese_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if size not in _FONT_CACHE:
        candidates = [
            Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "msyh.ttc",
            Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "simhei.ttf",
            Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "simsun.ttc",
        ]
        font = None
        for path in candidates:
            if path.exists():
                font = ImageFont.truetype(str(path), size=size)
                break
        _FONT_CACHE[size] = font or ImageFont.load_default()
    return _FONT_CACHE[size]


def draw_chinese_texts(
    image: np.ndarray,
    items: list[tuple[str, tuple[int, int], tuple[int, int, int], int]],
) -> np.ndarray:
    """一次转换绘制全部中文，颜色参数沿用 OpenCV 的 BGR 顺序。"""
    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    canvas = Image.fromarray(rgb)
    drawer = ImageDraw.Draw(canvas)
    for content, position, bgr, size in items:
        drawer.text(position, content, font=chinese_font(size), fill=(bgr[2], bgr[1], bgr[0]))
    return cv2.cvtColor(np.asarray(canvas), cv2.COLOR_RGB2BGR)


def show_cv_window_on_top(internal_name: str, display_title: str, initial_image: np.ndarray) -> None:
    """显示 OpenCV 窗口，并通过 Unicode Win32 接口置顶和设置中文标题。"""
    cv2.imshow(internal_name, initial_image)
    cv2.waitKey(1)
    hwnd = user32.FindWindowW(None, internal_name)
    if not hwnd:
        raise RuntimeError("校准窗口创建失败")
    user32.SetWindowTextW(hwnd, display_title)
    # Qt/OpenCV 可能沿用同名窗口的上次最小化状态，必须显式恢复。
    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    switch_to_window = getattr(user32, "SwitchToThisWindow", None)
    if switch_to_window is not None:
        switch_to_window(hwnd, True)


def select_region(name: str, frame: np.ndarray) -> dict[str, float]:
    selected = select_roi_chinese(name, frame)
    return normalized_roi(selected, frame.shape)


def select_roi_chinese(name: str, frame: np.ndarray) -> tuple[int, int, int, int]:
    """中文鼠标框选器：拖动框选，回车确认，R 重选，Esc 取消。"""
    height, width = frame.shape[:2]
    scale = min(1.0, 1500.0 / width, 850.0 / height)
    shown = frame if scale == 1.0 else cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    start: list[tuple[int, int]] = []
    end: list[tuple[int, int]] = []
    dragging = [False]

    def callback(event: int, x: int, y: int, _flags: int, _param: Any) -> None:
        if event == cv2.EVENT_LBUTTONDOWN:
            start[:] = [(x, y)]
            end[:] = [(x, y)]
            dragging[0] = True
        elif event == cv2.EVENT_MOUSEMOVE and dragging[0]:
            end[:] = [(x, y)]
        elif event == cv2.EVENT_LBUTTONUP and dragging[0]:
            end[:] = [(x, y)]
            dragging[0] = False

    internal_name = f"MBV-Selector-{os.getpid()}-{time.time_ns()}"
    cv2.namedWindow(internal_name, cv2.WINDOW_AUTOSIZE)
    show_cv_window_on_top(internal_name, name, shown)
    cv2.setMouseCallback(internal_name, callback)
    try:
        while True:
            canvas = shown.copy()
            cv2.rectangle(canvas, (0, 0), (canvas.shape[1], 34), (20, 20, 20), -1)
            if start and end:
                x1, y1 = start[0]
                x2, y2 = end[0]
                cv2.rectangle(canvas, (min(x1, x2), min(y1, y2)), (max(x1, x2), max(y1, y2)), (0, 255, 255), 2)
            canvas = draw_chinese_texts(
                canvas,
                [("按住鼠标左键拖动框选｜回车确认｜R 重新框选｜Esc 取消", (8, 5), (0, 255, 255), 17)],
            )
            cv2.imshow(internal_name, canvas)
            key = cv2.waitKey(20) & 0xFF
            if key in (13, 32) and start and end:
                x1, y1 = start[0]
                x2, y2 = end[0]
                left, top = min(x1, x2), min(y1, y2)
                right, bottom = max(x1, x2), max(y1, y2)
                if right - left >= 4 and bottom - top >= 4:
                    return (
                        int(round(left / scale)),
                        int(round(top / scale)),
                        min(width, int(round(right / scale))) - int(round(left / scale)),
                        min(height, int(round(bottom / scale))) - int(round(top / scale)),
                    )
            elif key in (ord("r"), ord("R")):
                start.clear()
                end.clear()
                dragging[0] = False
            elif key == 27:
                raise RuntimeError(f"已取消框选：{name}")
    finally:
        cv2.destroyWindow(internal_name)


def click_color(name: str, image: np.ndarray) -> tuple[int, int, int]:
    point: list[tuple[int, int]] = []
    shown = image.copy()

    def callback(event: int, x: int, y: int, _flags: int, _param: Any) -> None:
        if event == cv2.EVENT_LBUTTONDOWN:
            point[:] = [(x, y)]

    internal_name = f"MBV-Color-{os.getpid()}-{time.time_ns()}"
    cv2.namedWindow(internal_name, cv2.WINDOW_NORMAL)
    show_cv_window_on_top(internal_name, name, shown)
    cv2.setMouseCallback(internal_name, callback)
    while not point:
        canvas = shown.copy()
        canvas = draw_chinese_texts(canvas, [("请点击自己的小地图标记中心；按 Esc 取消", (8, 5), (0, 255, 255), 16)])
        cv2.imshow(internal_name, canvas)
        if cv2.waitKey(20) & 0xFF == 27:
            cv2.destroyWindow(internal_name)
            raise RuntimeError("已取消玩家标记颜色选择")
    x, y = point[0]
    cv2.destroyWindow(internal_name)
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    patch = hsv[max(0, y - 2) : y + 3, max(0, x - 2) : x + 3]
    median = np.median(patch.reshape(-1, 3), axis=0).astype(int)
    return int(median[0]), int(median[1]), int(median[2])


def calibrate(config_path: Path) -> None:
    config = load_config(config_path)
    window = find_game_window(config)
    print(f"正在校准：{window.title}（{window.width}×{window.height}）")
    print("提示会直接叠加在游戏画面上。拖动框选后按回车或空格确认。")
    focus_game_window(window)
    shape = (window.height, window.width, 3)

    def choose_rectangle(title: str) -> dict[str, float]:
        result = interactive_overlay(window, title, "rectangle")
        if result.cancelled or result.rectangle is None:
            raise RuntimeError(f"已取消校准：{title}")
        return normalized_roi(result.rectangle, shape)

    config["regions"]["hp_bar"] = choose_rectangle("第 1 步：框选血条")
    config["regions"]["mp_bar"] = choose_rectangle("第 2 步：框选蓝条")
    config["regions"]["minimap"] = choose_rectangle("第 3 步：框选整个小地图内部画面")
    print("第 4 步请框选整个可见游戏场景，不要只框角色当前所在的平台；排除底部状态栏即可。")
    config["regions"]["combat"] = choose_rectangle("第 4 步：框选整个可见游戏场景（排除底部状态栏）")
    minimap_rect = roi_pixels(shape, config["regions"]["minimap"])
    point_result = interactive_overlay(window, "第 5 步：点击自己的小地图标记中心", "point", minimap_rect)
    if point_result.cancelled or point_result.point is None:
        raise RuntimeError("已取消玩家标记颜色选择")
    focus_game_window(window, settle_seconds=0.15)
    with mss.MSS() as sct:
        frame = capture_client(sct, window)
    px, py = point_result.point
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    patch = hsv[max(0, py - 2) : py + 3, max(0, px - 2) : px + 3]
    median = np.median(patch.reshape(-1, 3), axis=0).astype(int)
    hue, saturation, value = int(median[0]), int(median[1]), int(median[2])
    config["vision"]["player_hsv_ranges"] = hue_ranges(hue, saturation, value)
    config["calibrated"] = True
    config["calibration"] = {
        "window_size": [window.width, window.height],
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "player_hsv_sample": [hue, saturation, value],
    }
    save_config(config_path, config)
    print(f"校准完成，配置已保存到：{config_path}")


def capture_template(config_path: Path) -> None:
    config = load_config(config_path)
    window = find_game_window(config)
    focus_game_window(window)
    result = interactive_overlay(window, "框选一只清晰可见、没有遮挡的怪物", "rectangle")
    if result.cancelled or result.rectangle is None:
        raise RuntimeError("已取消怪物模板框选")
    focus_game_window(window, settle_seconds=0.15)
    with mss.MSS() as sct:
        frame = capture_client(sct, window)
    x, y, w, h = result.rectangle
    image = frame[y : y + h, x : x + w]
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    path = ASSET_DIR / f"monster-{datetime.now().strftime('%Y%m%d-%H%M%S')}.png"
    ok, encoded = cv2.imencode(".png", image)
    if not ok:
        raise RuntimeError("无法保存怪物模板图片")
    encoded.tofile(path)
    print(f"怪物模板已保存：{path}")


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


def capture_player_template(config_path: Path) -> None:
    config = load_config(config_path)
    window = find_game_window(config)
    focus_game_window(window)
    result = interactive_overlay(
        window,
        "紧贴框选自己姓名板的第一行蓝色板（含名字），不要包含角色、宠物或怪物",
        "rectangle",
    )
    if result.cancelled or result.rectangle is None:
        raise RuntimeError("已取消玩家模板框选")
    focus_game_window(window, settle_seconds=0.15)
    with mss.MSS() as sct:
        frame = capture_client(sct, window)
    x, y, w, h = result.rectangle
    image = frame[y : y + h, x : x + w]
    alpha = nameplate_template_alpha(image)
    bgra = cv2.cvtColor(image, cv2.COLOR_BGR2BGRA)
    bgra[:, :, 3] = alpha
    PLAYER_ASSET_DIR.mkdir(parents=True, exist_ok=True)
    path = PLAYER_ASSET_DIR / f"nameplate-{datetime.now().strftime('%Y%m%d-%H%M%S')}.png"
    ok, encoded = cv2.imencode(".png", bgra)
    if not ok:
        raise RuntimeError("无法保存玩家模板图片")
    encoded.tofile(path)
    print(f"玩家姓名板模板已保存：{path}")


def capture_player_aux_template(config_path: Path, kind: str) -> None:
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
    focus_game_window(window)
    result = interactive_overlay(window, prompt, "rectangle")
    if result.cancelled or result.rectangle is None:
        raise RuntimeError(f"已取消{label}模板框选")
    focus_game_window(window, settle_seconds=0.15)
    with mss.MSS() as sct:
        frame = capture_client(sct, window)
    x, y, w, h = result.rectangle
    image = frame[y : y + h, x : x + w]
    alpha = player_template_alpha(image) if kind == "head" else nameplate_template_alpha(image)
    bgra = cv2.cvtColor(image, cv2.COLOR_BGR2BGRA)
    bgra[:, :, 3] = alpha
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{prefix}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.png"
    ok, encoded = cv2.imencode(".png", bgra)
    if not ok:
        raise RuntimeError(f"无法保存{label}模板图片")
    encoded.tofile(path)
    print(f"{label}模板已保存：{path}")


def draw_roi(frame: np.ndarray, roi: dict[str, float], color: tuple[int, int, int]) -> None:
    x, y, w, h = roi_pixels(frame.shape, roi)
    cv2.rectangle(frame, (x, y), (x + w, y + h), color, 1)


STATE_LABELS = {
    "OBSERVER": "观察模式",
    "PAUSED": "已暂停",
    "SCANNING": "正在搜索目标",
    "PATROL_LEFT": "向左巡逻",
    "PATROL_RIGHT": "向右巡逻",
    "ATTACK_LEFT": "向左攻击",
    "ATTACK_RIGHT": "向右攻击",
    "CHASE_LEFT": "向左接近目标",
    "CHASE_RIGHT": "向右接近目标",
    "PLAYER_SCREEN_LOST": "正在识别玩家位置",
    "TARGET_OUT_OF_RANGE": "战斗区暂无同层目标",
    "HP_POTION": "正在使用回血药",
    "MP_POTION": "正在使用回蓝药",
    "PICKUP": "正在拾取",
    "MARKER_LOST": "玩家标记丢失",
}


def runtime_limit_reached(
    started_at: float,
    now: float,
    max_runtime_minutes: float,
) -> bool:
    """非正数表示不限制运行时长；正数按分钟计算截止时间。"""
    minutes = float(max_runtime_minutes)
    return minutes > 0.0 and now - started_at >= minutes * 60.0


PREVIEW_INTERNAL_NAME = "MBV-Preview"
PREVIEW_TITLE_OBSERVER = "冒险岛弓箭手视觉助手——观察模式"
PREVIEW_TITLE_INPUT = "冒险岛弓箭手视觉助手——输入模式"


class BowmanBot:
    def __init__(self, config: dict[str, Any], input_authorized: bool) -> None:
        self.config = config
        self.input_authorized = input_authorized
        self.keyboard = Keyboard()
        self.templates = load_templates()
        self.player_templates = load_templates(PLAYER_ASSET_DIR)
        self.player_head_templates = load_templates(PLAYER_HEAD_ASSET_DIR)
        self.player_title_templates = load_templates(PLAYER_TITLE_ASSET_DIR)
        self.log = SessionLog()
        self.armed = False
        self.state = "OBSERVER"
        self.direction: str | None = None
        self.marker: tuple[float, float] | None = None
        self.marker_last_seen = time.monotonic()
        self.last_target_seen = 0.0
        self.last_attack = 0.0
        self.last_hp = 0.0
        self.last_mp = 0.0
        self.last_pickup = 0.0
        self.started_at = 0.0
        self.hotkey_state: dict[str, bool] = {}
        self.hotkey_stop = threading.Event()
        self.hotkey_thread_id = 0
        self.f8_requested = threading.Event()
        self.f9_requested = threading.Event()
        self.notice = ""
        self.notice_until = 0.0
        self.last_detection_box: tuple[int, int, int, int] | None = None
        self.last_detection_score = -1.0
        self.last_detection_name: str | None = None
        self.last_detection_at = 0.0
        self.last_detections: list[Detection] = []
        self.last_player_anchor: PlayerAnchor | None = None
        self.last_player_at = 0.0
        self.integrity_ok = True

    def notify(self, message: str, seconds: float = 4.0) -> None:
        self.notice = message
        self.notice_until = time.monotonic() + seconds
        self.log.write("notice", message=message)
        print(message)

    def monitor_hotkeys(self) -> None:
        self.hotkey_thread_id = int(ctypes.windll.kernel32.GetCurrentThreadId())
        registered: list[int] = []
        try:
            bindings = (
                ("f8", HOTKEY_ID_F8, 0),
                ("f9", HOTKEY_ID_F9, 0),
                ("q", HOTKEY_ID_EXIT, MOD_CONTROL | MOD_SHIFT),
            )
            for name, hotkey_id, modifiers in bindings:
                if not user32.RegisterHotKey(None, hotkey_id, modifiers | MOD_NOREPEAT, vk_for(name)):
                    label = "Ctrl+Shift+Q" if hotkey_id == HOTKEY_ID_EXIT else name.upper()
                    raise OSError(f"无法注册全局热键 {label}")
                registered.append(hotkey_id)
            self.log.write("hotkey_listener", mode="global")
            msg = wintypes.MSG()
            while not self.hotkey_stop.is_set():
                status = int(user32.GetMessageW(ctypes.byref(msg), None, 0, 0))
                if status <= 0:
                    break
                if int(msg.message) != WM_HOTKEY:
                    continue
                if int(msg.wParam) == HOTKEY_ID_F8:
                    self.log.write("hotkey_pressed", key="F8")
                    self.f8_requested.set()
                elif int(msg.wParam) == HOTKEY_ID_F9:
                    self.log.write("hotkey_pressed", key="F9")
                    self.f9_requested.set()
                elif int(msg.wParam) == HOTKEY_ID_EXIT:
                    self.log.write("hotkey_pressed", key="Ctrl+Shift+Q")
                    self.f9_requested.set()
        except OSError as exc:
            # 极少数软件会抢占全局热键；这种情况下继续使用原有轮询方案。
            for hotkey_id in registered:
                user32.UnregisterHotKey(None, hotkey_id)
            registered.clear()
            self.log.write("hotkey_listener", mode="polling_fallback", error=str(exc))
            while not self.hotkey_stop.is_set():
                if rising_edge("f8", self.hotkey_state):
                    self.f8_requested.set()
                if rising_edge("f9", self.hotkey_state):
                    self.f9_requested.set()
                if rising_edge("q", self.hotkey_state) and key_is_down("ctrl") and key_is_down("shift"):
                    self.f9_requested.set()
                time.sleep(0.02)
        finally:
            for hotkey_id in registered:
                user32.UnregisterHotKey(None, hotkey_id)
            self.hotkey_thread_id = 0

    def stop_hotkey_monitor(self) -> None:
        self.hotkey_stop.set()
        thread_id = self.hotkey_thread_id
        if thread_id:
            user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0)

    def request_exit(self) -> None:
        self.log.write("exit_requested", source="overlay_button")
        self.f9_requested.set()

    def disarm(self, reason: str) -> None:
        was_armed = self.armed
        self.armed = False
        self.state = "PAUSED"
        self.keyboard.release_all()
        if was_armed:
            self.log.write("disarm", reason=reason)
            self.notify(f"已暂停：{reason}", 5.0)
            user32.MessageBeep(0x00000030)

    def toggle(self, window: WindowInfo) -> None:
        if self.armed:
            self.disarm("按下了 F8")
            return
        if not self.input_authorized:
            self.notify("当前是观察模式，不会发送按键。请关闭后运行 Start-Bot.bat。", 6.0)
            return
        if not self.config.get("calibrated"):
            self.notify("尚未完成校准，请先运行 Start-Calibrate.bat。")
            return
        if not self.player_templates:
            self.notify("尚未采集玩家姓名板模板，请先运行 Capture-Player-Template.bat。", 8.0)
            return
        if not self.integrity_ok:
            self.notify("启动失败：助手权限低于游戏。请关闭后重新运行 Start-Bot.bat，并在 UAC 中选择“是”。", 8.0)
            return
        if int(user32.GetForegroundWindow()) != window.hwnd:
            try:
                focus_game_window(window, settle_seconds=0.15)
            except Exception as exc:
                self.notify(f"启动失败：无法切换到游戏窗口（{exc}）", 6.0)
                return
        if int(user32.GetForegroundWindow()) != window.hwnd:
            self.notify("启动失败：游戏没有进入前台，请点击游戏后重试。", 6.0)
            return
        self.armed = True
        self.state = "SCANNING"
        self.started_at = time.monotonic()
        self.log.write("arm", window=window.title, templates=len(self.templates))
        self.notify("已经启动。按 F8 暂停，按 F9 或 Ctrl+Shift+Q 立即退出。", 3.0)
        user32.MessageBeep(0x00000040)

    def move(self, direction: str) -> None:
        keys = self.config["keys"]
        opposite = "left" if direction == "right" else "right"
        self.keyboard.up(keys[opposite])
        self.keyboard.down(keys[direction])
        self.direction = direction
        self.state = f"PATROL_{direction.upper()}"

    def stop_move(self) -> None:
        keys = self.config["keys"]
        self.keyboard.up(keys["left"])
        self.keyboard.up(keys["right"])

    def face_and_attack(self, target_x: float, player_x: float, now: float) -> None:
        behavior = self.config["behavior"]
        keys = self.config["keys"]
        self.stop_move()
        desired = "left" if target_x < player_x else "right"
        dead = float(behavior["attack_dead_zone"])
        if abs(target_x - player_x) <= dead and self.direction is not None:
            desired = self.direction
        self.direction = desired
        self.state = f"ATTACK_{desired.upper()}"
        if now - self.last_attack >= float(behavior["attack_interval_seconds"]):
            # 每一箭前都重新点按目标方向，避免内部方向状态与游戏实际朝向脱节。
            self.keyboard.tap(keys[desired], float(behavior["face_tap_seconds"]))
            self.keyboard.tap(keys["attack"])
            self.last_attack = now
            self.log.write(
                "attack",
                direction=desired,
                player_x=round(player_x, 3),
                target_x=round(target_x, 3),
                distance=round(abs(target_x - player_x), 3),
            )

    def chase_target(self, target_x: float, player_x: float) -> None:
        desired = "left" if target_x < player_x else "right"
        self.move(desired)
        self.state = f"CHASE_{desired.upper()}"

    def act(
        self,
        window: WindowInfo,
        hp: float,
        mp: float,
        marker: tuple[float, float] | None,
        player_box: tuple[int, int, int, int] | None,
        target_box: tuple[int, int, int, int] | None,
        chase_box: tuple[int, int, int, int] | None,
        combat_width: int,
        has_monster_candidates: bool,
        now: float,
    ) -> None:
        if not self.armed:
            return
        if int(user32.GetForegroundWindow()) != window.hwnd:
            self.disarm("游戏不再位于前台")
            return
        behavior = self.config["behavior"]
        keys = self.config["keys"]
        if runtime_limit_reached(
            self.started_at,
            now,
            float(behavior.get("max_runtime_minutes", 0)),
        ):
            self.disarm("已达到单次最长运行时间")
            return
        if marker is None and now - self.marker_last_seen >= float(behavior["max_marker_lost_seconds"]):
            self.disarm("小地图玩家标记丢失")
            return
        if hp <= float(behavior["hp_threshold"]) and now - self.last_hp >= float(behavior["potion_cooldown_seconds"]):
            self.stop_move()
            self.keyboard.tap(keys["hp_potion"])
            self.last_hp = now
            self.state = "HP_POTION"
            self.log.write("hp_potion", fill=round(hp, 3))
            return
        if mp <= float(behavior["mp_threshold"]) and now - self.last_mp >= float(behavior["potion_cooldown_seconds"]):
            self.stop_move()
            self.keyboard.tap(keys["mp_potion"])
            self.last_mp = now
            self.state = "MP_POTION"
            self.log.write("mp_potion", fill=round(mp, 3))
            return
        if player_box is None:
            self.stop_move()
            self.state = "PLAYER_SCREEN_LOST"
            return
        if target_box is not None:
            self.last_target_seen = now
            target_x = (target_box[0] + target_box[2] / 2) / max(1, combat_width)
            player_x = (player_box[0] + player_box[2] / 2) / max(1, combat_width)
            self.face_and_attack(target_x, player_x, now)
            return
        if chase_box is not None:
            self.last_target_seen = now
            target_x = (chase_box[0] + chase_box[2] / 2) / max(1, combat_width)
            player_x = (player_box[0] + player_box[2] / 2) / max(1, combat_width)
            self.chase_target(target_x, player_x)
            return
        if has_monster_candidates:
            self.stop_move()
            self.state = "TARGET_OUT_OF_RANGE"
            return
        target_lost_for = now - self.last_target_seen
        if bool(behavior["pickup_after_target_lost"]) and 0.25 < target_lost_for < 0.8 and now - self.last_pickup > 1.0:
            self.keyboard.tap(keys["pickup"])
            self.last_pickup = now
            self.state = "PICKUP"
            return
        if not bool(behavior["fallback_patrol"]) or target_lost_for < float(behavior["target_lost_patrol_delay_seconds"]):
            self.stop_move()
            self.state = "SCANNING"
            return
        if marker is None:
            self.stop_move()
            self.state = "MARKER_LOST"
            return
        x = marker[0]
        if x <= float(behavior["patrol_left"]):
            self.direction = "right"
        elif x >= float(behavior["patrol_right"]):
            self.direction = "left"
        self.move(self.direction or "right")

    def run(self, overlay: RuntimeOverlay) -> None:
        window = find_game_window(self.config)
        game_pid, _game_path = window_process_path(window.hwnd)
        current_pid = int(ctypes.windll.kernel32.GetCurrentProcessId())
        current_integrity = process_integrity_level(current_pid)
        game_integrity = process_integrity_level(game_pid)
        self.integrity_ok = current_integrity < 0 or game_integrity < 0 or current_integrity >= game_integrity
        fps = max(2.0, float(self.config["capture"]["fps"]))
        frame_period = 1.0 / fps
        vision = self.config["vision"]
        print(f"游戏窗口：{window.title}（{window.width}×{window.height}）")
        print(f"已载入怪物模板：{len(self.templates)} 个")
        print(
            "已载入玩家定位模板："
            f"姓名板 {len(self.player_templates)} / 头部 {len(self.player_head_templates)} / "
            f"称号勋章 {len(self.player_title_templates)}"
        )
        if self.input_authorized and not self.integrity_ok:
            print("权限不足：助手权限低于游戏，输入会被 Windows 阻止。")
        print("F8 启动或暂停，F9 / Ctrl+Shift+Q 立即退出。按键输入：" + ("已允许。" if self.input_authorized else "未允许（观察模式）。"))
        self.log.write(
            "session_start",
            input_authorized=self.input_authorized,
            templates=len(self.templates),
            player_templates=len(self.player_templates),
            player_head_templates=len(self.player_head_templates),
            player_title_templates=len(self.player_title_templates),
        )
        hotkey_thread = threading.Thread(target=self.monitor_hotkeys, name="MapleHotkeys", daemon=True)
        hotkey_thread.start()
        capture_failures = 0
        try:
            with mss.MSS() as sct:
                while True:
                    loop_start = time.monotonic()
                    if self.f9_requested.is_set():
                        self.f9_requested.clear()
                        break
                    if self.f8_requested.is_set():
                        self.f8_requested.clear()
                        self.toggle(window)
                    try:
                        frame = capture_client(sct, window)
                        capture_failures = 0
                    except mss.exception.ScreenShotError as exc:
                        capture_failures += 1
                        if capture_failures == 1 or capture_failures % 30 == 0:
                            self.log.write("capture_retry", failures=capture_failures, error=str(exc))
                        if self.armed:
                            self.disarm("游戏画面暂时无法截取")
                        time.sleep(0.1)
                        continue
                    hp_img, hp_rect = crop(frame, self.config["regions"]["hp_bar"])
                    mp_img, mp_rect = crop(frame, self.config["regions"]["mp_bar"])
                    minimap_img, minimap_rect = crop(frame, self.config["regions"]["minimap"])
                    combat_img, combat_rect = crop(frame, self.config["regions"]["combat"])
                    hp = bar_fill(hp_img, vision["hp_hsv_ranges"])
                    mp = bar_fill(mp_img, vision["mp_hsv_ranges"])
                    marker, _mask = player_marker(
                        minimap_img,
                        vision["player_hsv_ranges"],
                        int(vision["player_blob_min_area"]),
                        int(vision["player_blob_max_area"]),
                        self.marker,
                    )
                    now = time.monotonic()
                    if marker is not None:
                        self.marker = marker
                        self.marker_last_seen = now
                    detected_monsters, detected_score, detected_name = find_detections(
                        combat_img,
                        self.templates,
                        float(vision["monster_template_threshold"]),
                        float(vision.get("monster_detection_scale", 1.0)),
                    )
                    if detected_monsters:
                        self.last_detections = detected_monsters
                        self.last_detection_box = detected_monsters[0].box
                        self.last_detection_score = detected_score
                        self.last_detection_name = detected_name
                        self.last_detection_at = now
                    hold_seconds = float(vision.get("monster_hold_seconds", 0.0))
                    if not detected_monsters and now - self.last_detection_at <= hold_seconds:
                        monsters = self.last_detections
                    else:
                        monsters = detected_monsters

                    player_detections, player_score, _player_template_name = find_detections(
                        combat_img,
                        self.player_templates,
                        float(vision.get("player_template_threshold", 0.76)),
                        float(vision.get("player_detection_scale", 0.5)),
                        max_per_template=8,
                        nms_iou=0.35,
                        max_detections=8,
                        structure_weight=0.55,
                    )
                    head_detections, head_score, _head_template_name = find_detections(
                        combat_img,
                        self.player_head_templates,
                        float(vision.get("player_head_threshold", 0.76)),
                        float(vision.get("player_detection_scale", 0.5)),
                        max_per_template=8,
                        nms_iou=0.35,
                        max_detections=8,
                        structure_weight=0.35,
                    )
                    title_detections, title_score, _title_template_name = find_detections(
                        combat_img,
                        self.player_title_templates,
                        float(vision.get("player_title_threshold", 0.70)),
                        float(vision.get("player_detection_scale", 0.5)),
                        max_per_template=8,
                        nms_iou=0.35,
                        max_detections=8,
                        structure_weight=0.55,
                    )
                    player_anchor = choose_fused_player_anchor(
                        [
                            ("姓名板", player_detections),
                            ("头部", head_detections),
                            ("称号勋章", title_detections),
                        ],
                        self.last_player_anchor,
                        combat_img.shape[1],
                        combat_img.shape[0],
                        float(vision.get("player_head_feet_offset", 0.07)),
                        float(vision.get("player_title_feet_offset", 0.076)),
                        float(vision.get("player_anchor_max_jump", 0.18)),
                        float(vision.get("player_anchor_agreement", 0.07)),
                    )
                    if player_anchor is not None:
                        self.last_player_anchor = player_anchor
                        self.last_player_at = now
                    player_hold = float(vision.get("player_hold_seconds", 0.8))
                    if player_anchor is not None:
                        active_player_anchor = player_anchor
                    elif now - self.last_player_at <= player_hold:
                        active_player_anchor = self.last_player_anchor
                    else:
                        active_player_anchor = None
                    player_box = active_player_anchor.box if active_player_anchor is not None else None
                    player_source = active_player_anchor.source if active_player_anchor is not None else ""
                    source_scores = {
                        "姓名板": player_score,
                        "头部": head_score,
                        "称号勋章": title_score,
                    }
                    active_player_score = (
                        active_player_anchor.score
                        if active_player_anchor is not None
                        else source_scores.get(player_source, -1.0)
                    )

                    target = choose_nearest_target(
                        monsters,
                        player_box,
                        combat_img.shape[1],
                        combat_img.shape[0],
                        float(self.config["behavior"].get("bow_attack_range", 0.312)),
                        float(self.config["behavior"].get("bow_vertical_tolerance", 0.12)),
                        player_feet_at_top=True,
                    )
                    chase_target = None
                    if target is None:
                        chase_target = choose_nearest_same_level_target(
                            monsters,
                            player_box,
                            combat_img.shape[0],
                            float(self.config["behavior"].get("bow_vertical_tolerance", 0.12)),
                            player_feet_at_top=True,
                        )
                    box = target.box if target is not None else None
                    chase_box = chase_target.box if chase_target is not None else None
                    selected_target = target if target is not None else chase_target
                    score = selected_target.score if selected_target is not None else detected_score
                    target_distance_px = None
                    target_direction = ""
                    if selected_target is not None and player_box is not None:
                        target_center = selected_target.box[0] + selected_target.box[2] / 2.0
                        player_center = player_box[0] + player_box[2] / 2.0
                        target_distance_px = int(round(abs(target_center - player_center)))
                        target_direction = "左" if target_center < player_center else "右"
                    self.act(
                        window,
                        hp,
                        mp,
                        marker,
                        player_box,
                        box,
                        chase_box,
                        combat_img.shape[1],
                        bool(monsters),
                        now,
                    )
                    current_window = client_window(window.hwnd, window.title)
                    monster_box = None
                    chase_screen_box = None
                    monster_boxes: list[tuple[int, int, int, int]] = []
                    player_screen_box = None
                    attack_range_box = None
                    marker_screen = None
                    if marker is not None:
                        mx = minimap_rect[0] + int(marker[0] * minimap_rect[2])
                        my = minimap_rect[1] + int(marker[1] * minimap_rect[3])
                        marker_screen = (mx, my)
                    for detection in monsters:
                        dx, dy, dw, dh = detection.box
                        monster_boxes.append((combat_rect[0] + dx, combat_rect[1] + dy, dw, dh))
                    if box is not None:
                        bx = combat_rect[0] + box[0]
                        by = combat_rect[1] + box[1]
                        monster_box = (bx, by, box[2], box[3])
                    if chase_box is not None:
                        cx = combat_rect[0] + chase_box[0]
                        cy = combat_rect[1] + chase_box[1]
                        chase_screen_box = (cx, cy, chase_box[2], chase_box[3])
                    if player_box is not None:
                        px, py, pw, ph = player_box
                        raw_player_box = active_player_anchor.raw_box if active_player_anchor is not None else player_box
                        rx, ry, rw, rh = raw_player_box
                        player_screen_box = (combat_rect[0] + rx, combat_rect[1] + ry, rw, rh)
                        horizontal = int(float(self.config["behavior"].get("bow_attack_range", 0.312)) * combat_rect[2])
                        vertical = int(float(self.config["behavior"].get("bow_vertical_tolerance", 0.12)) * combat_rect[3])
                        center_x = px + pw // 2
                        feet_y = py
                        range_left = max(0, center_x - horizontal)
                        range_right = min(combat_rect[2], center_x + horizontal)
                        range_top = max(0, feet_y - vertical)
                        range_bottom = min(combat_rect[3], feet_y + vertical)
                        attack_range_box = (
                            combat_rect[0] + range_left,
                            combat_rect[1] + range_top,
                            range_right - range_left,
                            range_bottom - range_top,
                        )
                    state_label = STATE_LABELS.get(self.state, self.state)
                    if not self.player_templates:
                        banner = "缺少玩家姓名板模板｜请先运行 Capture-Player-Template.bat"
                    elif self.input_authorized:
                        if not self.integrity_ok:
                            banner = "输入权限不足｜请关闭后重新运行 Start-Bot.bat，并在 UAC 中选择“是”"
                        else:
                            banner = f"{'运行中' if self.armed else '输入待命'}｜{state_label}｜F8 启动/暂停｜F9 / Ctrl+Shift+Q 退出"
                    else:
                        banner = "仅观察，不会发送按键｜如需启动，请关闭本窗口后运行 Start-Bot.bat｜F9 / Ctrl+Shift+Q 退出"
                    overlay.update(
                        {
                            "left": current_window.left,
                            "top": current_window.top,
                            "width": current_window.width,
                            "height": current_window.height,
                            "armed": self.armed,
                            "input_authorized": self.input_authorized,
                            "banner": banner,
                            "notice": self.notice if now <= self.notice_until else "",
                            "hp": hp,
                            "mp": mp,
                            "hp_roi": self.config["regions"]["hp_bar"],
                            "mp_roi": self.config["regions"]["mp_bar"],
                            "minimap_roi": self.config["regions"]["minimap"],
                            "combat_roi": self.config["regions"]["combat"],
                            "marker_screen": marker_screen,
                            "player_box": player_screen_box,
                            "player_score": active_player_score,
                            "player_source": player_source,
                            "attack_range_box": attack_range_box,
                            "monster_boxes": monster_boxes,
                            "monster_box": monster_box,
                            "chase_box": chase_screen_box,
                            "monster_score": score,
                            "target_distance_px": target_distance_px,
                            "target_direction": target_direction,
                            "monster_threshold": float(vision["monster_template_threshold"]),
                        }
                    )
                    elapsed = time.monotonic() - loop_start
                    if elapsed < frame_period:
                        time.sleep(frame_period - elapsed)
        finally:
            self.stop_hotkey_monitor()
            hotkey_thread.join(timeout=1.0)
            self.disarm("程序退出")
            overlay.close()
            self.log.write("session_end")
            print(f"运行日志：{self.log.path}")


def list_windows() -> None:
    for hwnd, title in visible_windows():
        print(f"0x{hwnd:08X}  {title}")


class ChineseArgumentParser(argparse.ArgumentParser):
    def format_usage(self) -> str:
        return super().format_usage().replace("usage: ", "用法：", 1)

    def format_help(self) -> str:
        text = super().format_help().replace("usage: ", "用法：", 1)
        return text.replace("options:\n", "选项：\n", 1)

    def error(self, message: str) -> None:
        replacements = {
            "unrecognized arguments:": "无法识别的参数：",
            "the following arguments are required:": "缺少必填参数：",
            "expected one argument": "需要提供一个参数值",
            "invalid choice:": "无效选项：",
        }
        for english, chinese in replacements.items():
            message = message.replace(english, chinese)
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}：参数错误：{message}\n")


def parse_args() -> argparse.Namespace:
    parser = ChineseArgumentParser(description="冒险岛弓箭手纯视觉固定地图原型", add_help=False)
    parser.add_argument("-h", "--help", action="help", help="显示这段帮助信息并退出")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="配置文件路径")
    parser.add_argument("--calibrate", action="store_true", help="进行画面区域校准")
    parser.add_argument("--capture-template", action="store_true", help="采集怪物模板")
    parser.add_argument("--capture-player-template", action="store_true", help="采集玩家模板")
    parser.add_argument("--capture-player-head", action="store_true", help="采集玩家头部模板")
    parser.add_argument("--capture-player-title", action="store_true", help="采集玩家称号勋章模板")
    parser.add_argument("--list-windows", action="store_true", help="列出可见窗口")
    parser.add_argument("--enable-input", action="store_true", help="允许发送按键；仍需按 F8 才会启动")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = args.config.resolve()
    if args.list_windows:
        list_windows()
        return 0
    if not config_path.exists():
        print(f"找不到配置文件：{config_path}")
        return 2
    if args.calibrate:
        calibrate(config_path)
        return 0
    if args.capture_template:
        capture_template(config_path)
        return 0
    if args.capture_player_template:
        capture_player_template(config_path)
        return 0
    if args.capture_player_head:
        capture_player_aux_template(config_path, "head")
        return 0
    if args.capture_player_title:
        capture_player_aux_template(config_path, "title")
        return 0
    config = load_config(config_path)
    bot = BowmanBot(config, input_authorized=bool(args.enable_input))
    overlay = RuntimeOverlay()
    overlay.set_exit_handler(bot.request_exit)
    worker_errors: list[BaseException] = []

    def worker() -> None:
        try:
            bot.run(overlay)
        except BaseException as exc:
            worker_errors.append(exc)
            overlay.close()

    thread = threading.Thread(target=worker, name="MapleVisionWorker", daemon=False)
    thread.start()
    overlay.mainloop()
    thread.join(timeout=3.0)
    if worker_errors:
        raise worker_errors[0]
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        if os.name == "nt" and Path(sys.executable).name.casefold() == "pythonw.exe":
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            crash_path = LOG_DIR / f"crash-{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt"
            import traceback

            crash_path.write_text(traceback.format_exc(), encoding="utf-8")
            user32.MessageBoxW(
                None,
                f"启动失败：{exc}\n\n详细信息已保存到：\n{crash_path}",
                "冒险岛弓箭手视觉助手",
                0x10,
            )
        raise SystemExit(1)
