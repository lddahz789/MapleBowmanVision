"""混合后台的限时前台移动租约；结束后留在游戏前台，技能不经过本模块。"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import multiprocessing
import threading
import time
from collections.abc import Callable

from mbv.win32 import user32


def window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


class _LastInput(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


class Desktop:
    def foreground(self) -> int:
        return int(user32.GetForegroundWindow() or 0)

    def valid(self, hwnd: int, pid: int) -> bool:
        return bool(hwnd and pid and user32.IsWindow(hwnd)
                    and not user32.IsIconic(hwnd) and window_pid(hwnd) == pid)

    def pid(self, hwnd: int) -> int:
        return window_pid(hwnd)

    def cursor(self) -> tuple[int, int]:
        point = wintypes.POINT()
        if not user32.GetCursorPos(ctypes.byref(point)):
            raise OSError("无法检查鼠标位置，取消临时前台移动")
        return point.x, point.y

    def keys_down(self, excluding: set[int]) -> bool:
        # 扫描码保持会反映到异步键状态中，排除本租约正在注入的键。
        return any(user32.GetAsyncKeyState(key) & 0x8000
                   for key in range(1, 256) if key not in excluding)

    def idle(self) -> bool:
        info = _LastInput(ctypes.sizeof(_LastInput), 0)
        if not user32.GetLastInputInfo(ctypes.byref(info)):
            return False
        elapsed = (int(ctypes.windll.kernel32.GetTickCount()) - info.dwTime) & 0xFFFFFFFF
        return elapsed >= 700 and not self.keys_down(set())

    def focus(self, hwnd: int, pid: int, expected: int, cancelled: Callable[[], bool]) -> None:
        # AttachThreadInput/激活可能受目标窗口阻塞，必须放可终止子进程。
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=False)
        process = context.Process(target=_focus_worker, args=(child, hwnd, pid, expected), daemon=True)
        process.start()
        child.close()
        deadline = time.monotonic() + 2.5
        try:
            while not parent.poll(0.02):
                if cancelled():
                    raise OSError("用户操作或切窗，取消焦点切换")
                if time.monotonic() > deadline or not process.is_alive():
                    raise OSError("焦点切换超时或辅助进程退出")
            error = parent.recv()
            if error:
                raise OSError(error)
            if cancelled() or not self.valid(hwnd, pid) or self.foreground() != hwnd:
                raise OSError("焦点切换未通过身份复核")
        finally:
            if process.is_alive():
                process.terminate()
            process.join(0.3)
            parent.close()
            if not process.is_alive():
                process.close()


def _focus_worker(connection, hwnd: int, pid: int, expected: int) -> None:
    from mbv.window import WindowInfo, focus_game_window

    try:
        desktop = Desktop()
        if not desktop.valid(hwnd, pid) or desktop.foreground() != expected or desktop.keys_down(set()):
            raise OSError("窗口或用户输入状态变化，取消激活")
        focus_game_window(WindowInfo(hwnd, "", 0, 0, 0, 0), settle_seconds=0.03)
        connection.send(None)
    except BaseException as exc:
        connection.send(str(exc))
    finally:
        connection.close()


class HybridMovement:
    """一段移动共用一个焦点租约；独立 watchdog 不依赖视觉帧抬键。

    手动输入检查是尽力而为，不能保证完全无打断，也不能分辨用户同时按下
    与本模块相同的方向键。不会阻断/吞掉用户输入。
    """
    MAX_LEASE_SECONDS = 4.0
    HEARTBEAT_SECONDS = 0.8
    MAX_KEY_SECONDS = 0.5

    def __init__(self, send: Callable[[int, bool], None], *, desktop=None,
                 clock=time.monotonic, start_watchdog: bool = True) -> None:
        self.send = send
        self.desktop = desktop or Desktop()
        self.clock = clock
        self.start_watchdog = start_watchdog
        self.lock = threading.RLock()
        self.hwnd = self.pid = self.original = self.original_pid = 0
        self.active = False
        self.user_intervened = False
        self.held: dict[int, float] = {}
        self.error: str | None = None
        self.started = self.heartbeat_at = 0.0
        self.resume_after = 0.0
        self.cursor_at_start = (0, 0)
        self.stop = threading.Event()
        self.events: list[tuple[str, str]] = []
        self.cancelled: Callable[[], bool] = lambda: False

    def bind(self, hwnd: int) -> None:
        self.finish()
        with self.lock:
            if self.held:
                raise OSError(self.error or "混合后台移动键尚未释放，不能重新启动")
            self.hwnd, self.pid = hwnd, self.desktop.pid(hwnd)
            self.error = None
            self.resume_after = 0.0

    def check(self) -> None:
        with self.lock:
            if self.error:
                raise OSError(self.error)

    def _intervened(self, allowed: set[int]) -> bool:
        excluded = set(self.held)
        for generic, sides in ((0x10, (0xA0, 0xA1)), (0x11, (0xA2, 0xA3)), (0x12, (0xA4, 0xA5))):
            if generic in excluded:
                excluded.update(sides)
        return (self.desktop.foreground() not in allowed
                or self.desktop.keys_down(excluded)
                or self.desktop.cursor() != self.cursor_at_start)

    def begin(self) -> bool:
        with self.lock:
            self.check()
            if self.cancelled():
                self.finish()
                return False
            if self.active:
                self.poll()
                self.check()
                self.heartbeat_at = self.clock()
                return True
            if self.clock() < self.resume_after:
                return False
            if not self.desktop.valid(self.hwnd, self.pid):
                raise OSError("混合后台：游戏关闭、最小化或窗口身份变化")
            if not self.desktop.idle():
                return False
            self.original = self.desktop.foreground()
            self.original_pid = self.desktop.pid(self.original)
            if not self.desktop.valid(self.original, self.original_pid):
                return False
            self.cursor_at_start = self.desktop.cursor()
            self.user_intervened = False
            self.active = True
            self.started = self.heartbeat_at = self.clock()
            try:
                if self.original != self.hwnd:
                    self.desktop.focus(self.hwnd, self.pid, self.original,
                                       lambda: self.cancelled() or self._intervened({self.original, self.hwnd}))
                if self.cancelled() or self._intervened({self.hwnd}):
                    self.user_intervened = True
                    raise OSError("用户操作或切窗，取消前台移动")
            except (OSError, EOFError) as exc:
                self.error = f"混合后台激活失败：{exc}"
                self.finish()
                raise OSError(self.error) from exc
            self.started = self.heartbeat_at = self.clock()
            self.events.append(("hybrid_focus_acquired", str(self.original)))
            self.stop = threading.Event()
            if self.start_watchdog:
                threading.Thread(target=self._watch, args=(self.stop,), daemon=True,
                                 name="MapleHybridGuard").start()
            return True

    def yield_if_due(self) -> bool:
        """长路线主动分段；保留 4 秒 watchdog，抬键等待但不恢复原窗口。"""
        with self.lock:
            self.poll()
            self.check()
            if self.active and self.clock() - self.started >= 2.5:
                self.finish()
                self.check()
                self.resume_after = self.clock() + 0.7
                return True
            return False

    def heartbeat(self) -> None:
        with self.lock:
            self.poll()
            self.check()
            self.heartbeat_at = self.clock()

    def down(self, vk: int, seconds: float | None = None) -> None:
        with self.lock:
            self.check()
            if not self.active:
                raise OSError("混合后台移动未取得前台授权")
            self.poll()
            self.check()
            # 每次真正 keydown 前检查焦点与身份；焦点切换与 SendInput 仍非原子操作。
            if self.desktop.foreground() != self.hwnd or not self.desktop.valid(self.hwnd, self.pid):
                raise OSError("混合后台移动前焦点变化")
            if vk not in self.held:
                self.held[vk] = self.clock() + self.MAX_KEY_SECONDS
                try:
                    self.send(vk, False)
                except BaseException:
                    self.up(vk)
                    raise
            duration = self.MAX_KEY_SECONDS if seconds is None else max(0.03, min(self.MAX_KEY_SECONDS, seconds))
            self.held[vk] = self.clock() + duration
            self.heartbeat_at = self.clock()

    def up(self, vk: int) -> None:
        with self.lock:
            if vk in self.held:
                # 必须先释放本模块注入的状态，再允许恢复其它窗口。
                self.send(vk, True)
                self.held.pop(vk, None)

    def poll(self) -> None:
        with self.lock:
            if not self.active:
                return
            now = self.clock()
            reason = None
            if self.cancelled():
                reason = "收到暂停或退出信号"
            elif not self.desktop.valid(self.hwnd, self.pid):
                self.user_intervened = True
                reason = "游戏窗口身份变化"
            elif self._intervened({self.hwnd}):
                self.user_intervened = True
                reason = "检测到用户操作或焦点变化"
            elif now - self.started > self.MAX_LEASE_SECONDS:
                reason = "临时前台移动超过 4 秒"
            elif now - self.heartbeat_at > self.HEARTBEAT_SECONDS:
                reason = "视觉或行动心跳中断"
            if reason:
                self.error = "混合后台停止：" + reason
                self.finish()
                return
            for vk, deadline in list(self.held.items()):
                if now >= deadline:
                    self.up(vk)

    def _watch(self, stop: threading.Event) -> None:
        while not stop.wait(0.02):
            with self.lock:
                if stop.is_set():
                    return
                try:
                    self.poll()
                except OSError as exc:
                    self.error = f"混合后台保护失败：{exc}"
                    self.finish()
                    return

    def finish(self) -> None:
        with self.lock:
            self.stop.set()
            if self.active:
                try:
                    self.user_intervened |= self._intervened({self.hwnd})
                except OSError as exc:
                    self.user_intervened = True
                    self.error = f"混合后台无法复核用户操作：{exc}"
            released = True
            for vk in list(self.held):
                try:
                    self.up(vk)
                except OSError as exc:
                    released = False
                    self.error = f"混合后台抬键失败，请手动释放移动键：{exc}"
            if not self.active:
                return
            self.active = False
            if not released:
                return
            # 用户明确允许移动结束后留在游戏前台。结束、暂停及异常都不再
            # 激活旧窗口，也不在用户切换窗口后抢回焦点；只释放本租约的键。
            result = "skipped_user_intervened" if self.user_intervened else "game_kept_foreground"
            self.events.append(("hybrid_movement_released", result))
