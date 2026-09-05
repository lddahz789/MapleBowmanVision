"""显式授权的前台短步/恢复窗口探针；不改挂机配置，不自动循环。

python -m mbv.foreground_move_probe 默认只读。
--enable-input --confirm-bot-stopped 才允许一次向右 0.2 秒试验。
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime
import json
import multiprocessing
import os
import time

from mbv.background_capture import BackgroundCapture
from mbv.config import load_config
from mbv.input import Keyboard, vk_for
from mbv.paths import LOG_DIR, profile_paths
from mbv.vision import crop, player_marker_observation
from mbv.win32 import process_integrity_level, user32
from mbv.window import WindowInfo, find_game_window, focus_game_window, window_process_path


def _focus_worker(connection, hwnd: int, pid: int, expected_foreground: int) -> None:
    try:
        # 在隔离子进程内运行既有窗口激活函数，防止附加队列时窗口卡住。
        if (window_process_path(hwnd)[0] != pid or not user32.IsWindow(hwnd)
                or user32.IsIconic(hwnd)):
            raise RuntimeError("切换目标已关闭、最小化或被替换")
        if int(user32.GetForegroundWindow() or 0) != expected_foreground:
            raise RuntimeError("用户已切换窗口，取消激活")
        if any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in range(1, 256)):
            raise RuntimeError("用户正在操作键鼠，取消激活")
        focus_game_window(WindowInfo(hwnd, "", 0, 0, 0, 0), settle_seconds=.05)
        connection.send({"ok": True})
    except BaseException as exc:
        connection.send({"error": str(exc)})
    finally:
        connection.close()


def bounded_focus(hwnd: int, pid: int, expected_foreground: int) -> None:
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_focus_worker,
                              args=(child, hwnd, pid, expected_foreground), daemon=True)
    process.start()
    child.close()
    try:
        if not parent.poll(3):
            raise RuntimeError("窗口激活超时，未授权发键")
        response = parent.recv()
        if "error" in response:
            raise RuntimeError(response["error"])
        if int(user32.GetForegroundWindow() or 0) != hwnd or window_process_path(hwnd)[0] != pid:
            raise RuntimeError("窗口激活未通过前台身份复核")
    finally:
        process.join(.1)
        if process.is_alive():
            process.terminate()
            process.join(.5)
        parent.close()
        if not process.is_alive():
            process.close()


def short_move(keyboard: Keyboard, guard, seconds: float = .2) -> None:
    if not .03 <= seconds <= .2:
        raise ValueError("前台探针仅允许 0.03–0.2 秒右移")
    guard(False)
    try:
        # 独立限时抬键不依赖截图或主循环；持键阶段不做截图。
        keyboard.movement_down("right", seconds=seconds)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            guard(True)
            keyboard.check_health()
            time.sleep(.01)
    finally:
        keyboard.release_all()


def restore_original(original: int, original_pid: int, game: int, emit) -> bool:
    current = int(user32.GetForegroundWindow() or 0)
    if current == original and window_process_path(original)[0] == original_pid:
        emit("restore_original", result="already_original")
        return True
    if current != game:
        emit("restore_original", result="skipped_user_changed_window")
        return False
    if not user32.IsWindow(original) or window_process_path(original)[0] != original_pid:
        emit("restore_original", result="skipped_original_closed_or_replaced")
        return False
    bounded_focus(original, original_pid, game)
    emit("restore_original", result="verified")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enable-input", action="store_true")
    parser.add_argument("--confirm-bot-stopped", action="store_true")
    args = parser.parse_args()
    config = load_config(profile_paths("newmaple").config)
    window = find_game_window(config)
    pid, _ = window_process_path(window.hwnd)
    original = int(user32.GetForegroundWindow() or 0)
    original_pid, _ = window_process_path(original)
    print(json.dumps({"game": window.hwnd, "pid": pid, "original": original,
                      "original_pid": original_pid, "enable_input": args.enable_input}), flush=True)
    if not args.enable_input:
        return 0
    if not args.confirm_bot_stopped:
        parser.error("必须确认原挂机及其它发键器已停止")
    if not original or original == window.hwnd or not original_pid:
        raise RuntimeError("请先点浏览器或资源管理器，游戏须在后台")
    own_level, game_level = process_integrity_level(os.getpid()), process_integrity_level(pid)
    if own_level < 0 or game_level < 0 or own_level < game_level:
        raise RuntimeError("无法确认权限足够")
    hotkey_id = 0x4D52
    if not user32.RegisterHotKey(None, hotkey_id, 0, vk_for("f9")):
        raise RuntimeError("F9 已占用，请完全退出原挂机助手")
    user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                    wintypes.UINT, wintypes.UINT, wintypes.UINT]
    keyboard = Keyboard("foreground")
    keyboard.bind_window(window.hwnd)
    capture = BackgroundCapture()
    events = []
    attempted_focus = False
    exit_code = 0
    started = time.monotonic()

    def emit(event, **data):
        row = {"ts": time.time(), "event": event, **data}
        events.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)

    def guard(holding=False, *, expected=window.hwnd):
        msg = wintypes.MSG()
        if user32.PeekMessageW(ctypes.byref(msg), None, 0x0312, 0x0312, 1):
            raise RuntimeError("F9 停止")
        if time.monotonic() - started > 20:
            raise RuntimeError("探针达到总时限")
        if (not user32.IsWindow(window.hwnd) or user32.IsIconic(window.hwnd)
                or window_process_path(window.hwnd)[0] != pid):
            raise RuntimeError("游戏窗口变化")
        if int(user32.GetForegroundWindow() or 0) != expected:
            raise RuntimeError("前台窗口变化，不再发方向键")
        # SendInput 会改变异步键状态，持键阶段只能排除本探针发出的右键。
        if any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in range(1, 256)
               if not (holding and vk == vk_for("right"))):
            raise RuntimeError("检测到手动键鼠输入")

    def observe(expected):
        guard(expected=expected)
        frame = capture.capture(window)
        minimap, _ = crop(frame, config["regions"]["minimap"])
        vision = config["vision"]
        obs, _ = player_marker_observation(minimap, vision["player_hsv_ranges"],
                                           int(vision["player_blob_min_area"]),
                                           int(vision["player_blob_max_area"]), None)
        guard(expected=expected)
        if not obs.unambiguous:
            raise RuntimeError("小地图玩家标记丢失或不唯一")
        return [obs.point[0] * minimap.shape[1], obs.point[1] * minimap.shape[0]]

    try:
        emit("probe_start", game=window.hwnd, pid=pid, original=original,
             own_integrity=own_level, game_integrity=game_level)
        before = observe(original)
        time.sleep(.15)
        stable = observe(original)
        if max(abs(a - b) for a, b in zip(before, stable)) >= .5:
            raise RuntimeError("角色不静止，无法归因")
        guard(expected=original)
        attempted_focus = True
        bounded_focus(window.hwnd, pid, original)
        guard()
        focused = observe(window.hwnd)
        if max(abs(a - b) for a, b in zip(before, focused)) >= .5:
            raise RuntimeError("切前台后位置变化，可能有缓存帧或外部移动，不发键")
        emit("focus_verified", before=focused)
        short_move(keyboard, guard)
        time.sleep(.15)
        after = observe(window.hwnd)
        dx, dy = after[0] - focused[0], after[1] - focused[1]
        emit("move_result", direction="right", seconds=.2, before=focused, after=after,
             dx_pixels=dx, dy_pixels=dy, result="right_displacement_observed" if dx >= .5 else "not_verified")
    except (OSError, RuntimeError, EOFError) as exc:
        emit("probe_aborted", reason=str(exc))
        exit_code = 1
    finally:
        keyboard.release_all()
        if attempted_focus:
            try:
                if not restore_original(original, original_pid, window.hwnd, emit):
                    exit_code = 1
            except (OSError, RuntimeError, EOFError) as exc:
                emit("restore_failed", reason=str(exc))
                exit_code = 1
        capture.close()
        user32.UnregisterHotKey(None, hotkey_id)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        report = LOG_DIR / ("foreground-move-probe-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".json")
        report.write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")
        print(str(report), flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
