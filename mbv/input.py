from __future__ import annotations

import ctypes
from ctypes import wintypes
import threading
import time
from typing import Any

from mbv.win32 import (
    EXTENDED_VKS,
    GA_ROOT,
    GW_CHILD,
    INPUT,
    INPUT_KEYBOARD,
    KEYBDINPUT,
    KEYEVENTF_EXTENDEDKEY,
    KEYEVENTF_KEYUP,
    KEYEVENTF_SCANCODE,
    SMTO_ABORTIFHUNG,
    WA_ACTIVE,
    WM_ACTIVATE,
    WM_CHAR,
    WM_KEYDOWN,
    WM_KEYUP,
    WM_SETFOCUS,
    user32,
)

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
    "f7": 0x76,
    "f8": 0x77,
    "f9": 0x78,
}
for _char in "abcdefghijklmnopqrstuvwxyz0123456789":
    VK[_char] = ord(_char.upper())
VK_BY_CODE = {code: name for name, code in VK.items()}


def vk_for(name: str) -> int:
    key = name.strip().lower()
    if key not in VK:
        raise ValueError(f"配置中存在不支持的按键：{name!r}")
    return VK[key]


def name_for_vk(code: int) -> str:
    if code not in VK_BY_CODE:
        raise ValueError(f"不支持的虚拟键：0x{code:02X}")
    return VK_BY_CODE[code]


def input_delivery(config: dict[str, Any]) -> str:
    raw = str(config.get("input", {}).get("delivery", "foreground")).strip().lower()
    if raw in {"window_message", "message_only"}:
        return "window_message"
    if raw in {"background", "postmessage", "window"}:
        return "background"
    if raw in {"foreground", "sendinput", "focus"}:
        return "foreground"
    raise ValueError(f"不支持的按键投递方式：{raw!r}，请使用 foreground、background 或 window_message")


def key_lparam(vk: int, key_up: bool, *, was_down: bool = False, repeat: int = 1) -> int:
    scan = int(user32.MapVirtualKeyW(vk, 0))
    if not scan:
        raise OSError(f"无法取得按键扫描码：0x{vk:02X}")
    extended = 1 if vk in EXTENDED_VKS else 0
    previous = 1 if key_up or was_down else 0
    transition = 1 if key_up else 0
    return (
        (repeat & 0xFFFF)
        | ((scan & 0xFF) << 16)
        | (extended << 24)
        | (previous << 30)
        | (transition << 31)
    )


def window_class_name(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value


def child_windows(root: int) -> list[int]:
    found: list[int] = []
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd: int, _lparam: int) -> bool:
        found.append(int(hwnd))
        return True

    cb = callback_type(callback)
    user32.EnumChildWindows(wintypes.HWND(root), cb, 0)
    return found


def resolve_input_hwnd(root: int) -> int:
    """后台按键打到真正吃键盘的窗口：优先 MapleStory 子窗口，其次客户区最大的子窗口。"""
    root = int(root)
    named_children: list[int] = []
    named_all: list[int] = []
    fallback: list[tuple[int, int]] = []
    for hwnd in [root, *child_windows(root)]:
        class_name = window_class_name(hwnd).casefold()
        if "maplestory" in class_name:
            named_all.append(hwnd)
            if hwnd != root:
                named_children.append(hwnd)
        rect = wintypes.RECT()
        if hwnd != root and user32.GetClientRect(hwnd, ctypes.byref(rect)):
            area = max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
            if area:
                fallback.append((area, hwnd))
    if named_children:
        return named_children[0]
    if named_all:
        return named_all[0]
    first_child = int(user32.GetWindow(root, GW_CHILD) or 0)
    if first_child:
        return first_child
    if fallback:
        fallback.sort(reverse=True)
        return fallback[0][1]
    return root


def window_is_foreground(hwnd: int) -> bool:
    if not hwnd:
        return False
    foreground = int(user32.GetForegroundWindow() or 0)
    if not foreground:
        return False
    if foreground == hwnd:
        return True
    root = int(user32.GetAncestor(hwnd, GA_ROOT) or hwnd)
    return foreground == root or bool(user32.IsChild(root, foreground))


