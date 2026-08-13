from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path
import threading
import time
from typing import Any

import mss

from mbv.config import SessionLog, load_config
from mbv.input import Keyboard, input_delivery, key_is_down, rising_edge
from mbv.overlay import RuntimeOverlay
from mbv.paths import PLAYER_ASSET_DIR, PLAYER_HEAD_ASSET_DIR, PLAYER_TITLE_ASSET_DIR
from mbv.vision import (
    _ordered_rect,
    attack_box_from_config,
    attack_rect_from_player,
    bar_fill,
    choose_fused_player_anchor,
    choose_nearest_same_level_target,
    choose_nearest_target,
    crop,
    find_detections,
    load_templates,
    player_anchor_center,
    player_marker,
)
from mbv.win32 import (
    HOTKEY_ID_EXIT,
    HOTKEY_ID_F7,
    HOTKEY_ID_F8,
    HOTKEY_ID_F9,
    MOD_CONTROL,
    MOD_NOREPEAT,
    MOD_SHIFT,
    WM_HOTKEY,
    WM_QUIT,
    process_integrity_level,
    user32,
)
from mbv.window import capture_client, client_window, find_game_window, focus_game_window, window_process_path

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


class BowmanBot:
    def __init__(self, config: dict[str, Any], input_authorized: bool) -> None:
        self.config = config
        self.input_authorized = input_authorized
        self.delivery = input_delivery(config)
        self.background_input = self.delivery == "background"
        self.keyboard = Keyboard(self.delivery)
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
        self.f7_requested = threading.Event()
        self.f8_requested = threading.Event()
        self.f9_requested = threading.Event()
        self.calibration_overlay_visible = True
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
        self.vision_suspended = threading.Event()
        self.window: WindowInfo | None = None
        self.ui_hp = 0.0
        self.ui_mp = 0.0
        self.config_lock = threading.Lock()

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
                ("f7", HOTKEY_ID_F7, 0),
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
                if int(msg.wParam) == HOTKEY_ID_F7:
                    self.log.write("hotkey_pressed", key="F7")
                    self.f7_requested.set()
                elif int(msg.wParam) == HOTKEY_ID_F8:
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
                if rising_edge("f7", self.hotkey_state):
                    self.f7_requested.set()
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
            self.notify("当前是观察模式，不会发送按键。请在控制面板点击「启动挂机」。", 6.0)
            return
        if not self.config.get("calibrated"):
            self.notify("尚未完成校准，请在控制面板点击「画面校准」。")
            return
        if not self.player_templates:
            self.notify("尚未采集玩家姓名板模板，请在控制面板点击「采集姓名板」。", 8.0)
            return
        if not self.integrity_ok:
            self.notify("启动失败：助手权限低于游戏。请在控制面板点击「启动挂机」，并在 UAC 中选择“是”。", 8.0)
            return
        if not user32.IsWindow(window.hwnd):
            self.notify("启动失败：游戏窗口已失效。", 6.0)
            return
        if user32.IsIconic(window.hwnd):
            self.notify("启动失败：游戏窗口已最小化。后台截图需要窗口保持可见。", 6.0)
            return
        self.keyboard.bind_window(window.hwnd)
        if int(user32.GetForegroundWindow()) != window.hwnd:
            try:
                focus_game_window(window, settle_seconds=0.15)
            except Exception as exc:
                if not self.background_input:
                    self.notify(f"启动失败：无法切换到游戏窗口（{exc}）", 6.0)
                    return
        if not self.background_input and int(user32.GetForegroundWindow()) != window.hwnd:
            self.notify("启动失败：游戏没有进入前台，请点击游戏后重试。", 6.0)
            return
        self.armed = True
        self.state = "SCANNING"
        self.started_at = time.monotonic()
        self.log.write(
            "arm",
            window=window.title,
            templates=len(self.templates),
            delivery=self.delivery,
            input_hwnd=self.keyboard.hwnd,
        )
        if self.background_input:
            self.notify("已经启动（后台按键）。始终发送扫描码；控制面板不抢焦点。", 5.0)
        else:
            self.notify("已经启动。按 F8 暂停，F7 显隐校对信息，按 F9 或 Ctrl+Shift+Q 立即退出。", 3.0)
        user32.MessageBeep(0x00000040)

    def request_toggle(self) -> None:
        self.f8_requested.set()

    def toggle_calibration_overlay(self) -> None:
        self.calibration_overlay_visible = not self.calibration_overlay_visible
        self.log.write("calibration_overlay", visible=self.calibration_overlay_visible)

    def suspend_vision(self) -> None:
        if self.armed:
            self.disarm("打开采集工具")
        self.vision_suspended.set()
        self.keyboard.release_all()

    def resume_vision(self) -> None:
        self.vision_suspended.clear()

    def reload_templates(self) -> None:
        self.templates = load_templates()
        self.player_templates = load_templates(PLAYER_ASSET_DIR)
        self.player_head_templates = load_templates(PLAYER_HEAD_ASSET_DIR)
        self.player_title_templates = load_templates(PLAYER_TITLE_ASSET_DIR)
        self.log.write(
            "templates_reloaded",
            monsters=len(self.templates),
            player=len(self.player_templates),
            head=len(self.player_head_templates),
            title=len(self.player_title_templates),
        )

    def apply_config(self, config: dict[str, Any]) -> None:
        with self.config_lock:
            was_armed = self.armed
            if was_armed:
                self.disarm("配置已更新")
            new_delivery = input_delivery(config)
            hwnd = self.keyboard.root_hwnd or self.keyboard.hwnd
            if new_delivery != self.delivery:
                self.keyboard.release_all()
                self.delivery = new_delivery
                self.background_input = new_delivery == "background"
                self.keyboard = Keyboard(self.delivery)
                if hwnd:
                    self.keyboard.bind_window(hwnd)
            self.config = config

    def reload_from_disk(self, config_path: Path) -> None:
        self.apply_config(load_config(config_path))
        self.reload_templates()

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
        if not user32.IsWindow(window.hwnd) or user32.IsIconic(window.hwnd):
            self.disarm("游戏窗口已关闭或最小化")
            return
        if not self.background_input and int(user32.GetForegroundWindow()) != window.hwnd:
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
        self.window = window
        game_pid, _game_path = window_process_path(window.hwnd)
        current_pid = int(ctypes.windll.kernel32.GetCurrentProcessId())
        current_integrity = process_integrity_level(current_pid)
        game_integrity = process_integrity_level(game_pid)
        self.integrity_ok = current_integrity < 0 or game_integrity < 0 or current_integrity >= game_integrity
        self.keyboard.bind_window(window.hwnd)
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
        print("按键投递：" + ("后台扫描码（失焦仍 SendInput，并补发窗口消息）。" if self.background_input else "前台 SendInput（游戏必须在前台）。"))
        if self.input_authorized and not self.integrity_ok:
            print("权限不足：助手权限低于游戏，输入会被 Windows 阻止。")
        print("F7 显隐校对信息，F8 启动或暂停，F9 / Ctrl+Shift+Q 立即退出。按键输入：" + ("已允许。" if self.input_authorized else "未允许（观察模式）。"))
        self.log.write(
            "session_start",
            input_authorized=self.input_authorized,
            delivery=self.delivery,
            input_hwnd=self.keyboard.hwnd,
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
                    if self.f7_requested.is_set():
                        self.f7_requested.clear()
                        self.toggle_calibration_overlay()
                    if self.vision_suspended.is_set():
                        time.sleep(0.05)
                        continue
                    vision = self.config["vision"]
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
                    self.ui_hp = hp
                    self.ui_mp = mp
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

                    attack_box = attack_box_from_config(self.config["behavior"])
                    player_raw_box = active_player_anchor.raw_box if active_player_anchor is not None else None
                    facing = self.direction or "right"
                    target = choose_nearest_target(
                        monsters,
                        player_box,
                        combat_img.shape[1],
                        combat_img.shape[0],
                        attack_box,
                        facing=facing,
                        raw_box=player_raw_box,
                    )
                    chase_target = None
                    if target is None:
                        chase_target = choose_nearest_same_level_target(
                            monsters,
                            player_box,
                            combat_img.shape[1],
                            combat_img.shape[0],
                            attack_box,
                            raw_box=player_raw_box,
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
                        raw_player_box = active_player_anchor.raw_box if active_player_anchor is not None else player_box
                        rx, ry, rw, rh = raw_player_box
                        player_screen_box = (combat_rect[0] + rx, combat_rect[1] + ry, rw, rh)
                        center_x, center_y = player_anchor_center(player_box, raw_player_box)
                        range_left, range_top, range_right, range_bottom = _ordered_rect(
                            *attack_rect_from_player(
                                (center_x, center_y),
                                combat_img.shape[1],
                                combat_img.shape[0],
                                attack_box,
                                facing,
                            )
                        )
                        attack_range_box = (
                            combat_rect[0] + int(round(range_left)),
                            combat_rect[1] + int(round(range_top)),
                            int(round(range_right - range_left)),
                            int(round(range_bottom - range_top)),
                        )
                    state_label = STATE_LABELS.get(self.state, self.state)
                    if not self.player_templates:
                        banner = "缺少玩家姓名板模板｜请在控制面板采集姓名板"
                    elif self.input_authorized:
                        if not self.integrity_ok:
                            banner = "输入权限不足｜请在控制面板点击「启动挂机」，并在 UAC 中选择“是”"
                        elif self.background_input:
                            banner = (
                                f"{'运行中' if self.armed else '输入待命'}｜后台按键｜{state_label}"
                                "｜F7 校对｜F8 启动/暂停｜F9 / Ctrl+Shift+Q 退出"
                            )
                        else:
                            banner = f"{'运行中' if self.armed else '输入待命'}｜{state_label}｜F7 校对｜F8 启动/暂停｜F9 / Ctrl+Shift+Q 退出"
                    else:
                        banner = "仅观察，不会发送按键｜在控制面板点击「启动挂机」｜F7 校对｜F9 / Ctrl+Shift+Q 退出"
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
                            "show_calibration": self.calibration_overlay_visible,
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
