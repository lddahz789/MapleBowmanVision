"""独立、限时的纯窗口移动兼容探针；默认只读，不修改挂机配置。"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from dataclasses import asdict, dataclass
from datetime import datetime
import json
import os
import time

from mbv.background_capture import BackgroundCapture
from mbv.config import load_config
from mbv.input import child_windows, key_lparam, resolve_input_hwnd, vk_for, window_class_name, window_is_foreground
from mbv.paths import LOG_DIR, profile_paths
from mbv.vision import crop, player_marker_observation
from mbv.win32 import SMTO_ABORTIFHUNG, WM_KEYDOWN, WM_KEYUP, process_integrity_level, user32
from mbv.window import find_game_window, window_process_path


@dataclass(frozen=True)
class Variant:
    transport: str
    parameter: str
    fresh_repeat: bool = False


VARIANTS = tuple(Variant(transport, parameter, fresh)
                 for transport in ("post", "sync")
                 for parameter, fresh in (("standard", False), ("nonextended", False),
                                           ("standard", True), ("zero", False)))


def message_param(vk: int, up: bool, repeated: bool, variant: Variant) -> int:
    if variant.parameter == "zero":
        return 0  # 明确标记的旧式兼容试验，不是标准键盘消息。
    value = key_lparam(vk, up, was_down=repeated and not variant.fresh_repeat)
    return value & ~(1 << 24) if variant.parameter == "nonextended" else value


def send_message(hwnd: int, vk: int, up: bool, repeated: bool, variant: Variant,
                 expected_pid: int | None = None) -> None:
    if expected_pid is not None and window_process_path(hwnd)[0] != expected_pid:
        raise OSError("目标窗口所属进程已变化，拒绝投递")
    message = WM_KEYUP if up else WM_KEYDOWN
    parameter = message_param(vk, up, repeated, variant)
    if variant.transport == "post":
        success = user32.PostMessageW(hwnd, message, vk, parameter)
    elif variant.transport == "sync":
        result = ctypes.c_size_t()
        success = user32.SendMessageTimeoutW(hwnd, message, vk, parameter,
                                             SMTO_ABORTIFHUNG, 40, ctypes.byref(result))
    else:
        raise ValueError("未知消息投递方式")
    if not success:
        raise OSError("窗口消息调用失败或超时；不能判断游戏是否处理")


def send_trial(hwnd: int, direction: str, variant: Variant, guard, duration: float = 0.3,
               expected_pid: int | None = None) -> int:
    """无截图阻塞的短消息序列；异常/停止也会尽力发出最终抬键。"""
    if direction not in {"left", "right"} or not 0.1 <= duration <= 0.4:
        raise ValueError("只允许 0.1–0.4 秒左右方向测试")
    code = vk_for(direction)
    sent = 0
    guard()
    try:
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            guard()
            send_message(hwnd, code, False, sent > 0, variant, expected_pid)
            sent += 1
            time.sleep(0.05)
    finally:
        try:
            send_message(hwnd, code, True, True, variant, expected_pid)
        except OSError:
            # 超时可能已送达，补一次同窗口标准抬键，但绝不补全局输入。
            if expected_pid is None or window_process_path(hwnd)[0] == expected_pid:
                user32.PostMessageW(hwnd, WM_KEYUP, code, key_lparam(code, True))
            raise
    return sent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enable-input", action="store_true", help="明确启用真实左右键试验")
    parser.add_argument("--confirm-bot-stopped", action="store_true", help="确认原挂机及其它发键器已停止")
    parser.add_argument("--shared-key-state", action="store_true",
                        help="测试线程键盘状态共享（仅状态/状态加异步消息），不修改日常配置")
    args = parser.parse_args()
    config = load_config(profile_paths("newmaple").config)
    window = find_game_window(config)
    pid, path = window_process_path(window.hwnd)
    targets = list(dict.fromkeys([resolve_input_hwnd(window.hwnd), window.hwnd, *child_windows(window.hwnd)]))
    targets = [h for h in targets if window_process_path(h)[0] == pid]
    metadata = {"root": window.hwnd, "pid": pid, "path": path,
                "shared_key_state": args.shared_key_state,
                "foreground": window_is_foreground(window.hwnd),
                "targets": [{"hwnd": h, "class": window_class_name(h)} for h in targets]}
    print(json.dumps(metadata, ensure_ascii=False), flush=True)
    if not args.enable_input:
        return 0
    if not args.confirm_bot_stopped:
        parser.error("必须确认原挂机已停止")
    own_level, game_level = process_integrity_level(os.getpid()), process_integrity_level(pid)
    if own_level < 0 or game_level < 0 or own_level < game_level:
        raise RuntimeError("无法确认权限足够，停止测试")
    if not targets or len(targets) > 4:
        raise RuntimeError("输入窗口数量异常，需人工筛选")

    hotkey_id = 0x4D51
    # 保留 F9 既用于停止，也避免与仍在运行的标准 MBV 热键监听器并发。
    if not user32.RegisterHotKey(None, hotkey_id, 0, vk_for("f9")):
        raise RuntimeError("F9 已被占用，请先退出正在运行的挂机会话")
    user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                    wintypes.UINT, wintypes.UINT, wintypes.UINT]
    capture = BackgroundCapture()
    events = []
    started = time.monotonic()
    initial_foreground = int(user32.GetForegroundWindow() or 0)

    def emit(event, **data):
        row = {"ts": time.time(), "event": event, **data}
        events.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)

    def guard():
        msg = wintypes.MSG()
        if user32.PeekMessageW(ctypes.byref(msg), None, 0x0312, 0x0312, 1):
            raise RuntimeError("收到停止快捷键")
        if time.monotonic() - started > 55:
            raise RuntimeError("探针达到总时限")
        if (not user32.IsWindow(window.hwnd) or user32.IsIconic(window.hwnd)
                or window_process_path(window.hwnd)[0] != pid):
            raise RuntimeError("游戏窗口已变化")
        if window_is_foreground(window.hwnd):
            raise RuntimeError("游戏回到前台，终止后台对照测试")
        if args.shared_key_state:
            if int(user32.GetForegroundWindow() or 0) != initial_foreground:
                raise RuntimeError("前台窗口变化，停止共享状态试验")
            if any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in range(1, 256)):
                raise RuntimeError("检测到真实键鼠输入，停止共享状态试验")
        if any(user32.GetAsyncKeyState(vk_for(k)) & 0x8000 for k in ("left", "right", "up", "down", "f8", "f9")):
            raise RuntimeError("检测到手动按键，停止测试")

    def observe():
        guard()
        frame = capture.capture(window)
        minimap, _ = crop(frame, config["regions"]["minimap"])
        vision = config["vision"]
        obs, _ = player_marker_observation(minimap, vision["player_hsv_ranges"],
                                            int(vision["player_blob_min_area"]),
                                            int(vision["player_blob_max_area"]), None)
        guard()
        if not obs.unambiguous:
            raise RuntimeError("小地图玩家标记不唯一或丢失")
        return (obs.point[0] * minimap.shape[1], obs.point[1] * minimap.shape[0])

    try:
        emit("probe_start", **metadata, own_integrity=own_level, game_integrity=game_level)
        initial = observe()
        for hwnd in targets:
            variants = (Variant("state_only", "standard"), Variant("state_post", "standard")) if args.shared_key_state else VARIANTS
            for variant in variants:
                for direction in ("left", "right"):
                    before = observe()
                    time.sleep(0.15)
                    stable = observe()
                    if max(abs(a - b) for a, b in zip(before, stable)) >= 0.5:
                        raise RuntimeError("测试前角色已在移动，无法归因")
                    if max(abs(a - b) for a, b in zip(initial, before)) > 3:
                        raise RuntimeError("角色超出初始位置安全范围")
                    # 目标身份每组复核，禁止将消息发向其它应用。
                    if window_process_path(hwnd)[0] != pid:
                        raise RuntimeError("输入目标身份变化")
                    state_result = {}
                    if args.shared_key_state:
                        from mbv.shared_state_probe import isolated_trial
                        state_result = isolated_trial(hwnd, pid, direction,
                                                      variant.transport == "state_post", guard)
                        sent = state_result["updates"] if state_result["messages"] else 0
                    else:
                        sent = send_trial(hwnd, direction, variant, guard, expected_pid=pid)
                    time.sleep(0.25)
                    after = observe()
                    delta = after[0] - before[0]
                    emit("trial", hwnd=hwnd, direction=direction, **asdict(variant),
                         before=before, after=after, dx_pixels=round(delta, 4),
                         dy_pixels=round(after[1] - before[1], 4), keydowns=sent,
                         shared_state_result=state_result,
                         foreground=False, result="displacement_observed" if abs(delta) >= 0.5 else "no_displacement")
                    if abs(delta) >= 0.5 or abs(after[1] - before[1]) >= 0.5:
                        emit("probe_stop", reason="发现位移，停止自动遍历，需人工复核方向与归因")
                        return 0
        emit("probe_complete", result="所有测试组均未观察到位移")
        return 0
    except (OSError, RuntimeError) as exc:
        emit("probe_aborted", reason=str(exc))
        return 1
    finally:
        capture.close()
        user32.UnregisterHotKey(None, hotkey_id)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        report = LOG_DIR / ("movement-probe-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".json")
        report.write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")
        print(str(report), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