class Keyboard:
    def __init__(self, delivery: str = "foreground") -> None:
        if delivery not in {"foreground", "background", "window_message"}:
            raise ValueError(f"不支持的按键投递方式：{delivery!r}")
        self.delivery = delivery
        self.root_hwnd = 0
        self.hwnd = 0
        self.held: set[int] = set()
        self._hardware_down: set[int] = set()
        self._lock = threading.RLock()
        self._repeat_stop = threading.Event()
        self._repeat_thread: threading.Thread | None = None
        self._movement_pulses: dict[int, tuple[bool, float]] = {}
        self._movement_deadlines: dict[int, float] = {}
        self._repeat_error: OSError | None = None

    def bind_window(self, hwnd: int) -> None:
        self.root_hwnd = int(hwnd)
        self.hwnd = resolve_input_hwnd(hwnd)

    def _ensure_repeat_thread(self) -> None:
        if self.delivery not in {"background", "window_message"} and not self._movement_deadlines:
            return
        thread = self._repeat_thread
        if thread is not None and thread.is_alive() and not self._repeat_stop.is_set():
            return
        self._repeat_stop = threading.Event()
        self._repeat_thread = threading.Thread(
            target=self._repeat_held_keys,
            args=(self._repeat_stop,),
            name="MapleKeyRepeat",
            daemon=True,
        )
        self._repeat_thread.start()

    def _repeat_held_keys(self, stop: threading.Event) -> None:
        while not stop.wait(0.05):
            with self._lock:
                if stop.is_set():
                    return
                for vk in list(self.held):
                    try:
                        self._repeat_key(vk, time.monotonic())
                    except OSError as exc:
                        self._repeat_error = exc
                        break

    def _repeat_key(self, vk: int, now: float) -> None:
        """调用方持锁；移动脉冲保留真实的抬键间隙及新的 keydown 边沿。"""
        if vk in self._movement_deadlines and now >= self._movement_deadlines[vk]:
            self.up(VK_BY_CODE[vk])
            return
        if self.delivery == "foreground":
            return
        pulse = self._movement_pulses.get(vk)
        if pulse is None:
            self._dispatch(vk, key_up=False, was_down=True)
            return
        is_down, due = pulse
        if now < due:
            return
        self._dispatch(vk, key_up=is_down, was_down=is_down)
        self._movement_pulses[vk] = (not is_down, now + (0.05 if is_down else 0.10))

    def check_health(self) -> None:
        with self._lock:
            if self._repeat_error is not None:
                raise OSError(f"后台持续按键投递失败：{self._repeat_error}")

    def movement_down(self, key: str, seconds: float | None = None) -> None:
        """只对纯窗口模式的位移启用脉冲；攻击/转向和旧扫描码路径不变。"""
        code = vk_for(key)
        with self._lock:
            self.check_health()
            if self.delivery != "window_message":
                self.down(key)
            elif code not in self.held:
                self._dispatch(code, False)
                self.held.add(code)
                self._movement_pulses[code] = (True, time.monotonic() + 0.10)
            if seconds is not None:
                self._movement_deadlines[code] = time.monotonic() + max(0.03, min(0.5, seconds))
            self._ensure_repeat_thread()

    def _release_hardware(self) -> None:
        for vk in list(self._hardware_down):
            try:
                self._send_input(vk, True)
            except OSError:
                pass
            self._hardware_down.discard(vk)

    def _post_targets(self) -> list[int]:
        targets: list[int] = []
        for hwnd in (self.root_hwnd, self.hwnd):
            hwnd = int(hwnd or 0)
            if hwnd and hwnd not in targets and user32.IsWindow(hwnd):
                targets.append(hwnd)
        return targets

    def _post(self, vk: int, key_up: bool, was_down: bool = False) -> None:
        targets = self._post_targets()
        if not targets:
            raise OSError("后台按键失败：游戏窗口句柄无效")
        lparam = key_lparam(vk, key_up, was_down=was_down)
        message = WM_KEYUP if key_up else WM_KEYDOWN
        posted_any = False
        for hwnd in targets:
            result = ctypes.c_size_t()
            if not key_up and not was_down:
                user32.SendMessageTimeoutW(hwnd, WM_ACTIVATE, WA_ACTIVE, 0, SMTO_ABORTIFHUNG, 30, ctypes.byref(result))
                user32.SendMessageTimeoutW(hwnd, WM_SETFOCUS, 0, 0, SMTO_ABORTIFHUNG, 30, ctypes.byref(result))
            posted = bool(user32.PostMessageW(hwnd, message, vk, lparam))
            timed = bool(
                user32.SendMessageTimeoutW(
                    hwnd, message, vk, lparam, SMTO_ABORTIFHUNG, 40, ctypes.byref(result)
                )
            )
            if not key_up and vk not in EXTENDED_VKS and (0x30 <= vk <= 0x5A):
                user32.PostMessageW(hwnd, WM_CHAR, vk, lparam)
            posted_any = posted_any or posted or timed
        if not posted_any:
            raise OSError("后台按键发送失败")

    def _send_input(self, vk: int, key_up: bool) -> None:
        scan = int(user32.MapVirtualKeyW(vk, 0))
        if not scan:
            raise OSError(f"无法取得按键扫描码：0x{vk:02X}")
        flags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if key_up else 0)
        if vk in EXTENDED_VKS:
            flags |= KEYEVENTF_EXTENDEDKEY
        event = INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(0, scan, flags, 0, 0))
        sent = user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT))
        if sent != 1:
            raise OSError("键盘输入发送失败")

    def _dispatch(self, vk: int, key_up: bool, *, was_down: bool = False) -> None:
        if self.delivery == "window_message":
            # 实验模式只向一个已绑定窗口排队，不重复 SendMessage、不发 WM_CHAR，
            # 也不伪造焦点事件或回退到全局扫描码。
            if not self.hwnd or not user32.IsWindow(self.hwnd):
                raise OSError("独立后台按键失败：游戏窗口句柄无效")
            message = WM_KEYUP if key_up else WM_KEYDOWN
            if not user32.PostMessageW(
                self.hwnd, message, vk, key_lparam(vk, key_up, was_down=was_down)
            ):
                raise OSError("独立后台按键投递失败，请检查游戏权限")
            return
        # Classic MapleStory reads GetAsyncKeyState / DirectInput, not WM_KEY*.
        # Switching to PostMessage-only after unfocus therefore does nothing, and
        # releasing hardware keys first makes GetAsyncKeyState go back up.
        if self.delivery == "background" and not window_is_foreground(self.root_hwnd or self.hwnd):
            try:
                self._post(vk, key_up, was_down=was_down)
            except OSError:
                pass
        if self.delivery == "foreground":
            self._send_input(vk, key_up)
            return
        if key_up:
            if vk in self._hardware_down:
                self._send_input(vk, True)
                self._hardware_down.discard(vk)
            return
        if vk not in self._hardware_down:
            self._send_input(vk, False)
            self._hardware_down.add(vk)

    def down(self, key: str) -> None:
        code = vk_for(key)
        with self._lock:
            self.check_health()
            if code in self.held:
                return
            self._dispatch(code, False)
            self.held.add(code)
            self._ensure_repeat_thread()

    def up(self, key: str) -> None:
        code = vk_for(key)
        with self._lock:
            if code not in self.held:
                return
            self._dispatch(code, True, was_down=True)
            self.held.discard(code)
            self._movement_pulses.pop(code, None)
            self._movement_deadlines.pop(code, None)

    def tap(self, key: str, seconds: float = 0.035) -> None:
        code = vk_for(key)
        try:
            self._dispatch(code, False)
            time.sleep(max(0.01, seconds))
        except BaseException:
            # 后台投递可能已先发出 WM_KEYDOWN、再在 SendInput 阶段报错；仍要尽力补发抬键。
            try:
                self._dispatch(code, True, was_down=True)
            except BaseException:
                pass
            raise
        else:
            self._dispatch(code, True, was_down=True)

    def release_all(self) -> None:
        with self._lock:
            self._repeat_stop.set()
            held = list(self.held)
            for code in held:
                try:
                    self._dispatch(code, True, was_down=True)
                except OSError:
                    pass
            self.held.clear()
            self._movement_pulses.clear()
            self._movement_deadlines.clear()
            self._repeat_error = None
            self._release_hardware()
            thread = self._repeat_thread
            self._repeat_thread = None
        if thread is not None and thread is not threading.current_thread() and thread.is_alive():
            thread.join(timeout=0.3)


def key_is_down(name: str) -> bool:
    return bool(user32.GetAsyncKeyState(vk_for(name)) & 0x8000)


def rising_edge(name: str, previous: dict[str, bool]) -> bool:
    current = key_is_down(name)
    old = previous.get(name, False)
    previous[name] = current
    return current and not old
