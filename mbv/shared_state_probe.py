"""线程键盘状态共享实验，仅供 movement_probe 显式启用；不接入正式输入层。

附加输入队列后，同步 SendMessage 的超时不再可靠，因此只使用 PostMessage。
每组在一次性子进程执行，由未附加的父进程负责停止和超时保护。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import multiprocessing
import time

from mbv.input import key_lparam, vk_for, window_is_foreground
from mbv.win32 import WM_KEYDOWN, WM_KEYUP, user32
from mbv.window import window_process_path


def configure_api() -> None:
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL
    for name in ("GetKeyboardState", "SetKeyboardState"):
        api = getattr(user32, name)
        api.argtypes = [ctypes.POINTER(ctypes.c_ubyte)]
        api.restype = wintypes.BOOL
    user32.GetKeyState.argtypes = [ctypes.c_int]
    user32.GetKeyState.restype = ctypes.c_short
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = ctypes.c_short
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                    wintypes.UINT, wintypes.UINT, wintypes.UINT]
    ctypes.windll.kernel32.GetCurrentThreadId.restype = wintypes.DWORD


def keyboard_state():
    state = (ctypes.c_ubyte * 256)()
    if not user32.GetKeyboardState(state):
        raise OSError("GetKeyboardState 失败")
    return state


def run_attached_trial(hwnd: int, pid: int, direction: str, messages: bool, guard,
                       duration: float = .3) -> dict:
    if direction not in {"left", "right"} or not .1 <= duration <= .4:
        raise ValueError("只允许 0.1–0.4 秒左右方向测试")
    configure_api()
    guard()
    if window_process_path(hwnd)[0] != pid:
        raise OSError("输入窗口所属进程变化")
    own_thread = int(ctypes.windll.kernel32.GetCurrentThreadId())
    target_pid = wintypes.DWORD()
    target_thread = int(user32.GetWindowThreadProcessId(hwnd, ctypes.byref(target_pid)))
    if not target_thread or target_pid.value != pid or target_thread == own_thread:
        raise OSError("输入线程身份异常")
    # 创建当前线程消息队列；不激活窗口、不伪造焦点、不发送全局按键。
    msg = wintypes.MSG()
    user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
    guard()
    if not user32.AttachThreadInput(own_thread, target_thread, True):
        raise OSError("AttachThreadInput 失败")
    code = vk_for(direction)
    downs = 0
    readback = False
    cleanup_errors = []
    try:
        guard()
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            guard()
            if window_process_path(hwnd)[0] != pid:
                raise OSError("输入窗口所属进程变化")
            state = keyboard_state()
            state[code] |= 0x80
            if not user32.SetKeyboardState(state):
                raise OSError("SetKeyboardState 按下失败")
            readback = bool(user32.GetKeyState(code) & 0x8000)
            if not readback:
                raise OSError("共享线程按下状态无法读回")
            if messages and not user32.PostMessageW(
                    hwnd, WM_KEYDOWN, code, key_lparam(code, False, was_down=downs > 0)):
                raise OSError("共享状态按键消息投递失败")
            downs += 1
            time.sleep(.02)
    finally:
        # 只清理本组方向键，不回放整个旧键盘状态，避免覆盖用户新按下的键。
        # 即使读取/清理失败，也必须继续尝试 keyup 与解除附加。
        try:
            state = keyboard_state()
            state[code] &= 0x7f
            if not user32.SetKeyboardState(state) or user32.GetKeyState(code) & 0x8000:
                cleanup_errors.append("共享方向键未确认释放")
        except OSError as exc:
            cleanup_errors.append(str(exc))
        finally:
            try:
                if messages and window_process_path(hwnd)[0] == pid:
                    if not user32.PostMessageW(hwnd, WM_KEYUP, code, key_lparam(code, True)):
                        cleanup_errors.append("抬键消息投递失败")
            finally:
                if not user32.AttachThreadInput(own_thread, target_thread, False):
                    cleanup_errors.append("解除线程附加失败")
        if cleanup_errors:
            raise OSError("；".join(cleanup_errors))
    return {"updates": downs, "pressed_state_readback": readback,
            "released": True, "detached": True, "target_thread": target_thread,
            "messages": messages}


def _worker(connection, hwnd: int, pid: int, direction: str, messages: bool) -> None:
    try:
        configure_api()
        connection.send({"ready": True})
        if not connection.poll(5) or connection.recv() != "go":
            return
        initial_foreground = int(user32.GetForegroundWindow() or 0)

        def guard():
            if connection.poll():
                raise RuntimeError("父进程取消试验")
            if (not user32.IsWindow(hwnd) or user32.IsIconic(hwnd)
                    or window_process_path(hwnd)[0] != pid):
                raise RuntimeError("游戏窗口变化")
            if window_is_foreground(hwnd) or int(user32.GetForegroundWindow() or 0) != initial_foreground:
                raise RuntimeError("前台窗口变化")
            if any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in range(1, 256)):
                raise RuntimeError("检测到真实键鼠输入")

        result = run_attached_trial(hwnd, pid, direction, messages, guard)
        connection.send({"result": result})
    except BaseException as exc:
        connection.send({"error": str(exc)})
    finally:
        connection.close()


def isolated_trial(hwnd: int, pid: int, direction: str, messages: bool, guard) -> dict:
    """父进程不附加输入队列；取消后先给子进程机会执行 finally。"""
    guard()
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_worker, args=(child, hwnd, pid, direction, messages), daemon=True)
    process.start()
    child.close()
    try:
        deadline = time.monotonic() + 5
        running = False
        while time.monotonic() < deadline:
            guard()
            if parent.poll(.02):
                response = parent.recv()
                if "error" in response:
                    raise RuntimeError(response["error"])
                if response.get("ready") and not running:
                    guard()
                    parent.send("go")
                    running = True
                    deadline = time.monotonic() + 1.5
                elif "result" in response:
                    return response["result"]
            if not process.is_alive():
                raise RuntimeError("共享状态子进程异常退出")
        raise RuntimeError("共享状态试验超时；停止后需人工检查按键状态")
    finally:
        if process.is_alive():
            try:
                parent.send("cancel")
            except (BrokenPipeError, OSError):
                pass
            process.join(.6)
        if process.is_alive():
            process.terminate()
            process.join(.5)
            # 仅作故障清理；不宣称该消息能替代共享状态清理。
            if window_process_path(hwnd)[0] == pid:
                code = vk_for(direction)
                user32.PostMessageW(hwnd, WM_KEYUP, code, key_lparam(code, True))
        parent.close()
        if not process.is_alive():
            process.close()
