from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
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
    if raw == "hybrid":
        return "hybrid"
    if raw in {"window_message", "message_only"}:
        return "window_message"
    if raw in {"background", "postmessage", "window"}:
        return "background"
    if raw in {"foreground", "sendinput", "focus"}:
        return "foreground"
    raise ValueError(f"不支持的按键投递方式：{raw!r}，请使用 foreground、background、window_message 或 hybrid")


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


@dataclass(frozen=True)
class _HybridPress:
    channel: str
    hwnd: int
    root: int
    pid: int


class Keyboard:
    def __init__(self, delivery: str = "foreground") -> None:
        if delivery not in {"foreground", "background", "window_message", "hybrid"}:
            raise ValueError(f"不支持的按键投递方式：{delivery!r}")
        self.delivery = delivery
        self.root_hwnd = 0
        self.hwnd = 0
        # 仅面板显式选窗时启用；一次读取完整快照，重复线程不拼接新旧绑定。
        self._bound_identity: tuple[int, int, int] | None = None
        self.held: set[int] = set()
        self._hardware_down: set[int] = set()
        self._hybrid_presses: dict[int, _HybridPress] = {}
        self.last_skill_channel: str | None = None
        self._lock = threading.RLock()
        self._repeat_stop = threading.Event()
        self._repeat_thread: threading.Thread | None = None
        self._movement_pulses: dict[int, tuple[bool, float]] = {}
        self._movement_deadlines: dict[int, float] = {}
        self._hold_repeat_at: dict[int, float] = {}
        self._repeat_error: OSError | None = None
        self.hybrid = None
        if delivery == "hybrid":
            from mbv.hybrid_movement import HybridMovement
            self.hybrid = HybridMovement(lambda vk, key_up: self._send_input(vk, key_up))

    @staticmethod
    def _window_has_pid(hwnd: int, expected_pid: int) -> bool:
        if not hwnd or not user32.IsWindow(hwnd):
            return False
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return bool(expected_pid and int(pid.value) == expected_pid)

    def _binding_valid(self) -> bool:
        identity = self._bound_identity
        if identity is None:
            return True  # 旧 CLI/未显式选窗调用不增加系统查询。
        root, target, pid = identity
        return self._window_has_pid(root, pid) and self._window_has_pid(target, pid)

    def _assert_bound_identity(self) -> None:
        if not self._binding_valid():
            raise OSError("所选输入窗口已关闭或身份变化，请重新选择窗口")

    def bind_window(self, hwnd: int, *, expected_pid: int | None = None) -> None:
        root = int(hwnd)
        if expected_pid is not None:
            expected_pid = int(expected_pid)
            if not self._window_has_pid(root, expected_pid):
                raise OSError("所选输入窗口已关闭或身份变化，请重新选择窗口")
        with self._lock:
            if self.hybrid is not None or expected_pid is not None or self._bound_identity is not None:
                # 旧键必须沿旧窗口/旧通道释放，不能先换 HWND 再清理。
                self.release_all()
                if self.held or self._hardware_down or self._hybrid_presses:
                    raise OSError("旧窗口按键尚未释放，不能重新绑定窗口")
            target = resolve_input_hwnd(root)
            if expected_pid is not None and (
                not self._window_has_pid(root, expected_pid)
                or not self._window_has_pid(target, expected_pid)
            ):
                raise OSError("所选窗口或输入子窗口身份变化，请重新选择窗口")
            if self.hybrid is not None:
                self.hybrid.bind(root)
                if expected_pid is not None and self.hybrid.pid != expected_pid:
                    raise OSError("所选输入窗口身份变化，请重新选择窗口")
                self._repeat_error = None
            self.root_hwnd, self.hwnd = root, target
            self._bound_identity = None if expected_pid is None else (root, target, expected_pid)

    def _ensure_repeat_thread(self) -> None:
        if self.delivery not in {"background", "window_message", "hybrid"} and not self._movement_deadlines:
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
                try:
                    self._poll_hybrid_presses()
                except OSError as exc:
                    self._repeat_error = exc
                for vk in list(self.held):
                    try:
                        self._repeat_key(vk, time.monotonic())
                    except OSError as exc:
                        if self._bound_identity is not None:
                            try:
                                self.release_all()
                            except OSError as release_error:
                                exc = OSError(f"{exc}；{release_error}")
                        self._repeat_error = exc
                        break

    def _repeat_key(self, vk: int, now: float) -> None:
        """调用方持锁；移动脉冲保留真实的抬键间隙及新的 keydown 边沿。"""
        if vk in self._movement_deadlines and now >= self._movement_deadlines[vk]:
            self.up(VK_BY_CODE[vk])
            return
        if vk in self._hold_repeat_at:
            if self._repeat_error is not None:
                self.up(VK_BY_CODE[vk])
                return
            if now >= self._hold_repeat_at[vk]:
                try:
                    self._repeat_hold(vk)
                except OSError:
                    self.up(VK_BY_CODE[vk])
                    raise
                if vk in self.held:
                    self._hold_repeat_at[vk] = now + 0.1
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

    def _repeat_hold(self, vk: int) -> None:
        """仅持续拾取补自动重复 keydown，不松键、不改变普通攻击/移动的重复行为。"""
        self._assert_bound_identity()
        if self.hybrid is not None:
            self._poll_hybrid_presses()
            press = self._hybrid_presses.get(vk)
            if press is None:
                return
            if self.hybrid.cancelled():
                self.up(VK_BY_CODE[vk])
                return
            if press.channel == "hardware":
                self.hybrid.skill_input(vk, False, repeat=True)
            else:
                self._hybrid_message(press, vk, False, True)
        elif self.delivery == "window_message":
            self._dispatch(vk, False, was_down=True)
        elif self.delivery == "foreground":
            if not window_is_foreground(self.root_hwnd or self.hwnd):
                self.up(VK_BY_CODE[vk])
                return
            self._send_input(vk, False)
        else:
            # 兼容后台原本就允许全局扫描码，并在失焦时补窗口消息。
            if not window_is_foreground(self.root_hwnd or self.hwnd):
                try:
                    self._post(vk, False, was_down=True)
                except OSError:
                    pass
            self._send_input(vk, False)

    def check_health(self) -> None:
        with self._lock:
            self._assert_bound_identity()
            if self.hybrid is not None:
                self.hybrid.check()
            if self._repeat_error is not None:
                raise OSError(f"后台持续按键投递失败：{self._repeat_error}")

    def movement_down(self, key: str, seconds: float | None = None) -> None:
        """只对纯窗口模式的位移启用脉冲；攻击/转向和旧扫描码路径不变。"""
        code = vk_for(key)
        self._assert_bound_identity()
        if self.hybrid is not None:
            self.hybrid.down(code, seconds)
            return
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

    def prepare_movement(self, *, allow_focus: bool = True) -> bool:
        self._assert_bound_identity()
        return self.hybrid is None or self.hybrid.begin(allow_focus=allow_focus)

    def movement_tap(self, key: str, seconds: float = 0.035) -> None:
        if self.hybrid is None:
            self.tap(key, seconds)
            return
        try:
            self.movement_down(key, seconds)
            time.sleep(max(0.01, min(0.5, seconds)))
        finally:
            self.up(key)
        self.check_health()

    def finish_movement(self) -> None:
        if self.hybrid is not None:
            self.hybrid.finish()

    def movement_heartbeat(self) -> None:
        if self.hybrid is not None:
            self.hybrid.heartbeat()

    def movement_events(self) -> list[tuple[str, str]]:
        if self.hybrid is None:
            return []
        with self.hybrid.lock:
            events, self.hybrid.events = self.hybrid.events, []
            return events

    def _release_hardware(self) -> None:
        for vk in list(self._hardware_down):
            try:
                self._send_input(vk, True)
            except OSError:
                if self._bound_identity is not None:
                    continue  # 留下失败记录，显式选窗会话不能带着旧键重绑定。
            self._hardware_down.discard(vk)

    def _post_targets(self) -> list[int]:
        targets: list[int] = []
        for hwnd in (self.root_hwnd, self.hwnd):
            hwnd = int(hwnd or 0)
            if hwnd and hwnd not in targets and user32.IsWindow(hwnd):
                targets.append(hwnd)
        return targets

    def _post(self, vk: int, key_up: bool, was_down: bool = False) -> None:
        if key_up and not self._binding_valid():
            return
        self._assert_bound_identity()
        targets = self._post_targets()
        if not targets:
            raise OSError("后台按键失败：游戏窗口句柄无效")
        lparam = key_lparam(vk, key_up, was_down=was_down)
        message = WM_KEYUP if key_up else WM_KEYDOWN
        posted_any = False
        for hwnd in targets:
            if key_up and not self._binding_valid():
                return
            self._assert_bound_identity()
            result = ctypes.c_size_t()
            if not key_up and not was_down:
                user32.SendMessageTimeoutW(hwnd, WM_ACTIVATE, WA_ACTIVE, 0, SMTO_ABORTIFHUNG, 30, ctypes.byref(result))
                self._assert_bound_identity()
                user32.SendMessageTimeoutW(hwnd, WM_SETFOCUS, 0, 0, SMTO_ABORTIFHUNG, 30, ctypes.byref(result))
            if key_up and not self._binding_valid():
                return
            self._assert_bound_identity()
            posted = bool(user32.PostMessageW(hwnd, message, vk, lparam))
            if key_up and not self._binding_valid():
                return
            self._assert_bound_identity()
            timed = bool(
                user32.SendMessageTimeoutW(
                    hwnd, message, vk, lparam, SMTO_ABORTIFHUNG, 40, ctypes.byref(result)
                )
            )
            if not key_up and vk not in EXTENDED_VKS and (0x30 <= vk <= 0x5A):
                self._assert_bound_identity()
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
        if not key_up:
            self._assert_bound_identity()
        sent = user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT))
        if sent != 1:
            raise OSError("键盘输入发送失败")

    def _hybrid_message(self, press: _HybridPress, vk: int, key_up: bool, was_down: bool) -> None:
        if key_up and not self._binding_valid():
            return
        self._assert_bound_identity()
        if (self.hybrid.desktop.pid(press.root) != press.pid
                or (not key_up and not self.hybrid.desktop.valid(press.root, press.pid))):
            if key_up:
                return  # 原进程已消失，不能向复用的 HWND 发消息。
            raise OSError("混合后台按键目标身份已变化")
        if (not press.hwnd or not user32.IsWindow(press.hwnd)
                or self.hybrid.desktop.pid(press.hwnd) != press.pid):
            if key_up:
                return
            raise OSError("混合后台按键窗口已失效")
        lparam = key_lparam(vk, key_up, was_down=was_down)
        if key_up and not self._binding_valid():
            return
        self._assert_bound_identity()
        if not user32.PostMessageW(press.hwnd, WM_KEYUP if key_up else WM_KEYDOWN, vk, lparam):
            raise OSError("混合后台窗口按键投递失败")

    def _dispatch_hybrid(self, vk: int, key_up: bool, was_down: bool) -> None:
        # 调用者持键盘锁；抬键必须沿按下时的通道，不能按新焦点重新路由。
        press = self._hybrid_presses.get(vk)
        if key_up:
            if press is not None:
                if press.channel == "hardware":
                    self.hybrid.skill_input(vk, True)
                else:
                    self._hybrid_message(press, vk, True, True)
                self._hybrid_presses.pop(vk, None)
            return
        self.check_health()
        if press is not None:
            if press.channel == "message":
                self._hybrid_message(press, vk, False, True)
            # 扫描码只保持，不反复全局 keydown；失焦由巡检和最终抬键清理。
            return
        guard = self.hybrid
        if not guard.desktop.valid(guard.hwnd, guard.pid):
            raise OSError("混合后台按键目标身份已变化")
        press = _HybridPress("hardware" if guard.desktop.foreground() == guard.hwnd else "message",
                             self.hwnd, guard.hwnd, guard.pid)
        self._hybrid_presses[vk] = press
        try:
            if press.channel == "hardware":
                guard.skill_input(vk, False)
            else:
                self._hybrid_message(press, vk, False, was_down)
            if press.channel != self.last_skill_channel:
                self.last_skill_channel = press.channel
                with guard.lock:
                    guard.events.append(("hybrid_skill_channel", press.channel))
            self._ensure_repeat_thread()
        except BaseException as exc:
            self._repeat_error = exc if isinstance(exc, OSError) else OSError(f"混合后台按键中断：{exc}")
            try:
                self._dispatch_hybrid(vk, True, True)
            except BaseException:
                pass  # 保留记录，暂停/退出时继续抬键，禁止换通道重发。
            raise

    def _poll_hybrid_presses(self) -> None:
        if self.hybrid is None:
            return
        guard = self.hybrid
        for vk, press in list(self._hybrid_presses.items()):
            if press.channel == "hardware" and (
                guard.desktop.foreground() != press.root
                or not guard.desktop.valid(press.root, press.pid)
                or vk not in guard.skill_held
            ):
                self._dispatch_hybrid(vk, True, True)
                self.held.discard(vk)
                self._movement_deadlines.pop(vk, None)
                self._hold_repeat_at.pop(vk, None)

    def _dispatch(self, vk: int, key_up: bool, *, was_down: bool = False) -> None:
        if not key_up:
            self._assert_bound_identity()
        if self.delivery == "hybrid":
            with self._lock:
                self._dispatch_hybrid(vk, key_up, was_down)
            return
        if self.delivery == "window_message":
            # 实验模式只向一个已绑定窗口排队，不重复 SendMessage、不发 WM_CHAR，
            # 也不伪造焦点事件或回退到全局扫描码。
            if key_up and not self._binding_valid():
                return
            if not self.hwnd or not user32.IsWindow(self.hwnd):
                raise OSError("独立后台按键失败：游戏窗口句柄无效")
            message = WM_KEYUP if key_up else WM_KEYDOWN
            lparam = key_lparam(vk, key_up, was_down=was_down)
            if key_up and not self._binding_valid():
                return
            self._assert_bound_identity()
            if not user32.PostMessageW(self.hwnd, message, vk, lparam):
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
            if self._bound_identity is not None:
                if key_up:
                    if vk in self._hardware_down:
                        self._send_input(vk, True)
                        self._hardware_down.discard(vk)
                else:
                    self._send_input(vk, False)
                    self._hardware_down.add(vk)
                return
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

    def hold(self, key: str, seconds: float = 0.8) -> None:
        """跨帧续期的长按，显式模拟自动重复；仅结束/中断/超时才抬键。"""
        code = vk_for(key)
        with self._lock:
            self._poll_hybrid_presses()
            self.down(key)
            now = time.monotonic()
            self._movement_deadlines[code] = now + max(0.1, min(0.8, seconds))
            self._hold_repeat_at.setdefault(code, now + 0.1)
            # 前台 down 时还没有 deadline，不会启动线程；必须在登记后启动。
            self._ensure_repeat_thread()

    def up(self, key: str) -> None:
        code = vk_for(key)
        if self.hybrid is not None:
            self.hybrid.up(code)
        with self._lock:
            if code not in self.held:
                return
            self._dispatch(code, True, was_down=True)
            self.held.discard(code)
            self._movement_pulses.pop(code, None)
            self._movement_deadlines.pop(code, None)
            self._hold_repeat_at.pop(code, None)

    def tap(self, key: str, seconds: float = 0.035) -> None:
        code = vk_for(key)
        if self.hybrid is not None:
            with self._lock:
                if code in self._hybrid_presses:
                    raise OSError("同一按键正在保持，不能叠加点按")
                self._dispatch(code, False)
                press = self._hybrid_presses[code]
            try:
                time.sleep(max(0.01, seconds))
            finally:
                with self._lock:
                    # 暂停/失焦已释放旧点按后，不能抬掉新会话的同名按键。
                    if self._hybrid_presses.get(code) is press:
                        try:
                            self._dispatch(code, True, was_down=True)
                        except OSError as exc:
                            self._repeat_error = exc
                            raise
            self.check_health()
            return
        if self._bound_identity is not None:
            with self._lock:
                identity = self._bound_identity
                if code in self.held:
                    raise OSError("同一按键正在保持，不能叠加点按")
                try:
                    self._dispatch(code, False)
                    self.held.add(code)
                except BaseException:
                    try:
                        self._dispatch(code, True, was_down=True)
                    except OSError:
                        pass
                    raise
            try:
                time.sleep(max(0.01, seconds))
            finally:
                with self._lock:
                    # 绑定时已释放旧键；旧点按结束不能再抬掉新会话同名键。
                    if self._bound_identity is identity:
                        self.up(key)
            return
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
        self.finish_movement()
        with self._lock:
            self._repeat_stop.set()
            held = list(self.held)
            unreleased: set[int] = set()
            for code in held:
                try:
                    self._dispatch(code, True, was_down=True)
                except OSError:
                    if self._bound_identity is not None:
                        unreleased.add(code)
            self.held = unreleased
            self._movement_pulses.clear()
            self._movement_deadlines.clear()
            self._hold_repeat_at.clear()
            for code in list(self._hybrid_presses):
                try:
                    self._dispatch(code, True, was_down=True)
                except OSError as exc:
                    self._repeat_error = exc
            if self.hybrid is None:
                self._repeat_error = None
            hardware_before_release = set(self._hardware_down)
            self._release_hardware()
            if self._bound_identity is not None:
                self.held.difference_update(hardware_before_release - self._hardware_down)
            pending_release = self._bound_identity is not None and bool(
                self.held or self._hardware_down or self._hybrid_presses
                or (self.hybrid is not None and (self.hybrid.held or self.hybrid.skill_held))
            )
            thread = self._repeat_thread
            self._repeat_thread = None
        if thread is not None and thread is not threading.current_thread() and thread.is_alive():
            thread.join(timeout=0.3)
        if pending_release:
            raise OSError("旧窗口按键尚未释放，不能切换窗口，请重试暂停")


def key_is_down(name: str) -> bool:
    return bool(user32.GetAsyncKeyState(vk_for(name)) & 0x8000)


def rising_edge(name: str, previous: dict[str, bool]) -> bool:
    current = key_is_down(name)
    old = previous.get(name, False)
    previous[name] = current
    return current and not old
