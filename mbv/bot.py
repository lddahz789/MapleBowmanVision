from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path
import threading
import time
from typing import Any

import mss

from mbv.buffs import (
    AutoBuffController,
    BUFF_KEY_HOLD_SECONDS,
    BUFF_PRE_CAST_SECONDS,
    BuffAction,
)
from mbv.config import SessionLog, load_config
from mbv.input import Keyboard, input_delivery, key_is_down, rising_edge, vk_for
from mbv.movement import MoveProgress, StepMotion, directed_progress
from mbv.background_capture import BackgroundCapture, BackgroundCaptureError
from mbv.overlay import RuntimeOverlay
from mbv.performance import PerformanceMonitor
from mbv.player_tracking import PlayerTrackState
from mbv.potion import AutoPotionController, PotionAction
from mbv.strategies import active_strategy, missing_recognition_data, strategy_settings
from mbv.strategies.base import StrategyActionContext, TargetSelectionContext, horizontal_overlap_ratio
from mbv.template_store import template_roots_from_config, template_trash_from_config
from mbv.vision import (
    Detection,
    MINIMAP_REGION_SPACE,
    PLAYER_RELATIVE_REGION_SPACE,
    PlayerAnchor,
    SceneFeatures,
    _ordered_rect,
    attack_rect_from_player,
    bar_fill,
    choose_fused_player_anchor,
    crop,
    deduplicate_nameplate_detections,
    find_detections,
    load_templates,
    monster_template_category,
    monster_templates_for_category,
    player_attack_anchor,
    player_marker_observation,
    player_relative_region_rect,
    player_tracking_roi,
    roi_pixels,
    smooth_player_attack_anchor,
    suppress_monster_detections,
    verify_nameplate_identities,
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
from mbv.window import (
    capture_client,
    client_window,
    find_game_window,
    focus_game_window,
    set_window_topmost,
    window_process_path,
)

STATE_LABELS = {
    "MOVEMENT_PREPARE": "停攻等待移动",
    "PERIODIC_STEP_VERIFY": "检查向右小步实际位移",
    "MOVEMENT_RETRY": "移动无进展，重新按键尝试",
    "PAUSED": "已暂停",
    "SCANNING": "正在搜索目标",
    "PATROL_LEFT": "向左巡逻",
    "PATROL_RIGHT": "向右巡逻",
    "ATTACK_LEFT": "向左攻击",
    "ATTACK_RIGHT": "向右攻击",
    "FACE_TARGET_LEFT": "面向左侧目标",
    "FACE_TARGET_RIGHT": "面向右侧目标",
    "CHASE_LEFT": "向左接近目标",
    "CHASE_RIGHT": "向右接近目标",
    "PLAYER_SCREEN_LOST": "正在识别玩家位置",
    "PLAYER_RECOVER_LEFT": "姓名板丢失，向左位移",
    "PLAYER_RECOVER_RIGHT": "姓名板丢失，向右位移",
    "TARGET_OUT_OF_RANGE": "战斗区暂无有效目标",
    "HP_POTION": "正在使用回血药",
    "MP_POTION": "正在使用回蓝药",
    "BUFF_1": "正在补 Buff 1",
    "BUFF_2": "正在补 Buff 2",
    "BUFF_3": "正在补 Buff 3",
    "PICKUP": "正在拾取",
    "MARKER_LOST": "玩家标记丢失",
    "RETURN_CENTER_LEFT": "向左返回平台安全点",
    "RETURN_CENTER_RIGHT": "向右返回平台安全点",
    "RETURN_CENTER_JUMP_LEFT": "向左跳回平台安全点",
    "RETURN_CENTER_JUMP_RIGHT": "向右跳回平台安全点",
    "RETURN_CENTER_JUMP_UP": "向上跳回平台安全点",
    "RETURN_CENTER_DOWN_JUMP": "下跳返回平台安全点",
    "WAITING_CENTER_JUMP": "等待下一次平台回位跳跃",
    "PERIODIC_STEP_RIGHT": "定时向右短步",
    "PERIODIC_STEP_RETURNED": "已回到平台安全点",
    "RETURN_SAFE_LEFT": "向左返回安全输出区",
    "RETURN_SAFE_RIGHT": "向右返回安全输出区",
    "SAFE_OUTPUT_UNCALIBRATED": "请重新框选小地图安全输出区",
    "SAFE_PATROL_LEFT": "在安全输出区内向左巡逻",
    "SAFE_PATROL_RIGHT": "在安全输出区内向右巡逻",
    "RETURN_SAFE_JUMP_LEFT": "向左跳回安全输出区",
    "RETURN_SAFE_JUMP_RIGHT": "向右跳回安全输出区",
    "RETURN_SAFE_JUMP_UP": "向上跳回安全输出区",
    "WAITING_SAFE_JUMP": "等待下一次回位跳跃",
    "SAFE_OUTPUT_ABOVE": "位于安全区上方，等待人工处理",
    "JUMP_ATTACK_NEAR": "近目标跳跃攻击",
    "WAITING_NEAR_JUMP_ATTACK": "等待近目标跳跃攻击间隔",
    "JUMP_ATTACK_CLOSE": "近身跳跃攻击",
    "WAITING_JUMP_ATTACK": "等待近身跳跃攻击冷却",
}

PLAYER_LOST_RECOVERY_PAUSE_SECONDS = 0.08
DYNAMIC_DIRECTION_CHANGE_MIN_TAP_SECONDS = 0.06
DYNAMIC_DIRECTION_CHANGE_SETTLE_SECONDS = 0.08

DEFAULT_PLAYER_AUXILIARY_INTERVAL_SECONDS = 0.5
JUMP_ATTACK_LEAD_SECONDS = 0.05
JUMP_ATTACK_OVERLAP_SECONDS = 0.05
DOWN_JUMP_LEAD_SECONDS = 0.04


def runtime_limit_reached(
    started_at: float,
    now: float,
    max_runtime_minutes: float,
) -> bool:
    """非正数表示不限制运行时长；正数按分钟计算截止时间。"""
    minutes = float(max_runtime_minutes)
    return minutes > 0.0 and now - started_at >= minutes * 60.0


def should_run_player_auxiliary_detections(
    previous_anchor: PlayerAnchor | None,
    nameplate_anchor: PlayerAnchor | None,
    now: float,
    last_auxiliary_at: float,
    interval_seconds: float,
) -> bool:
    """姓名板稳定跟踪时降频辅助检测；任何不确定情况都立即恢复三路检测。"""
    if previous_anchor is None or nameplate_anchor is None:
        return True
    if previous_anchor.source != "姓名板":
        return True
    interval = max(0.0, float(interval_seconds))
    return last_auxiliary_at <= 0.0 or now - last_auxiliary_at >= interval


def player_anchor_within_hold(
    anchor: PlayerAnchor | None,
    last_seen_at: float,
    now: float,
    hold_seconds: float,
) -> PlayerAnchor | None:
    """仅把仍在保持窗口内的锚点作为下一帧定位先验。"""
    if anchor is not None and now - last_seen_at <= float(hold_seconds):
        return anchor
    return None


def configured_player_marker(config: dict[str, Any]) -> tuple[float, float] | None:
    """读取采集时确认的初始小地图位置，供启动首帧消歧。"""
    value = config.get("calibration", {}).get("player_marker_position")
    if not isinstance(value, list) or len(value) != 2:
        return None
    try:
        x, y = float(value[0]), float(value[1])
    except (TypeError, ValueError):
        return None
    if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
        return None
    return x, y


class BowmanBot:
    def __init__(self, config: dict[str, Any], input_authorized: bool) -> None:
        self.config = config
        self.strategy = active_strategy(config)
        self.input_authorized = input_authorized
        self.delivery = input_delivery(config)
        self.background_input = self.delivery in {"background", "window_message"}
        self.keyboard = Keyboard(self.delivery)
        self.template_roots = template_roots_from_config(config)
        self.template_trash_dir = template_trash_from_config(config)
        self.active_monster_category = str(config["vision"].get("active_monster_category", "")).strip()
        self.templates = monster_templates_for_category(
            load_templates(self.template_roots.monster, recursive=True),
            self.active_monster_category,
        )
        self.monster_filter_templates = monster_templates_for_category(
            load_templates(self.template_roots.filter, recursive=True),
            self.active_monster_category,
        )
        self.player_templates = load_templates(self.template_roots.player)
        self.player_head_templates = load_templates(self.template_roots.head)
        self.player_title_templates = load_templates(self.template_roots.title)
        self.log = SessionLog()
        self.performance = PerformanceMonitor(max(2.0, float(config["capture"]["fps"])))
        self.armed = False
        self.state = "PAUSED"
        self.direction: str | None = None
        self.marker = configured_player_marker(config)
        self.marker_last_seen = time.monotonic()
        self.last_target_seen = 0.0
        self.last_attack = 0.0
        self.last_jump = 0.0
        self.last_jump_attack = 0.0
        self.attack_facing_ready_at = 0.0
        self.last_strategy_attack_skill: str | None = None
        self.last_periodic_step = 0.0
        self.periodic_step_pending_return = False
        self.step_motion: StepMotion | None = None
        self.move_progress: MoveProgress | None = None
        self.last_pickup = 0.0
        self.started_at = 0.0
        self.hotkey_state: dict[str, bool] = {}
        self.hotkey_stop = threading.Event()
        self.hotkey_thread_id = 0
        self.f7_requested = threading.Event()
        self.f8_requested = threading.Event()
        self.f9_requested = threading.Event()
        self.potion_enabled_requested: bool | None = None
        self.calibration_overlay_visible = True
        self.calibration_overlay_hidden_items: frozenset[str] = frozenset()
        self.notice = ""
        self.notice_until = 0.0
        self.last_detection_box: tuple[int, int, int, int] | None = None
        self.last_detection_score = -1.0
        self.last_detection_name: str | None = None
        self.last_detection_at = 0.0
        self.last_detections: list[Detection] = []
        self.player_track = PlayerTrackState()
        self.last_attack_anchor: tuple[float, float] | None = None
        self.nameplate_visible_this_frame = False
        self.last_nameplate_seen_at = time.monotonic()
        self.player_recognition_lost_at = 0.0
        self.player_recognition_last_log_at = 0.0
        self.player_lost_recovery_direction: str | None = None
        self.player_lost_recovery_started_at = 0.0
        self.player_lost_recovery_next_at = 0.0
        self.player_lost_recovery_next_direction = "left"
        self.auto_potion = AutoPotionController()
        self.auto_buff = AutoBuffController()
        self.buff_preparation: tuple[BuffAction, float] | None = None
        self.integrity_ok = True
        self.vision_suspended = threading.Event()
        self.window: WindowInfo | None = None
        self.window_topmost = False
        self.ui_hp = 0.0
        self.ui_mp = 0.0
        self.config_lock = threading.Lock()
        self.action_lock = threading.RLock()

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
        self.buff_preparation = None
        self.step_motion = None
        self.move_progress = None
        track = getattr(self, "player_track", None)
        if track is not None:
            track.cancel_reacquisition()
        self.armed = False
        self.state = "PAUSED"
        self.attack_facing_ready_at = 0.0
        self.last_strategy_attack_skill = None
        self.periodic_step_pending_return = False
        try:
            self.keyboard.release_all()
            self._reset_player_lost_recovery(release_key=False)
        finally:
            if getattr(self, "window_topmost", False):
                window = getattr(self, "window", None)
                self.window_topmost = False
                if window is not None and user32.IsWindow(window.hwnd):
                    try:
                        set_window_topmost(window, False)
                    except OSError as exc:
                        self.log.write("window_topmost_error", enabled=False, error=str(exc))
        if was_armed:
            self.log.write("disarm", reason=reason)
            self.notify(f"已暂停：{reason}", 5.0)
            user32.MessageBeep(0x00000030)

    def toggle(self, window: WindowInfo) -> None:
        with self.action_lock:
            self._toggle(window)

    def _toggle(self, window: WindowInfo) -> None:
        if self.armed:
            self.disarm("按下了 F8")
            return
        if not self.input_authorized:
            self.notify("当前进程没有按键授权，请从唯一入口 Start.bat 启动。", 6.0)
            return
        if not self.config.get("calibrated"):
            self.notify("校准未完成，请依次采集状态栏、小地图和战斗识别区域。")
            return
        missing = missing_recognition_data(self.config, self.strategy)
        if missing:
            labels = {"platform_center": "小地图平台安全点"}
            labels.update(
                {field.recognition_key: field.button_label for field in self.strategy.capture_fields}
            )
            missing_text = "、".join(labels.get(key, key) for key in missing)
            self.notify(f"策略“{self.strategy.display_name}”需要先完成：{missing_text}。", 6.0)
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
        try:
            window = client_window(window.hwnd, window.title)
        except Exception as exc:
            self.notify(f"启动失败：无法读取当前游戏窗口（{exc}）", 6.0)
            return
        calibrated_size = self.config.get("calibration", {}).get("window_size")
        if (
            isinstance(calibrated_size, list)
            and len(calibrated_size) == 2
            and [window.width, window.height] != [int(calibrated_size[0]), int(calibrated_size[1])]
        ):
            self.notify(
                f"启动失败：游戏窗口已从 {calibrated_size[0]}×{calibrated_size[1]} 变为 "
                f"{window.width}×{window.height}，请按左侧项目重新校准。",
                8.0,
            )
            return
        self.keyboard.bind_window(window.hwnd)
        topmost_while_armed = bool(
            self.config.get("window", {}).get("topmost_while_armed", True)
        ) and self.delivery != "window_message"
        if int(user32.GetForegroundWindow()) != window.hwnd and (
            not self.background_input or topmost_while_armed
        ):
            try:
                focus_game_window(window, settle_seconds=0.15)
            except Exception as exc:
                if not self.background_input:
                    self.notify(f"启动失败：无法切换到游戏窗口（{exc}）", 6.0)
                    return
        if not self.background_input and int(user32.GetForegroundWindow()) != window.hwnd:
            self.notify("启动失败：游戏没有进入前台，请点击游戏后重试。", 6.0)
            return
        if topmost_while_armed:
            try:
                set_window_topmost(window, True)
            except OSError as exc:
                self.notify(f"启动失败：无法将游戏窗口置顶（{exc}）", 6.0)
                return
        self.window = window
        self.window_topmost = topmost_while_armed
        self.armed = True
        self.state = "SCANNING"
        self.started_at = time.monotonic()
        self.attack_facing_ready_at = 0.0
        self.last_strategy_attack_skill = None
        self.last_periodic_step = self.started_at
        self.periodic_step_pending_return = False
        self.log.write(
            "arm",
            window=window.title,
            templates=len(self.templates),
            delivery=self.delivery,
            input_hwnd=self.keyboard.hwnd,
            window_topmost=topmost_while_armed,
        )
        if self.delivery == "window_message":
            self.notify("已启动独立后台实验模式：仅窗口消息与窗口截图；请确认角色确实响应。", 8.0)
        elif self.background_input:
            window_mode = "游戏窗口置顶" if topmost_while_armed else "游戏窗口不置顶"
            self.notify(f"已经启动（后台按键、{window_mode}）。始终发送扫描码；控制面板不抢焦点。", 5.0)
        else:
            window_mode = "游戏窗口已置顶" if topmost_while_armed else "游戏窗口不置顶"
            self.notify(f"已经启动，{window_mode}。按 F8 暂停，F7 显隐 Debug 框，按 F9 或 Ctrl+Shift+Q 立即退出。", 3.0)
        user32.MessageBeep(0x00000040)

    def request_toggle(self) -> None:
        self.f8_requested.set()

    def request_auto_potion(self, enabled: bool) -> None:
        with self.action_lock:
            self.potion_enabled_requested = bool(enabled)

    def request_standalone_potion(self, enabled: bool) -> None:
        """兼容旧面板调用；开关现在控制全部自动喝药。"""
        self.request_auto_potion(enabled)

    def _calibration_item_complete(self, key: str) -> bool:
        calibration = self.config.get("calibration", {})
        items = calibration.get("items", {}) if isinstance(calibration, dict) else {}
        if isinstance(items, dict) and key in items:
            value = items[key]
            return bool(value.get("complete")) if isinstance(value, dict) else bool(value)
        return bool(calibration.get("status_regions_complete")) if isinstance(calibration, dict) else False

    def _set_auto_potion(self, window: WindowInfo, enabled: bool) -> None:
        with self.action_lock:
            if not enabled:
                was_enabled = self.auto_potion.enabled
                self.auto_potion.set_enabled(False)
                if was_enabled:
                    self.notify("全局自动喝药已关闭", 3.0)
                return
            if self.auto_potion.enabled:
                return
            if self.vision_suspended.is_set():
                self.notify("采集工具打开期间不能开启全局自动喝药。", 4.0)
                return
            if not self.input_authorized:
                self.notify("按键未授权，请从唯一入口 Start.bat 启动。", 5.0)
                return
            if not self.integrity_ok:
                self.notify("自动喝药无法启动：助手权限低于游戏。", 5.0)
                return
            if not user32.IsWindow(window.hwnd) or user32.IsIconic(window.hwnd):
                self.notify("自动喝药无法启动：游戏窗口已关闭或最小化。", 5.0)
                return
            missing = [
                label
                for key, label in (("hp_bar", "血条"), ("mp_bar", "蓝条"))
                if not self._calibration_item_complete(key)
            ]
            if missing:
                self.notify(f"自动喝药需要先采集：{'、'.join(missing)}。", 5.0)
                return
            self.auto_potion.set_enabled(True)
            if not self.armed and int(user32.GetForegroundWindow()) != window.hwnd:
                self.auto_potion.waiting_foreground = True
                self.notify("全局自动喝药已开启；暂停时切回游戏窗口后生效。", 5.0)
            else:
                self.notify("全局自动喝药已开启。", 4.0)
            self.player_track = PlayerTrackState()
            self.last_attack_anchor = None
            self.last_detections = []
            self.last_detection_at = 0.0

    def _set_standalone_potion(self, window: WindowInfo, enabled: bool) -> None:
        """兼容旧调用；开关现在控制全部自动喝药。"""
        self._set_auto_potion(window, enabled)

    def _try_auto_potion(
        self,
        window: WindowInfo,
        hp: float,
        mp: float,
        now: float,
    ) -> bool:
        standalone = not self.armed
        if not self.auto_potion.enabled:
            return False
        if not self.input_authorized or not self.integrity_ok:
            if standalone:
                self.auto_potion.set_unavailable("权限不足")
            return False
        if not user32.IsWindow(window.hwnd) or user32.IsIconic(window.hwnd):
            if standalone:
                self.auto_potion.set_unavailable("窗口不可用")
            return False
        if standalone and int(user32.GetForegroundWindow()) != window.hwnd:
            self.auto_potion.unavailable_reason = ""
            self.auto_potion.waiting_foreground = True
            return False
        self.auto_potion.unavailable_reason = ""
        self.auto_potion.waiting_foreground = False
        action: PotionAction | None = self.auto_potion.decide(
            hp,
            mp,
            now,
            self.config["behavior"],
            self.config["keys"],
        )
        if action is None:
            return False
        if self.armed:
            self._interrupt_step()
            self.stop_move()
            self._reset_player_lost_recovery(release_key=False)
        self.buff_preparation = None
        self.keyboard.tap(action.key)
        self.auto_potion.record(action, now)
        if self.armed:
            self.state = action.state
        self.log.write(
            f"{action.kind}_potion",
            fill=round(action.fill, 3),
            standalone=standalone,
        )
        return True

    def _try_auto_buff(self, now: float) -> bool:
        if not self.armed or not self.input_authorized:
            self.buff_preparation = None
            return False
        auto_buff = getattr(self, "auto_buff", None)
        buffs = self.config.get("buffs", {})
        if auto_buff is None or not isinstance(buffs, dict):
            return False
        if auto_buff.casting_guard_active(now):
            self._interrupt_step()
            self.stop_move()
            return True
        action: BuffAction | None = auto_buff.decide(buffs, now)
        if action is None:
            self.buff_preparation = None
            return False
        self._interrupt_step()
        self.stop_move()
        self._reset_player_lost_recovery(release_key=False)
        # 准备期由后续帧推进，视觉、暂停请求及配置开关继续得到处理。
        preparation = getattr(self, "buff_preparation", None)
        if preparation is None or preparation[0] != action:
            self.buff_preparation = (action, now + BUFF_PRE_CAST_SECONDS)
            self.state = action.slot.upper()
            return True
        if now < preparation[1]:
            return True
        if any(
            event is not None and event.is_set()
            for event in (
                getattr(self, "f8_requested", None),
                getattr(self, "f9_requested", None),
                getattr(self, "vision_suspended", None),
            )
        ):
            self.buff_preparation = None
            return True
        window = getattr(self, "window", None)
        if (
            window is None
            or not user32.IsWindow(window.hwnd)
            or user32.IsIconic(window.hwnd)
            or (
                not self.background_input
                and int(user32.GetForegroundWindow()) != window.hwnd
            )
        ):
            self.disarm("补 Buff 前游戏窗口已不可用或失去前台")
            return True
        self.buff_preparation = None
        self.keyboard.tap(action.key, BUFF_KEY_HOLD_SECONDS)
        cast_at = time.monotonic()
        auto_buff.record(action, cast_at)
        self.state = action.slot.upper()
        self.log.write(
            "buff",
            slot=action.slot,
            key=action.key,
            interval_seconds=action.interval_seconds,
            pre_cast_seconds=BUFF_PRE_CAST_SECONDS,
            key_hold_seconds=BUFF_KEY_HOLD_SECONDS,
            result="key_sent_unverified",
        )
        return True

    def toggle_calibration_overlay(self) -> None:
        self.set_calibration_overlay_visible(not self.calibration_overlay_visible)

    def set_calibration_overlay_visible(self, visible: bool) -> None:
        self.calibration_overlay_visible = bool(visible)
        hidden_items = getattr(self, "calibration_overlay_hidden_items", frozenset())
        self.log.write(
            "calibration_overlay",
            visible=self.calibration_overlay_visible,
            hidden_items=sorted(hidden_items),
        )

    def calibration_overlay_item_visible(self, item: str) -> bool:
        selected = str(item).strip()
        if not selected:
            raise ValueError("Debug 采集项不能为空")
        hidden_items = getattr(self, "calibration_overlay_hidden_items", frozenset())
        return selected not in hidden_items

    def set_calibration_overlay_item_visible(self, item: str, visible: bool) -> None:
        selected = str(item).strip()
        if not selected:
            raise ValueError("Debug 采集项不能为空")
        hidden = set(getattr(self, "calibration_overlay_hidden_items", frozenset()))
        if visible:
            hidden.discard(selected)
            self.calibration_overlay_visible = True
        else:
            hidden.add(selected)
        self.calibration_overlay_hidden_items = frozenset(hidden)
        self.log.write(
            "calibration_overlay_item",
            item=selected,
            visible=bool(visible),
            hidden_items=sorted(self.calibration_overlay_hidden_items),
        )

    def toggle_calibration_overlay_item(self, item: str) -> bool:
        visible = not self.calibration_overlay_item_visible(item)
        self.set_calibration_overlay_item_visible(item, visible)
        return visible

    def suspend_vision(self) -> None:
        with self.action_lock:
            self.vision_suspended.set()
            performance = getattr(self, "performance", None)
            if performance is not None:
                performance.set_suspended(True)
            self.f8_requested.clear()
            if hasattr(self, "potion_enabled_requested"):
                self.potion_enabled_requested = None
            if self.armed:
                self.disarm("打开采集工具")
            self.keyboard.release_all()

    def resume_vision(self) -> None:
        self.vision_suspended.clear()
        performance = getattr(self, "performance", None)
        if performance is not None:
            performance.set_suspended(False)

    def reload_templates(self) -> None:
        monster_templates = monster_templates_for_category(
            load_templates(self.template_roots.monster, recursive=True),
            self.active_monster_category,
        )
        monster_filter_templates = monster_templates_for_category(
            load_templates(self.template_roots.filter, recursive=True),
            self.active_monster_category,
        )
        player_templates = load_templates(self.template_roots.player)
        player_head_templates = load_templates(self.template_roots.head)
        player_title_templates = load_templates(self.template_roots.title)
        self.templates = monster_templates
        self.monster_filter_templates = monster_filter_templates
        self.player_templates = player_templates
        self.player_head_templates = player_head_templates
        self.player_title_templates = player_title_templates
        self.last_detections = []
        self.last_detection_box = None
        self.last_detection_score = -1.0
        self.last_detection_name = None
        self.last_detection_at = 0.0
        self.last_attack_anchor = None
        self.player_track = PlayerTrackState()
        self.nameplate_visible_this_frame = False
        self.last_nameplate_seen_at = time.monotonic()
        self.player_recognition_lost_at = 0.0
        self.player_recognition_last_log_at = 0.0
        self.log.write(
            "templates_reloaded",
            category=self.active_monster_category,
            monsters=len(self.templates),
            filters=len(self.monster_filter_templates),
            player=len(self.player_templates),
            head=len(self.player_head_templates),
            title=len(self.player_title_templates),
        )

    def apply_config(self, config: dict[str, Any]) -> None:
        with self.action_lock:
            self.buff_preparation = None
            self.step_motion = None
            self.move_progress = None
            self.f8_requested.clear()
            if hasattr(self, "potion_enabled_requested"):
                self.potion_enabled_requested = None
            with self.config_lock:
                old_marker_seed = configured_player_marker(getattr(self, "config", {}))
                new_marker_seed = configured_player_marker(config)
                was_armed = self.armed
                if was_armed:
                    self.disarm("配置已更新")
                else:
                    self._reset_player_lost_recovery(release_key=True)
                new_delivery = input_delivery(config)
                hwnd = self.keyboard.root_hwnd or self.keyboard.hwnd
                if new_delivery != self.delivery:
                    self.keyboard.release_all()
                    self.delivery = new_delivery
                    self.background_input = new_delivery in {"background", "window_message"}
                    self.keyboard = Keyboard(self.delivery)
                    if hwnd:
                        self.keyboard.bind_window(hwnd)
                self.config = config
                self.template_roots = template_roots_from_config(config)
                self.template_trash_dir = template_trash_from_config(config)
                self.strategy = active_strategy(config)
                self.attack_facing_ready_at = 0.0
                self.last_strategy_attack_skill = None
                self.last_periodic_step = time.monotonic()
                self.periodic_step_pending_return = False
                self.active_monster_category = str(
                    config["vision"].get("active_monster_category", "")
                ).strip()
                self.player_track = PlayerTrackState()
                self.last_attack_anchor = None
                self.nameplate_visible_this_frame = False
                self.last_nameplate_seen_at = time.monotonic()
                self.player_recognition_lost_at = 0.0
                self.player_recognition_last_log_at = 0.0
                if new_marker_seed != old_marker_seed:
                    self.marker = new_marker_seed
                    self.marker_last_seen = time.monotonic()

    def reload_from_disk(self, config_path: Path) -> None:
        self.apply_config(load_config(config_path))
        self.reload_templates()

    def preview_strategy_setting(self, path: str, value: Any) -> None:
        """实时预览策略参数，不暂停挂机；面板同时负责持久化。"""
        parts = str(path).split(".")
        if not parts or any(not part for part in parts):
            raise ValueError("策略参数路径无效")
        with self.action_lock:
            with self.config_lock:
                cursor: dict[str, Any] = strategy_settings(self.config)
                for part in parts[:-1]:
                    child = cursor.get(part)
                    if not isinstance(child, dict):
                        raise KeyError(f"策略参数路径不存在：{path}")
                    cursor = child
                if parts[-1] not in cursor:
                    raise KeyError(f"策略参数不存在：{path}")
                cursor[parts[-1]] = value
        self.log.write("strategy_setting_preview", strategy=self.strategy.key, path=path, value=value)

    def preview_targeting_setting(self, path: str, value: float) -> None:
        """实时预览公共索敌区参数，不暂停挂机。"""
        parts = str(path).split(".")
        if not parts or any(not part for part in parts):
            raise ValueError("索敌区参数路径无效")
        with self.action_lock:
            with self.config_lock:
                cursor: dict[str, Any] = self.config["targeting"]
                for part in parts[:-1]:
                    child = cursor.get(part)
                    if not isinstance(child, dict):
                        raise KeyError(f"索敌区参数路径不存在：{path}")
                    cursor = child
                if parts[-1] not in cursor:
                    raise KeyError(f"索敌区参数不存在：{path}")
                cursor[parts[-1]] = float(value)
        self.log.write("targeting_setting_preview", path=path, value=float(value))

    def preview_config_setting(self, path: str, value: Any) -> None:
        """实时更新不需要重建输入器的普通配置项。"""
        parts = str(path).split(".")
        if not parts or any(not part for part in parts):
            raise ValueError("配置参数路径无效")
        with self.action_lock:
            with self.config_lock:
                cursor: dict[str, Any] = self.config
                for part in parts[:-1]:
                    child = cursor.get(part)
                    if not isinstance(child, dict):
                        raise KeyError(f"配置参数路径不存在：{path}")
                    cursor = child
                if parts[-1] not in cursor:
                    raise KeyError(f"配置参数不存在：{path}")
                cursor[parts[-1]] = value
        self.log.write("config_setting_preview", path=path, value=value)

    def move(self, direction: str) -> None:
        keys = self.config["keys"]
        opposite = "left" if direction == "right" else "right"
        self.keyboard.up(keys[opposite])
        if getattr(self, "delivery", "foreground") == "window_message":
            self.keyboard.movement_down(keys[direction])
        else:
            self.keyboard.down(keys[direction])
        self.direction = direction
        self.attack_facing_ready_at = 0.0
        self.state = f"PATROL_{direction.upper()}"

    def stop_move(self) -> None:
        keys = self.config["keys"]
        self.keyboard.up(keys["left"])
        self.keyboard.up(keys["right"])
        self.move_progress = None

    def _interrupt_step(self) -> None:
        motion = getattr(self, "step_motion", None)
        if motion is not None:
            motion.phase = "prepare"
            motion.deadline = time.monotonic() + 0.6
            motion.baseline = None

    def periodic_step(self, direction: str, seconds: float, now: float, state: str) -> None:
        """跨帧停攻、移动、验证；不能把发出按键当成已经移动。"""
        desired = str(direction).strip().lower()
        if desired not in {"left", "right"}:
            raise ValueError(f"短步方向无效：{direction}")
        duration = max(0.03, min(0.5, float(seconds)))
        self.stop_move()
        self.step_motion = StepMotion(desired, duration, now, now + 0.6)
        self.state = "MOVEMENT_PREPARE"

    def _advance_periodic_step(self, marker: tuple[float, float], now: float) -> bool:
        motion = getattr(self, "step_motion", None)
        if motion is None:
            return False
        if now - motion.started > 12.0:
            self.disarm("小步动作长时间无法完成，请检查识别、补药与移动输入")
            return True
        if now < motion.deadline:
            return True
        if motion.phase == "prepare":
            if motion.attempts >= 3:
                self.disarm("小步移动多次中断，已停止重试")
                return True
            motion.baseline = marker[0]
            motion.attempts += 1
            self.stop_move()
            duration = min(0.5, motion.seconds * motion.attempts)
            self.keyboard.movement_down(self.config["keys"][motion.direction], seconds=duration)
            self.direction = motion.direction
            self.attack_facing_ready_at = 0.0
            motion.phase = "hold"
            motion.deadline = now + duration
            self.state = "PERIODIC_STEP_RIGHT"
            self.log.write("periodic_step", direction=motion.direction,
                           seconds=round(motion.deadline - now, 3), attempt=motion.attempts,
                           marker=marker, result="key_sent_unverified")
        elif motion.phase == "hold":
            self.stop_move()
            motion.phase = "verify"
            motion.deadline = now + 0.25
            self.state = "PERIODIC_STEP_VERIFY"
        else:
            delta = directed_progress(float(motion.baseline), marker[0], motion.direction,
                                      getattr(self, "live_minimap_width", 100))
            if delta >= 0.5:
                self.log.write("periodic_step_verified", direction=motion.direction,
                               displacement_pixels=round(delta, 3), attempt=motion.attempts)
                self.step_motion = None
                self.last_periodic_step = now
                self.periodic_step_pending_return = True
            elif motion.attempts >= 3 or delta <= -0.5:
                self.log.write("movement_failed", action="step", marker=marker,
                               displacement_pixels=round(delta, 3), attempt=motion.attempts)
                self.disarm("小步未检测到正确方向位移：后台方向键可能不兼容，或角色被阻挡")
            else:
                motion.phase = "prepare"
                motion.deadline = now + 0.3
                self.state = "MOVEMENT_RETRY"
        return True

    def _move_with_feedback(self, direction: str, marker: tuple[float, float],
                            now: float, state: str) -> None:
        progress = getattr(self, "move_progress", None)
        if progress is None or progress.direction != direction:
            self.stop_move()
            ready_at = max(now, float(getattr(self, "last_attack", now)) + 0.6)
            self.move_progress = MoveProgress(direction, marker[0], ready_at, ready_at)
            self.state = "MOVEMENT_PREPARE"
            self.log.write("movement_start", direction=direction, marker=marker,
                           delivery=getattr(self, "delivery", "foreground"))
            return
        if now < progress.ready_at:
            self.state = "MOVEMENT_PREPARE"
            return
        delta = directed_progress(progress.baseline, marker[0], direction,
                                  getattr(self, "live_minimap_width", 100))
        if delta >= 0.5:
            progress.baseline = marker[0]
            progress.progress_at = now
            progress.retried = False
            self.log.write("movement_progress", direction=direction, marker=marker,
                           displacement_pixels=round(delta, 3))
        stalled = now - progress.progress_at
        if stalled >= 4.0:
            self.log.write("movement_failed", action="return", direction=direction,
                           marker=marker, no_progress_seconds=round(stalled, 3))
            self.disarm("回位连续 4 秒无正确方向位移：请检查后台移动兼容性、障碍或角色状态")
            return
        if stalled >= 2.0 and not progress.retried:
            self.keyboard.up(self.config["keys"][direction])
            progress.retried = True
            progress.ready_at = now + 0.1
            self.state = "MOVEMENT_RETRY"
            self.log.write("movement_retry", direction=direction, marker=marker)
            return
        self.move(direction)
        self.state = state

    def _reset_player_lost_recovery(self, *, release_key: bool) -> None:
        current = getattr(self, "player_lost_recovery_direction", None)
        if release_key and current in {"left", "right"}:
            self.keyboard.up(self.config["keys"][current])
        self.player_lost_recovery_direction = None
        self.player_lost_recovery_started_at = 0.0
        self.player_lost_recovery_next_at = 0.0
        self.player_lost_recovery_next_direction = "left"

    def recover_player_nameplate(self, now: float) -> None:
        """三路玩家定位均丢失时，以固定屏幕方向交替左右位移。"""
        behavior = self.config["behavior"]
        keys = self.config["keys"]
        move_seconds = max(0.05, min(2.0, float(behavior["player_lost_move_seconds"])))
        current = getattr(self, "player_lost_recovery_direction", None)
        if current in {"left", "right"}:
            self.state = f"PLAYER_RECOVER_{current.upper()}"
            started_at = float(getattr(self, "player_lost_recovery_started_at", now))
            if now - started_at < move_seconds:
                return
            self.keyboard.up(keys[current])
            self.player_lost_recovery_direction = None
            self.player_lost_recovery_next_direction = "right" if current == "left" else "left"
            self.player_lost_recovery_next_at = now + PLAYER_LOST_RECOVERY_PAUSE_SECONDS
            self.state = "PLAYER_SCREEN_LOST"
            self.log.write("player_lost_move_end", direction=current)
            return
        if now < float(getattr(self, "player_lost_recovery_next_at", 0.0)):
            self.state = "PLAYER_SCREEN_LOST"
            return
        direction = str(getattr(self, "player_lost_recovery_next_direction", "left"))
        if direction not in {"left", "right"}:
            direction = "left"
        self.stop_move()
        self.keyboard.down(keys[direction])
        self.direction = direction
        self.attack_facing_ready_at = 0.0
        self.player_lost_recovery_direction = direction
        self.player_lost_recovery_started_at = now
        self.state = f"PLAYER_RECOVER_{direction.upper()}"
        self.log.write(
            "player_lost_move_start",
            direction=direction,
            seconds=round(move_seconds, 3),
        )

    def face_and_attack(
        self,
        target_x: float,
        player_x: float,
        now: float,
        face_each_attack: bool = True,
        attack_key: str | None = None,
        attack_skill: str | None = None,
    ) -> None:
        behavior = self.config["behavior"]
        keys = self.config["keys"]
        selected_attack_key = str(attack_key or "").strip().lower() or keys["attack"]
        self.stop_move()
        desired = "left" if target_x < player_x else "right"
        dead = float(behavior["attack_dead_zone"])
        if abs(target_x - player_x) <= dead and self.direction is not None:
            desired = self.direction
        self.state = f"ATTACK_{desired.upper()}"
        if now - self.last_attack >= float(behavior["attack_interval_seconds"]):
            # NewMaple 在上一技能动作尚未结束时，可能忽略与下一次技能同时到达的换向。
            # 动态策略换边时先独立转向并等待下一帧再次确认目标仍在同侧，避免内部方向
            # 已更新、角色画面却仍朝旧方向。目标左右抖动时也只会转向，不会朝错误方向出手。
            if face_each_attack and self.direction != desired:
                previous = self.direction
                tap_seconds = max(
                    DYNAMIC_DIRECTION_CHANGE_MIN_TAP_SECONDS,
                    float(behavior["face_tap_seconds"]),
                )
                self.keyboard.tap(keys[desired], tap_seconds)
                self.direction = desired
                self.attack_facing_ready_at = (
                    time.monotonic() + DYNAMIC_DIRECTION_CHANGE_SETTLE_SECONDS
                )
                self.state = f"FACE_TARGET_{desired.upper()}"
                self.log.write(
                    "attack_face_prepare",
                    direction=desired,
                    previous_direction=previous,
                    tap_seconds=round(tap_seconds, 3),
                    settle_seconds=DYNAMIC_DIRECTION_CHANGE_SETTLE_SECONDS,
                )
                return
            if face_each_attack and time.monotonic() < float(
                getattr(self, "attack_facing_ready_at", 0.0)
            ):
                self.state = f"FACE_TARGET_{desired.upper()}"
                return
            # 已确认面向后，每次攻击仍短暂按住目标方向，在方向键按下期间发送技能。
            # 原地策略保持原行为，只在换边时点按方向。
            if face_each_attack:
                direction_key = keys[desired]
                try:
                    self.keyboard.down(direction_key)
                    time.sleep(float(behavior["face_tap_seconds"]))
                    self.direction = desired
                    self.keyboard.tap(selected_attack_key)
                finally:
                    self.keyboard.up(direction_key)
            else:
                if self.direction != desired:
                    self.keyboard.tap(keys[desired], float(behavior["face_tap_seconds"]))
                    self.direction = desired
                self.keyboard.tap(selected_attack_key)
            self.last_attack = now
            self.log.write(
                "attack",
                direction=desired,
                player_x=round(player_x, 3),
                target_x=round(target_x, 3),
                distance=round(abs(target_x - player_x), 3),
                skill=str(attack_skill or "single"),
                key=selected_attack_key,
            )

    def face_target(
        self,
        direction: str,
        state: str,
        tap_seconds: float | None = None,
    ) -> None:
        """只短按一次方向键改变面向，不进入移动或追踪状态。"""
        desired = str(direction).strip().lower()
        if desired not in {"left", "right"}:
            raise ValueError(f"目标面向无效：{direction}")
        self.stop_move()
        if self.direction != desired:
            duration = (
                float(self.config["behavior"]["face_tap_seconds"])
                if tap_seconds is None
                else max(0.0, float(tap_seconds))
            )
            self.keyboard.tap(
                self.config["keys"][desired],
                duration,
            )
            self.direction = desired
        self.state = state
        self.log.write("face_target", direction=desired)

    def chase_target(self, target_x: float, player_x: float) -> None:
        desired = "left" if target_x < player_x else "right"
        self.move(desired)
        self.state = f"CHASE_{desired.upper()}"

    def jump_to_safe(self, direction: str | None, now: float, state: str) -> None:
        if direction is None:
            self.stop_move()
        else:
            self.move(direction)
        self.keyboard.tap(self.config["keys"]["jump"])
        self.last_jump = now
        self.state = state
        self.log.write("safe_return_jump", direction=direction or "up")

    def down_jump_to_safe(self, now: float, state: str) -> None:
        """按住下方向后点按跳跃，并无论输入是否失败都释放下方向。"""
        keys = self.config["keys"]
        self.stop_move()
        try:
            self.keyboard.down(keys["down"])
            time.sleep(DOWN_JUMP_LEAD_SECONDS)
            self.keyboard.tap(keys["jump"])
        finally:
            self.keyboard.up(keys["down"])
        self.last_jump = now
        self.state = state
        self.log.write("safe_return_down_jump")

    def jump_attack(
        self,
        target_x: float,
        player_x: float,
        overlap_ratio: float,
        now: float,
        state: str = "JUMP_ATTACK_CLOSE",
    ) -> None:
        keys = self.config["keys"]
        self.stop_move()
        desired = "left" if target_x < player_x else "right"
        if self.direction != desired:
            self.keyboard.tap(keys[desired], float(self.config["behavior"]["face_tap_seconds"]))
            self.direction = desired
        jump_key = keys["jump"]
        attack_key = keys["attack"]
        try:
            self.keyboard.down(jump_key)
            time.sleep(JUMP_ATTACK_LEAD_SECONDS)
            self.keyboard.down(attack_key)
            time.sleep(JUMP_ATTACK_OVERLAP_SECONDS)
        finally:
            self.keyboard.up(attack_key)
            self.keyboard.up(jump_key)
        self.last_jump_attack = now
        self.last_attack = now
        self.state = state
        self.log.write(
            "jump_attack",
            state=state,
            direction=desired,
            overlap_ratio=round(overlap_ratio, 4),
            jump_key=jump_key,
            attack_key=attack_key,
        )

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
        combat_height: int = 1,
        eligible_detections: tuple[Detection, ...] = (),
    ) -> None:
        with self.action_lock:
            if not self.armed:
                self._try_auto_potion(window, hp, mp, now)
                return
            self._act(
                window,
                hp,
                mp,
                marker,
                player_box,
                target_box,
                chase_box,
                combat_width,
                has_monster_candidates,
                now,
                combat_height,
                eligible_detections,
            )

    def _act(
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
        combat_height: int = 1,
        eligible_detections: tuple[Detection, ...] = (),
    ) -> None:
        if not self.armed or not self.input_authorized:
            return
        self.keyboard.check_health()
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
        if self._try_auto_potion(window, hp, mp, now):
            return
        if self._try_auto_buff(now):
            return
        nameplate_lost_seconds = max(
            0.0,
            float(self.config["vision"].get("player_hold_seconds", 0.8)),
        )
        nameplate_lost = (
            now - float(getattr(self, "last_nameplate_seen_at", now))
            >= nameplate_lost_seconds
        )
        if bool(behavior.get("player_lost_recovery_enabled", True)) and nameplate_lost:
            self._interrupt_step()
            self.recover_player_nameplate(now)
            return
        if player_box is None:
            self._reset_player_lost_recovery(release_key=True)
            self._interrupt_step()
            self.stop_move()
            self.state = "PLAYER_SCREEN_LOST"
            return
        self._reset_player_lost_recovery(release_key=True)
        if marker is None or not getattr(self, "live_marker_unambiguous", True):
            self._interrupt_step()
            self.stop_move()
            self.state = "MARKER_LOST"
            return
        if self._advance_periodic_step(marker, now):
            return
        decision = self.strategy.decide(
            StrategyActionContext(
                marker=marker,
                player_box=player_box,
                player_anchor=self.last_attack_anchor,
                target_box=target_box,
                chase_box=chase_box,
                combat_width=combat_width,
                has_monster_candidates=has_monster_candidates,
                now=now,
                last_target_seen=self.last_target_seen,
                last_pickup=self.last_pickup,
                direction=self.direction,
                behavior=behavior,
                settings=strategy_settings(self.config),
                recognition=self.config["recognition"],
                combat_height=combat_height,
                last_jump=getattr(self, "last_jump", 0.0),
                last_jump_attack=getattr(self, "last_jump_attack", 0.0),
                last_periodic_step=getattr(self, "last_periodic_step", self.started_at),
                periodic_step_pending_return=bool(
                    getattr(self, "periodic_step_pending_return", False)
                ),
                eligible_detections=eligible_detections,
                previous_attack_skill=getattr(self, "last_strategy_attack_skill", None),
            )
        )
        if decision.attack_skill is not None:
            self.last_strategy_attack_skill = decision.attack_skill
        if decision.periodic_step_return_complete:
            self.periodic_step_pending_return = False
        if decision.target_seen:
            self.last_target_seen = now
        if decision.action != "move":
            self.move_progress = None
        if decision.action == "face":
            self.face_target(
                str(decision.direction),
                decision.state,
                decision.face_tap_seconds,
            )
        elif decision.action == "attack":
            self.face_and_attack(
                float(decision.target_x),
                float(decision.player_x),
                now,
                face_each_attack=decision.face_each_attack,
                attack_key=decision.attack_key,
                attack_skill=decision.attack_skill,
            )
        elif decision.action == "chase":
            self.chase_target(float(decision.target_x), float(decision.player_x))
        elif decision.action == "move":
            self._move_with_feedback(str(decision.direction), marker, now, decision.state)
        elif decision.action == "step":
            self.periodic_step(
                str(decision.direction),
                float(decision.move_seconds or 0.03),
                now,
                decision.state,
            )
        elif decision.action == "jump":
            self.jump_to_safe(decision.direction, now, decision.state)
        elif decision.action == "down_jump":
            self.down_jump_to_safe(now, decision.state)
        elif decision.action == "jump_attack":
            self.jump_attack(
                float(decision.target_x),
                float(decision.player_x),
                float(decision.close_overlap_ratio or 0.0),
                now,
                decision.state,
            )
        elif decision.action == "pickup":
            self.stop_move()
            self.keyboard.tap(keys["pickup"])
            self.last_pickup = now
            self.state = decision.state
        else:
            self.stop_move()
            self.state = decision.state

    def _detect_player_nameplate(
        self,
        scene: SceneFeatures,
        vision: dict[str, Any],
        search_roi: tuple[int, int, int, int] | None,
        *,
        threshold: float | None = None,
    ) -> tuple[list[Detection], float, str | None]:
        detections, score, template_name = find_detections(
            scene,
            self.player_templates,
            (
                float(vision.get("player_template_threshold", 0.76))
                if threshold is None
                else float(threshold)
            ),
            float(vision.get("player_detection_scale", 0.5)),
            max_per_template=8,
            nms_iou=0.35,
            max_detections=max(8, len(self.player_templates) * 8),
            structure_weight=0.55,
            search_roi=search_roi,
            nms_across_templates=False,
        )
        verified = verify_nameplate_identities(scene.scene, detections, self.player_templates)
        return deduplicate_nameplate_detections(verified, nms_iou=0.35, max_detections=8), score, template_name

    def _detect_player_auxiliary(
        self,
        scene: SceneFeatures,
        vision: dict[str, Any],
        search_roi: tuple[int, int, int, int] | None,
    ) -> tuple[list[Detection], float, list[Detection], float]:
        head_detections, head_score, _head_template_name = find_detections(
            scene,
            self.player_head_templates,
            float(vision.get("player_head_threshold", 0.74)),
            float(vision.get("player_detection_scale", 0.5)),
            max_per_template=8,
            nms_iou=0.35,
            max_detections=8,
            structure_weight=0.35,
            search_roi=search_roi,
        )
        title_detections, title_score, _title_template_name = find_detections(
            scene,
            self.player_title_templates,
            float(vision.get("player_title_threshold", 0.70)),
            float(vision.get("player_detection_scale", 0.5)),
            max_per_template=8,
            nms_iou=0.35,
            max_detections=8,
            structure_weight=0.55,
            search_roi=search_roi,
        )
        return head_detections, head_score, title_detections, title_score

    def _record_player_recognition_result(
        self,
        anchor: PlayerAnchor | None,
        now: float,
        *,
        nameplate_score: float,
        nameplate_count: int,
        best_identity_score: float,
        head_score: float,
        title_score: float,
        global_scan: bool,
        minimap_guard_active: bool,
        marker_unambiguous: bool,
    ) -> PlayerAnchor | None:
        """仅在状态转换或持续丢失时记录定位分数，避免逐帧刷日志。"""
        lost_at = float(getattr(self, "player_recognition_lost_at", 0.0))
        last_log_at = float(getattr(self, "player_recognition_last_log_at", 0.0))
        log = getattr(self, "log", None)
        if anchor is None:
            first_loss = lost_at <= 0.0
            if first_loss:
                lost_at = now
                self.player_recognition_lost_at = now
            if first_loss or now - last_log_at >= 5.0:
                event = "player_recognition_lost" if first_loss else "player_recognition_still_lost"
                if log is not None:
                    log.write(
                        event,
                        lost_seconds=round(max(0.0, now - lost_at), 3),
                        nameplate_score=round(float(nameplate_score), 4),
                        nameplate_count=int(nameplate_count),
                        best_identity_score=round(float(best_identity_score), 4),
                        head_score=round(float(head_score), 4),
                        title_score=round(float(title_score), 4),
                        global_scan=bool(global_scan),
                        minimap_guard_active=bool(minimap_guard_active),
                        minimap_blocked=bool(self.player_track.minimap_stationary_blocked),
                        marker_unambiguous=bool(marker_unambiguous),
                        misses=int(self.player_track.misses),
                    )
                self.player_recognition_last_log_at = now
            return None
        if lost_at > 0.0 and log is not None:
            log.write(
                "player_recognition_reacquired",
                lost_seconds=round(max(0.0, now - lost_at), 3),
                source=anchor.source,
                nameplate_score=round(float(nameplate_score), 4),
                best_identity_score=round(float(best_identity_score), 4),
                head_score=round(float(head_score), 4),
                title_score=round(float(title_score), 4),
            )
        self.player_recognition_lost_at = 0.0
        self.player_recognition_last_log_at = 0.0
        return anchor

    def _track_player(
        self,
        scene: SceneFeatures,
        vision: dict[str, Any],
        now: float,
        marker: tuple[float, float] | None = None,
        *,
        marker_unambiguous: bool = False,
        marker_size: tuple[int, int] | None = None,
    ) -> PlayerAnchor | None:
        """名字确认身份；视觉暂失时，小地图只证明旧视觉位置是否仍可短时沿用。"""
        self.nameplate_visible_this_frame = False
        # 配置热更新会原子替换 tracker；本帧始终使用同一个实例，旧结果不会回写新状态。
        track = self.player_track
        track.begin_frame()
        had_minimap_reference = track.minimap_stationary_reference is not None
        track.clear_minimap_evidence()
        scene_height, scene_width = scene.scene.shape[:2]
        map_width, map_height = marker_size if marker_size is not None else (0, 0)
        minimap_assist_enabled = bool(vision.get("player_minimap_assist_enabled", True))
        minimap_guard_active = (
            minimap_assist_enabled
            and had_minimap_reference
            and track.has_nameplate_identity()
        )
        stationary_anchor = None
        nameplate_score = -1.0
        head_score = -1.0
        title_score = -1.0
        nameplate_detections: list[Detection] = []

        def finish(result: PlayerAnchor | None) -> PlayerAnchor | None:
            identity_scores = [
                float(detection.identity_score)
                for detection in nameplate_detections
                if detection.identity_score is not None
            ]
            return self._record_player_recognition_result(
                result,
                now,
                nameplate_score=nameplate_score,
                nameplate_count=len(nameplate_detections),
                best_identity_score=max(identity_scores, default=-1.0),
                head_score=head_score,
                title_score=title_score,
                global_scan=global_scan,
                minimap_guard_active=minimap_guard_active,
                marker_unambiguous=marker_unambiguous,
            )
        if not minimap_assist_enabled and (
            had_minimap_reference or track.minimap_stationary_blocked
        ):
            # 关闭开关意味着放弃这次遮挡期的证据；稍后重新开启也不能复活旧基准。
            track.invalidate_minimap_assist()
        elif minimap_guard_active:
            # 始终与上次真实视觉命中的固定 marker 比较，不与上一帧滚动比较。
            stationary_anchor = track.minimap_stationary_anchor(
                marker,
                now,
                scene_width,
                scene_height,
                map_width,
                map_height,
                max_seconds=float(vision.get("player_minimap_assist_max_seconds", 0.2)),
                marker_unambiguous=marker_unambiguous,
            )
        player_hold = float(vision.get("player_hold_seconds", 0.8))
        previous_anchor = track.anchor_within_hold(now, player_hold)
        miss_limit = max(1, int(vision.get("player_local_miss_limit", 2)))
        global_scan = previous_anchor is None or track.needs_global_scan(
            now,
            float(vision.get("player_global_verify_interval_seconds", 1.5)),
            miss_limit,
        )
        predicted_point = track.predicted_point(
            now,
            float(vision.get("player_prediction_horizon_seconds", 0.2)),
        )
        search_roi = None
        if not global_scan and predicted_point is not None:
            search_roi = player_tracking_roi(
                predicted_point,
                scene_width,
                scene_height,
                float(vision.get("player_local_roi_width", 0.36)),
                float(vision.get("player_local_roi_up", 0.24)),
                float(vision.get("player_local_roi_down", 0.18)),
            )

        raw_nameplate_detections, nameplate_score, _template_name = self._detect_player_nameplate(
            scene,
            vision,
            search_roi,
        )
        identity_threshold = float(vision.get("player_name_identity_threshold", 0.50))
        nameplate_detections = [
            detection
            for detection in raw_nameplate_detections
            if detection.identity_score is not None and detection.identity_score >= identity_threshold
        ]
        if global_scan:
            track.last_global_at = now
        anchor_args = (
            scene_width,
            scene_height,
            float(vision.get("player_head_feet_offset", 0.07)),
            float(vision.get("player_title_feet_offset", 0.076)),
            float(vision.get("player_anchor_max_jump", 0.18)),
            float(vision.get("player_anchor_agreement", 0.07)),
        )
        if (
            global_scan
            and previous_anchor is None
            and not nameplate_detections
            and track.has_nameplate_identity()
            and track.anchor is not None
            and predicted_point is not None
            and now - track.last_seen_at
            <= max(
                0.0,
                float(vision.get("player_auxiliary_continuation_seconds", 15.0)),
            )
        ):
            # 正常全图搜索仍使用严格阈值。只在已经由姓名板确认过本人、普通 hold
            # 也已超时后，围绕最后预测位置放宽原始模板候选门槛；候选仍必须通过
            # 全分辨率名字字形核验、最大位移约束，以及下游的连续多帧重获确认。
            recovery_roi = player_tracking_roi(
                predicted_point,
                scene_width,
                scene_height,
                float(vision.get("player_local_roi_width", 0.36)),
                float(vision.get("player_local_roi_up", 0.24)),
                float(vision.get("player_local_roi_down", 0.18)),
            )
            recovery_detections, recovery_score, _recovery_template = (
                self._detect_player_nameplate(
                    scene,
                    vision,
                    recovery_roi,
                    threshold=float(
                        vision.get("player_nameplate_recovery_threshold", 0.66)
                    ),
                )
            )
            nameplate_score = max(nameplate_score, recovery_score)
            recovery_anchor_args = (
                scene_width,
                scene_height,
                float(vision.get("player_head_feet_offset", 0.07)),
                float(vision.get("player_title_feet_offset", 0.076)),
                float(vision.get("player_auxiliary_max_jump", 0.06)),
                float(vision.get("player_anchor_agreement", 0.07)),
            )
            nameplate_detections = [
                detection
                for detection in recovery_detections
                if detection.identity_score is not None
                and detection.identity_score >= identity_threshold
                and choose_fused_player_anchor(
                    [("姓名板", [detection])],
                    track.anchor,
                    *recovery_anchor_args,
                    reference_point=predicted_point,
                )
                is not None
            ]
        nameplate_anchor = choose_fused_player_anchor(
            [("姓名板", nameplate_detections)],
            previous_anchor,
            *anchor_args,
            reference_point=predicted_point if previous_anchor is not None else None,
        )
        run_auxiliary = should_run_player_auxiliary_detections(
            previous_anchor,
            nameplate_anchor,
            now,
            track.last_auxiliary_at,
            float(
                vision.get(
                    "player_auxiliary_interval_seconds",
                    DEFAULT_PLAYER_AUXILIARY_INTERVAL_SECONDS,
                )
            ),
        ) or global_scan
        head_detections: list[Detection] = []
        title_detections: list[Detection] = []
        if run_auxiliary:
            head_detections, head_score, title_detections, title_score = self._detect_player_auxiliary(
                scene,
                vision,
                search_roi,
            )
            track.last_auxiliary_at = now
        anchor: PlayerAnchor | None = None
        identity_confirmed = False
        if nameplate_anchor is not None and previous_anchor is not None:
            anchor = nameplate_anchor
            identity_confirmed = True
        elif nameplate_detections:
            # 通过了名字验证但离旧位置很远：视为传送/换层，连续多帧确认后才能换锁。
            ordered = sorted(
                nameplate_detections,
                key=lambda detection: float(detection.identity_score or 0.0),
                reverse=True,
            )
            margin = float(vision.get("player_name_identity_margin", 0.08))
            unambiguous = len(ordered) == 1 or (
                float(ordered[0].identity_score or 0.0)
                - float(ordered[1].identity_score or 0.0)
                >= margin
            )
            if unambiguous:
                candidate = choose_fused_player_anchor(
                    [("姓名板", [ordered[0]])],
                    None,
                    *anchor_args,
                )
                if candidate is not None:
                    anchor = track.consider_reacquisition(
                        candidate,
                        int(vision.get("player_reacquire_confirm_frames", 2)),
                        float(vision.get("player_anchor_agreement", 0.07))
                        * max(scene_width, scene_height),
                        kind="nameplate",
                    )
                    identity_confirmed = anchor is not None
        elif (
            previous_anchor is not None
            and not nameplate_detections
            and track.has_confirmed_identity()
        ):
            # 姓名板原始候选可能只是遮挡物造成的误匹配；只有通过名字身份校验的候选
            # 才能阻止辅助模板补位。头部和称号不能建立身份，但在姓名板确认过本人后，
            # 可以围绕连续预测位置续跟踪；即使本帧是周期全图复核，也仍受小位移约束。
            auxiliary_args = (
                scene_width,
                scene_height,
                float(vision.get("player_head_feet_offset", 0.07)),
                float(vision.get("player_title_feet_offset", 0.076)),
                float(vision.get("player_auxiliary_max_jump", 0.06)),
                float(vision.get("player_anchor_agreement", 0.07)),
            )
            anchor = choose_fused_player_anchor(
                [("头部", head_detections), ("称号勋章", title_detections)],
                previous_anchor,
                *auxiliary_args,
                reference_point=predicted_point,
            )
        elif (
            global_scan
            and not nameplate_detections
            and track.has_nameplate_identity()
            and track.anchor is not None
            and now - track.last_seen_at
            <= max(
                0.0,
                float(vision.get("player_auxiliary_continuation_seconds", 15.0)),
            )
        ):
            # 已由本人姓名板建立身份后，攻击、Buff 或短步动画可能让姓名板/头部同时
            # 低分超过普通 hold。此时不能退回“首次认人”的 0.90 门槛：只允许普通
            # 辅助模板在最后可靠位置附近连续多帧确认，从而恢复同一人的空间续跟踪。
            auxiliary_args = (
                scene_width,
                scene_height,
                float(vision.get("player_head_feet_offset", 0.07)),
                float(vision.get("player_title_feet_offset", 0.076)),
                float(vision.get("player_auxiliary_max_jump", 0.06)),
                float(vision.get("player_anchor_agreement", 0.07)),
            )
            candidate = choose_fused_player_anchor(
                [("头部", head_detections), ("称号勋章", title_detections)],
                track.anchor,
                *auxiliary_args,
                reference_point=predicted_point,
            )
            if candidate is not None:
                anchor = track.consider_reacquisition(
                    candidate,
                    int(vision.get("player_auxiliary_reacquire_confirm_frames", 3)),
                    float(vision.get("player_anchor_agreement", 0.07))
                    * max(scene_width, scene_height),
                    kind="known_identity_aux",
                )
        elif global_scan and not nameplate_detections:
            # 启动时姓名板可能已被遮挡。普通辅助命中仍不能认人；只有高置信头部/称号
            # 连续多帧落到同一位置，才允许建立受限的视觉身份。
            auxiliary_identity_threshold = float(
                vision.get("player_auxiliary_identity_threshold", 0.90)
            )
            strong_head = [
                detection
                for detection in head_detections
                if detection.score >= auxiliary_identity_threshold
            ]
            strong_title = [
                detection
                for detection in title_detections
                if detection.score >= auxiliary_identity_threshold
            ]
            candidate = choose_fused_player_anchor(
                [("头部", strong_head), ("称号勋章", strong_title)],
                None,
                *anchor_args,
            )
            if candidate is not None:
                anchor = track.consider_reacquisition(
                    candidate,
                    int(vision.get("player_auxiliary_reacquire_confirm_frames", 3)),
                    float(vision.get("player_anchor_agreement", 0.07))
                    * max(scene_width, scene_height),
                    kind="startup_aux",
                )
                identity_confirmed = anchor is not None

        if anchor is None and search_roi is not None and track.misses + 1 >= miss_limit:
            # 连续局部丢失后只允许通过本人名字做全图重定位，辅助模板不得建立新身份。
            raw_nameplate_detections, nameplate_score, _template_name = self._detect_player_nameplate(
                scene,
                vision,
                None,
            )
            nameplate_detections = [
                detection
                for detection in raw_nameplate_detections
                if detection.identity_score is not None and detection.identity_score >= identity_threshold
            ]
            track.last_global_at = now
            if nameplate_detections:
                ordered = sorted(
                    nameplate_detections,
                    key=lambda detection: float(detection.identity_score or 0.0),
                    reverse=True,
                )
                margin = float(vision.get("player_name_identity_margin", 0.08))
                unambiguous = len(ordered) == 1 or (
                    float(ordered[0].identity_score or 0.0)
                    - float(ordered[1].identity_score or 0.0)
                    >= margin
                )
                nearby_candidate = choose_fused_player_anchor(
                    [("姓名板", [ordered[0]])] if unambiguous else [],
                    previous_anchor,
                    *anchor_args,
                )
                if nearby_candidate is not None:
                    anchor = nearby_candidate
                    identity_confirmed = True
                elif unambiguous:
                    candidate = choose_fused_player_anchor(
                        [("姓名板", [ordered[0]])],
                        None,
                        *anchor_args,
                    )
                    if candidate is not None:
                        anchor = track.consider_reacquisition(
                            candidate,
                            int(vision.get("player_reacquire_confirm_frames", 2)),
                            float(vision.get("player_anchor_agreement", 0.07))
                            * max(scene_width, scene_height),
                            kind="nameplate",
                        )
                        identity_confirmed = anchor is not None

        if nameplate_detections:
            self.nameplate_visible_this_frame = True
            self.last_nameplate_seen_at = now

        if anchor is not None:
            # 当前帧已有真实视觉锚点，静止证据只保留给纯视觉丢失分支。
            track.clear_minimap_evidence()
            track.record(
                anchor,
                now,
                velocity_alpha=float(vision.get("player_velocity_alpha", 0.35)),
                max_displacement=float(vision.get("player_anchor_max_jump", 0.18))
                * max(scene_width, scene_height),
                identity_confirmed=identity_confirmed,
            )
            if minimap_assist_enabled:
                track.record_minimap_stationary_reference(
                    anchor,
                    marker,
                    now,
                    scene_width,
                    scene_height,
                    map_width,
                    map_height,
                    head_continuous=previous_anchor is not None,
                    marker_unambiguous=marker_unambiguous,
                )
            return finish(anchor)
        track.mark_miss()
        if minimap_guard_active:
            if stationary_anchor is not None:
                return finish(stationary_anchor)
            # 位移、歧义、丢失、尺寸变化或过期时立即隐藏旧锚点，不能再由通用 hold 绕过。
            return finish(None)
        if minimap_assist_enabled and track.minimap_stationary_blocked:
            # 失效门禁会一直保持到真实视觉重新命中；不能在下一帧因基准已清空而复活旧 hold。
            return finish(None)
        if track.misses >= miss_limit:
            return finish(None)
        return finish(track.anchor_within_hold(now, player_hold))

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
        self.performance.update_target_fps(fps)
        vision = self.config["vision"]
        print(f"游戏窗口：{window.title}（{window.width}×{window.height}）")
        print(f"已载入怪物模板：{len(self.templates)} 个")
        print(f"已载入怪物过滤项：{len(self.monster_filter_templates)} 个")
        print(f"当前怪物分类：{self.active_monster_category or '未分类'}")
        print(
            "已载入玩家定位模板："
            f"姓名板 {len(self.player_templates)} / 头部 {len(self.player_head_templates)} / "
            f"称号勋章 {len(self.player_title_templates)}"
        )
        print("按键投递：" + {
            "window_message": "纯窗口消息（移动采用按下/抬起脉冲，不发送全局按键）。",
            "background": "后台扫描码（失焦仍 SendInput，并补发窗口消息）。",
            "foreground": "前台 SendInput（游戏必须在前台）。",
        }[self.delivery])
        if self.input_authorized and not self.integrity_ok:
            print("权限不足：助手权限低于游戏，输入会被 Windows 阻止。")
        print("F7 显隐 Debug 框，F8 启动或暂停，F9 / Ctrl+Shift+Q 立即退出。按键输入：" + ("已允许。" if self.input_authorized else "未授权。"))
        self.log.write(
            "session_start",
            input_authorized=self.input_authorized,
            active_monster_category=self.active_monster_category,
            delivery=self.delivery,
            input_hwnd=self.keyboard.hwnd,
            templates=len(self.templates),
            monster_filter_templates=len(self.monster_filter_templates),
            player_templates=len(self.player_templates),
            player_head_templates=len(self.player_head_templates),
            player_title_templates=len(self.player_title_templates),
        )
        hotkey_thread = threading.Thread(target=self.monitor_hotkeys, name="MapleHotkeys", daemon=True)
        hotkey_thread.start()
        capture_failures = 0
        background_capture = BackgroundCapture()
        try:
            with mss.MSS() as sct:
                while True:
                    loop_start = time.monotonic()
                    frame_started_ns = time.perf_counter_ns()
                    stages_ns: dict[str, int] = {}
                    if self.f9_requested.is_set():
                        self.f9_requested.clear()
                        break
                    with self.action_lock:
                        if self.f8_requested.is_set():
                            self.f8_requested.clear()
                            self.toggle(window)
                    with self.action_lock:
                        potion_enabled_requested = self.potion_enabled_requested
                        self.potion_enabled_requested = None
                        if potion_enabled_requested is not None:
                            self._set_auto_potion(window, potion_enabled_requested)
                    if self.f7_requested.is_set():
                        self.f7_requested.clear()
                        self.toggle_calibration_overlay()
                    if self.vision_suspended.is_set():
                        time.sleep(0.05)
                        continue
                    vision = self.config["vision"]
                    capture_started_ns = time.perf_counter_ns()
                    try:
                        if self.delivery == "window_message":
                            frame = background_capture.capture(window)
                        else:
                            background_capture.close()
                            frame = capture_client(sct, window)
                        capture_failures = 0
                    except (mss.exception.ScreenShotError, BackgroundCaptureError) as exc:
                        capture_failures += 1
                        self.performance.record_capture_failure(exc)
                        if self.auto_potion.enabled:
                            self.auto_potion.set_unavailable("画面不可用")
                        if capture_failures == 1 or capture_failures % 30 == 0:
                            self.log.write("capture_retry", failures=capture_failures, error=str(exc))
                            self.notify(f"截图不可用：{exc}", 8.0)
                        if self.armed:
                            self.disarm("游戏画面暂时无法截取")
                        time.sleep(0.1)
                        continue
                    stages_ns["capture"] = time.perf_counter_ns() - capture_started_ns
                    preprocess_started_ns = time.perf_counter_ns()
                    hp_img, hp_rect = crop(frame, self.config["regions"]["hp_bar"])
                    mp_img, mp_rect = crop(frame, self.config["regions"]["mp_bar"])
                    minimap_img, minimap_rect = crop(frame, self.config["regions"]["minimap"])
                    combat_img, combat_rect = crop(frame, self.config["regions"]["combat"])
                    hp = bar_fill(hp_img, vision["hp_hsv_ranges"])
                    mp = bar_fill(mp_img, vision["mp_hsv_ranges"])
                    self.ui_hp = hp
                    self.ui_mp = mp
                    marker_observation, _mask = player_marker_observation(
                        minimap_img,
                        vision["player_hsv_ranges"],
                        int(vision["player_blob_min_area"]),
                        int(vision["player_blob_max_area"]),
                        self.marker,
                    )
                    marker = marker_observation.point
                    self.live_marker_unambiguous = marker_observation.unambiguous
                    self.live_minimap_width = minimap_img.shape[1]
                    now = time.monotonic()
                    if marker is not None:
                        self.marker = marker
                        self.marker_last_seen = now
                    # 暂停时的全局喝药只需要血蓝条；保留截图循环，但跳过昂贵的战斗模板匹配。
                    lightweight_potion_only = bool(
                        not self.armed and self.auto_potion.enabled
                    )
                    # 四路模板检测共用同一份场景特征，避免每帧重复缩放、颜色转换和 Canny。
                    scene = SceneFeatures(combat_img)
                    stages_ns["preprocess"] = time.perf_counter_ns() - preprocess_started_ns
                    monster_started_ns = time.perf_counter_ns()
                    if lightweight_potion_only:
                        detected_monsters, detected_score, detected_name = [], -1.0, None
                    else:
                        detected_monsters, detected_score, detected_name = find_detections(
                            scene,
                            self.templates,
                            float(vision["monster_template_threshold"]),
                            float(vision.get("monster_detection_scale", 1.0)),
                            structure_weight=float(vision.get("monster_structure_weight", 0.15)),
                        )
                    raw_monster_count = len(detected_monsters)
                    hold_seconds = float(vision.get("monster_hold_seconds", 0.0))
                    held_monsters = (
                        self.last_detections
                        if not lightweight_potion_only and now - self.last_detection_at <= hold_seconds
                        else []
                    )
                    filter_sources = [*detected_monsters, *held_monsters]
                    if not lightweight_potion_only and self.monster_filter_templates and filter_sources:
                        active_categories = {
                            monster_template_category(detection.name).casefold()
                            for detection in filter_sources
                        }
                        filter_detections: list[Detection] = []
                        for category in active_categories:
                            category_templates = [
                                template
                                for template in self.monster_filter_templates
                                if monster_template_category(template.name).casefold() == category
                            ]
                            if not category_templates:
                                continue
                            category_filters, _filter_score, _filter_name = find_detections(
                                scene,
                                category_templates,
                                float(vision.get("monster_filter_threshold", 0.84)),
                                float(vision.get("monster_detection_scale", 1.0)),
                                max_per_template=16,
                                nms_iou=0.38,
                                max_detections=32,
                            )
                            filter_detections.extend(category_filters)
                        if filter_detections:
                            overlap = float(vision.get("monster_filter_overlap", 0.5))
                            detected_monsters = suppress_monster_detections(
                                detected_monsters,
                                filter_detections,
                                overlap,
                            )
                            held_monsters = suppress_monster_detections(
                                held_monsters,
                                filter_detections,
                                overlap,
                            )
                            self.last_detections = held_monsters
                    if detected_monsters:
                        detected_score = detected_monsters[0].score
                        detected_name = detected_monsters[0].name
                    elif raw_monster_count:
                        # 候选已被过滤项明确排除，不再作为“未确认怪物”显示。
                        detected_score = -1.0
                        detected_name = None
                    if detected_monsters:
                        self.last_detections = detected_monsters
                        self.last_detection_box = detected_monsters[0].box
                        self.last_detection_score = detected_score
                        self.last_detection_name = detected_name
                        self.last_detection_at = now
                    if not detected_monsters and held_monsters:
                        monsters = held_monsters
                    else:
                        monsters = detected_monsters

                    if not lightweight_potion_only:
                        stages_ns["monster"] = time.perf_counter_ns() - monster_started_ns
                    player_started_ns = time.perf_counter_ns()
                    active_player_anchor = (
                        None
                        if lightweight_potion_only
                        else self._track_player(
                            scene,
                            vision,
                            now,
                            marker,
                            marker_unambiguous=marker_observation.unambiguous,
                            marker_size=(minimap_img.shape[1], minimap_img.shape[0]),
                        )
                    )
                    player_box = active_player_anchor.box if active_player_anchor is not None else None
                    player_source = active_player_anchor.source if active_player_anchor is not None else ""
                    active_player_score = active_player_anchor.score if active_player_anchor is not None else -1.0

                    player_raw_box = active_player_anchor.raw_box if active_player_anchor is not None else None
                    instant_attack_anchor = (
                        player_attack_anchor(player_box, player_raw_box)
                        if player_box is not None
                        else None
                    )
                    self.last_attack_anchor = smooth_player_attack_anchor(
                        self.last_attack_anchor,
                        instant_attack_anchor,
                        float(vision.get("player_anchor_smoothing_alpha", 0.25)),
                        float(vision.get("player_anchor_smoothing_snap", 0.08))
                        * max(combat_img.shape[1], combat_img.shape[0]),
                    )
                    if not lightweight_potion_only:
                        stages_ns["player"] = time.perf_counter_ns() - player_started_ns
                    targeting_started_ns = time.perf_counter_ns()
                    facing = self.direction or "right"
                    target_selection = self.strategy.select_targets(
                        TargetSelectionContext(
                            detections=monsters,
                            player_box=player_box,
                            player_raw_box=player_raw_box,
                            player_anchor=self.last_attack_anchor,
                            scene_width=combat_img.shape[1],
                            scene_height=combat_img.shape[0],
                            facing=facing,
                            target_area=self.config["targeting"]["box"],
                            settings=strategy_settings(self.config),
                            previous_attack_skill=getattr(
                                self,
                                "last_strategy_attack_skill",
                                None,
                            ),
                        )
                    )
                    target = target_selection.target
                    chase_target = target_selection.chase_target
                    has_strategy_candidates = (
                        bool(monsters)
                        if target_selection.eligible_candidate_count is None
                        else target_selection.eligible_candidate_count > 0
                    )
                    attack_box = (
                        self.config["targeting"]["box"]
                        if target_selection.uses_common_target_area
                        else None
                    )
                    box = target.box if target is not None else None
                    chase_box = chase_target.box if chase_target is not None else None
                    selected_target = target if target is not None else chase_target
                    score = selected_target.score if selected_target is not None else detected_score
                    target_distance_px = None
                    target_direction = ""
                    close_overlap_ratio = None
                    close_overlap_threshold = None
                    if selected_target is not None and player_box is not None:
                        target_center = selected_target.box[0] + selected_target.box[2] / 2.0
                        player_center = player_box[0] + player_box[2] / 2.0
                        target_distance_px = int(round(abs(target_center - player_center)))
                        target_direction = "左" if target_center < player_center else "右"
                        settings = strategy_settings(self.config)
                        if "close_overlap_threshold" in settings:
                            close_overlap_ratio = horizontal_overlap_ratio(player_box, selected_target.box)
                            close_overlap_threshold = float(settings["close_overlap_threshold"])
                    stages_ns["targeting"] = time.perf_counter_ns() - targeting_started_ns
                    action_started_ns = time.perf_counter_ns()
                    self.act(
                        window,
                        hp,
                        mp,
                        marker,
                        player_box,
                        box,
                        chase_box,
                        combat_img.shape[1],
                        has_strategy_candidates,
                        now,
                        combat_height=combat_img.shape[0],
                        eligible_detections=tuple(target_selection.eligible_detections or ()),
                    )
                    stages_ns["action"] = time.perf_counter_ns() - action_started_ns
                    hud_started_ns = time.perf_counter_ns()
                    current_window = client_window(window.hwnd, window.title)
                    monster_box = None
                    chase_screen_box = None
                    monster_boxes: list[tuple[int, int, int, int]] = []
                    eligible_monster_boxes: list[tuple[int, int, int, int]] | None = None
                    player_screen_box = None
                    attack_range_box = None
                    strategy_area_boxes: list[dict[str, Any]] = []
                    current_player_anchor: tuple[float, float] | None = None
                    close_overlap_span = None
                    marker_screen = None
                    if marker is not None:
                        mx = minimap_rect[0] + int(marker[0] * minimap_rect[2])
                        my = minimap_rect[1] + int(marker[1] * minimap_rect[3])
                        marker_screen = (mx, my)
                    recognition = self.config["recognition"]
                    for detection in monsters:
                        dx, dy, dw, dh = detection.box
                        monster_boxes.append((combat_rect[0] + dx, combat_rect[1] + dy, dw, dh))
                    if target_selection.eligible_detections is not None:
                        eligible_monster_boxes = []
                        for detection in target_selection.eligible_detections:
                            dx, dy, dw, dh = detection.box
                            eligible_monster_boxes.append(
                                (combat_rect[0] + dx, combat_rect[1] + dy, dw, dh)
                            )
                    if box is not None:
                        bx = combat_rect[0] + box[0]
                        by = combat_rect[1] + box[1]
                        monster_box = (bx, by, box[2], box[3])
                        if close_overlap_ratio is not None and player_box is not None:
                            overlap_left = max(player_box[0], box[0])
                            overlap_right = min(player_box[0] + player_box[2], box[0] + box[2])
                            if overlap_right > overlap_left:
                                close_overlap_span = (
                                    combat_rect[0] + overlap_left,
                                    combat_rect[1] + player_box[1],
                                    overlap_right - overlap_left,
                                )
                    if chase_box is not None:
                        cx = combat_rect[0] + chase_box[0]
                        cy = combat_rect[1] + chase_box[1]
                        chase_screen_box = (cx, cy, chase_box[2], chase_box[3])
                    if player_box is not None:
                        raw_player_box = active_player_anchor.raw_box if active_player_anchor is not None else player_box
                        rx, ry, rw, rh = raw_player_box
                        player_screen_box = (combat_rect[0] + rx, combat_rect[1] + ry, rw, rh)
                        center_x, center_y = self.last_attack_anchor or player_attack_anchor(
                            player_box,
                            raw_player_box,
                        )
                        current_player_anchor = (center_x, center_y)
                        if attack_box is not None:
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
                    for field in self.strategy.capture_fields:
                        settings = strategy_settings(self.config)
                        if field.enable_setting and not bool(settings.get(field.enable_setting)):
                            continue
                        if field.settings_path:
                            raw_areas = settings.get(field.settings_path, [])
                            areas = raw_areas if isinstance(raw_areas, list) else []
                        else:
                            if not recognition.get(f"{field.recognition_key}_captured"):
                                continue
                            raw_area = recognition.get(field.recognition_key)
                            areas = [raw_area] if isinstance(raw_area, dict) else []
                        for index, area in enumerate(areas, start=1):
                            if not isinstance(area, dict) or not bool(area.get("enabled", True)):
                                continue
                            if area.get("space") == PLAYER_RELATIVE_REGION_SPACE:
                                if current_player_anchor is None:
                                    continue
                                left, top, right, bottom = player_relative_region_rect(
                                    current_player_anchor,
                                    combat_img.shape[1],
                                    combat_img.shape[0],
                                    area,
                                    clip=True,
                                )
                                ax = int(round(left))
                                ay = int(round(top))
                                aw = int(round(right - left))
                                ah = int(round(bottom - top))
                                if aw <= 0 or ah <= 0:
                                    continue
                                area_origin_x = combat_rect[0]
                                area_origin_y = combat_rect[1]
                            elif area.get("space") == MINIMAP_REGION_SPACE:
                                ax, ay, aw, ah = roi_pixels(
                                    (minimap_rect[3], minimap_rect[2], 3),
                                    area,
                                )
                                area_origin_x = minimap_rect[0]
                                area_origin_y = minimap_rect[1]
                            else:
                                ax, ay, aw, ah = roi_pixels(combat_img.shape, area)
                                area_origin_x = combat_rect[0]
                                area_origin_y = combat_rect[1]
                            strategy_area_boxes.append(
                                {
                                    "box": (area_origin_x + ax, area_origin_y + ay, aw, ah),
                                    "label": str(area.get("name", "")).strip()
                                    or (field.debug_label if len(areas) == 1 else f"{field.debug_label} {index}"),
                                    "key": field.recognition_key,
                                }
                            )
                    state_label = STATE_LABELS.get(self.state, self.state)
                    if player_source == "小地图静止确认":
                        state_label = "小地图显示角色未移动（沿用上次视觉位置）"
                    if not self.armed and self.auto_potion.enabled:
                        potion_state = self.auto_potion.display_state(now)
                        banner = (
                            f"自动喝药｜{potion_state}｜暂停时仅游戏前台发药键"
                            "｜F8 启动挂机｜F9 / Ctrl+Shift+Q 退出"
                        )
                    elif not self.player_templates:
                        banner = "缺少玩家姓名板模板｜请在控制面板采集姓名板"
                    elif self.input_authorized:
                        if not self.integrity_ok:
                            banner = "输入权限不足｜请在控制面板点击「启动挂机」，并在 UAC 中选择“是”"
                        elif self.background_input:
                            banner = (
                                f"{'运行中' if self.armed else '输入待命'}｜后台按键｜{state_label}"
                                "｜F7 Debug 框｜F8 启动/暂停｜F9 / Ctrl+Shift+Q 退出"
                            )
                        else:
                            banner = f"{'运行中' if self.armed else '输入待命'}｜{state_label}｜F7 Debug 框｜F8 启动/暂停｜F9 / Ctrl+Shift+Q 退出"
                    else:
                        banner = "按键未授权｜请从 Start.bat 启动｜F7 Debug 框｜F9 / Ctrl+Shift+Q 退出"
                    overlay.update(
                        {
                            "background_hidden": self.delivery == "window_message"
                            and int(user32.GetForegroundWindow()) != window.hwnd,
                            "left": current_window.left,
                            "top": current_window.top,
                            "width": current_window.width,
                            "height": current_window.height,
                            "armed": self.armed,
                            "input_authorized": self.input_authorized,
                            "banner": banner,
                            "notice": self.notice if now <= self.notice_until else "",
                            "show_calibration": self.calibration_overlay_visible,
                            "debug_hidden_items": tuple(self.calibration_overlay_hidden_items),
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
                            "attack_range_label": "有效索敌区",
                            "strategy_area_boxes": strategy_area_boxes,
                            "close_overlap_ratio": close_overlap_ratio,
                            "close_overlap_threshold": close_overlap_threshold,
                            "close_overlap_span": close_overlap_span,
                            "monster_boxes": monster_boxes,
                            "eligible_monster_boxes": eligible_monster_boxes,
                            "monster_box": monster_box,
                            "chase_box": chase_screen_box,
                            "monster_score": score,
                            "target_distance_px": target_distance_px,
                            "target_direction": target_direction,
                            "monster_threshold": float(vision["monster_template_threshold"]),
                        }
                    )
                    stages_ns["hud"] = time.perf_counter_ns() - hud_started_ns
                    frame_finished_ns = time.perf_counter_ns()
                    self.performance.record_frame(
                        started_ns=frame_started_ns,
                        finished_ns=frame_finished_ns,
                        stages_ns=stages_ns,
                        mode="potion_only" if lightweight_potion_only else "full",
                        target_fps=fps,
                    )
                    elapsed = time.monotonic() - loop_start
                    if elapsed < frame_period:
                        time.sleep(frame_period - elapsed)
        except BaseException as exc:
            self.performance.mark_error(exc)
            raise
        finally:
            background_capture.close()
            self.performance.stop()
            self.stop_hotkey_monitor()
            hotkey_thread.join(timeout=1.0)
            self.auto_potion.set_enabled(False)
            self.disarm("程序退出")
            overlay.close()
            self.log.write("session_end")
            print(f"运行日志：{self.log.path}")
