from __future__ import annotations

import ctypes
from contextlib import nullcontext
from pathlib import Path
import threading
import time
import tkinter as tk
from tkinter import font as tkfont, messagebox, simpledialog, ttk
from typing import Any, Callable

from PIL import Image, ImageTk

from mbv.app_icon import configure_app_identity, install_app_icon
from mbv.bot import STATE_LABELS, BowmanBot
from mbv.calibrate import (
    calibrate,
    capture_combat_region,
    capture_platform_center,
    capture_minimap_point,
    capture_player_marker,
    capture_status_region,
    capture_strategy_area,
    capture_target_range,
    capture_key_name,
    capture_monster_filter,
    capture_player_aux_template,
    capture_player_template,
    capture_recognition_region,
    capture_strategy_region,
    capture_template,
)
from mbv.config import load_config, save_config, template_counts
from mbv.input import input_delivery, vk_for
from mbv.overlay import RuntimeOverlay, _exclude_from_capture, _top_level_hwnd, prevent_window_activate
from mbv.performance import format_performance_summary
from mbv.panel_theme import (
    ACCENT, ACCENT_HOVER, ACCENT_SOFT, ARMED, BG, BORDER, BUTTON_ACTIVE, BUTTON_BG,
    DANGER_HOVER,
    ENTRY_BG, FG, FONT, FONT_SECTION, FONT_SMALL, FONT_TITLE, MUTED,
    PANEL, SUCCESS, WARNING, install_theme, style_button,
)
from mbv.panel_widgets import RoundedButton, RoundedCard
from mbv.strategies import active_strategy, get_strategy, list_strategies
from mbv.strategies.base import StrategyCaptureField
from mbv.template_store import (
    UNCATEGORIZED_LABEL,
    create_monster_category,
    list_monster_categories,
    list_template_items,
    rename_monster_category,
    trash_monster_category,
    trash_template,
)
from mbv.window import (
    WindowTarget,
    resolve_window_target,
    selected_window,
    validate_window_target,
    window_candidates,
)

DELIVERY_LABELS = {
    "foreground": "前台按键",
    "background": "兼容后台（全局按键）",
    "window_message": "独立后台（实验）",
    "hybrid": "混合后台（移动后留前台）",
}
TEMPLATE_GROUPS = (
    ("monster", "怪物模板"),
    ("filter", "过滤项"),
    ("player", "姓名板"),
    ("head", "头部"),
    ("title", "称号勋章"),
)
TEMPLATE_GROUP_LABELS = dict(TEMPLATE_GROUPS)


def template_preview_image(path: Path, max_size: tuple[int, int] = (280, 260)) -> Image.Image:
    """载入模板并合成到深色背景，便于看清透明边缘。"""
    with Image.open(path) as source:
        image = source.convert("RGBA")
    image.thumbnail(max_size, Image.Resampling.LANCZOS)
    background = Image.new("RGBA", image.size, (17, 17, 17, 255))
    background.alpha_composite(image)
    return background.convert("RGB")


def adjusted_numeric_text(
    raw: str,
    delta: float,
    minimum: float | None = None,
    maximum: float | None = None,
) -> str:
    value = float(raw.strip()) + float(delta)
    if minimum is not None:
        value = max(float(minimum), value)
    if maximum is not None:
        value = min(float(maximum), value)
    return f"{value:.6f}".rstrip("0").rstrip(".")


def is_elevated() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def disable_combobox_mousewheel(combo: ttk.Combobox) -> None:
    """防止滚动面板时意外改变只读下拉框。"""
    for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
        combo.bind(sequence, lambda _event: "break", add="+")


class ControlPanel:
    def __init__(self, config_path: Path, enable_input: bool) -> None:
        self.config_path = config_path
        config = load_config(config_path)
        self.profile_label = "NewMaple" if config["profile"] == "newmaple" else "怀旧服"
        configure_app_identity()
        self.root = tk.Tk()
        install_app_icon(self.root)
        install_theme(self.root)
        self.root.title(f"MapleBowmanVision — {self.profile_label}")
        self.root.configure(bg=BG)
        self.root.minsize(560, 720)
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        width = min(720, max(560, screen_w - 80))
        height = min(900, max(720, screen_h - 100))
        left = max(20, screen_w - width - 20)
        self.root.geometry(f"{width}x{height}+{left}+20")
        self.root.wm_attributes("-topmost", False)
        self.root.update_idletasks()
        panel_hwnd = _top_level_hwnd(self.root)
        _exclude_from_capture(panel_hwnd)
        prevent_window_activate(panel_hwnd)

        self.bot = BowmanBot(config, input_authorized=enable_input)
        self._input_authorized = enable_input
        self.overlay = RuntimeOverlay(self.root)
        self.overlay.set_exit_handler(self.quit)
        self.worker_errors: list[BaseException] = []
        self.worker: threading.Thread | None = None
        self._selected_target: WindowTarget | None = None
        self._pending_target: WindowTarget | None = None
        self._stopping_session = False
        self._closing = False
        self._window_lookup: dict[str, WindowTarget] = {}
        self._connection_message = "先选择作用窗口，再点击连接；原配置已作为默认值。"
        self.busy = False
        self._loading_settings = True
        self._autosave_after_id: str | None = None
        self._autosave_reconfigure = False
        self._performance_after_id: str | None = None
        performance_monitor = config["performance_monitor"]
        self._performance_interval_ms = int(
            round(float(performance_monitor["refresh_interval_seconds"]) * 1000)
        )
        self.status = tk.StringVar(value="等待选择窗口")
        self.run_badge = tk.StringVar(value="未连接")
        self.run_metrics = tk.StringVar(value="连接窗口后显示运行状态")
        self.run_notice = tk.StringVar(value="")
        self.window_choice = tk.StringVar(value="")
        self.window_hint = tk.StringVar(value=self._connection_message)
        self.counts = tk.StringVar(value="")
        self.performance_visible = tk.BooleanVar(value=bool(performance_monitor["visible"]))
        self.performance_text = tk.StringVar(value="等待性能数据…")
        self.monster_category = tk.StringVar(value=UNCATEGORIZED_LABEL)
        self.monster_category_summary = tk.StringVar(value="")
        self._monster_category_lookup: dict[str, str] = {UNCATEGORIZED_LABEL: ""}
        self.debug_boxes = tk.BooleanVar(value=self.bot.calibration_overlay_visible)
        self.auto_potion_enabled = tk.BooleanVar(value=False)
        self.standalone_potion = self.auto_potion_enabled
        self.verification_alert_enabled = tk.BooleanVar(value=bool(config["verification_alert"]["enabled"]))
        self.buff_enabled: dict[str, tk.BooleanVar] = {
            slot: tk.BooleanVar(value=bool(config["buffs"][slot]["enabled"]))
            for slot in ("buff_1", "buff_2", "buff_3")
        }
        self._buff_toggle_buttons: dict[str, tk.Checkbutton] = {}
        self.hp_threshold_percent = tk.IntVar(value=int(round(float(config["behavior"]["hp_threshold"]) * 100)))
        self.mp_threshold_percent = tk.IntVar(value=int(round(float(config["behavior"]["mp_threshold"]) * 100)))
        self.delivery = tk.StringVar(value=DELIVERY_LABELS[input_delivery(config)])
        self.topmost_while_armed = tk.BooleanVar(
            value=bool(config.get("window", {}).get("topmost_while_armed", True))
        )
        self.fallback_patrol = tk.BooleanVar(value=bool(config["behavior"].get("fallback_patrol")))
        self.pickup_lost = tk.BooleanVar(value=bool(config["behavior"].get("pickup_after_target_lost")))
        self.player_lost_recovery = tk.BooleanVar(
            value=bool(config["behavior"].get("player_lost_recovery_enabled", True))
        )
        self.minimap_assist = tk.BooleanVar(
            value=bool(config["vision"].get("player_minimap_assist_enabled", True))
        )
        selected_strategy = active_strategy(config)
        self.profession_name = tk.StringVar(value=selected_strategy.profession)
        self.strategy_name = tk.StringVar(value=selected_strategy.display_name)
        self.strategy_description = tk.StringVar(value=selected_strategy.description)
        self._strategy_lookup = {item.display_name: item.key for item in list_strategies()}
        self._profession_strategies: dict[str, tuple[Any, ...]] = {}
        for item in list_strategies():
            self._profession_strategies.setdefault(item.profession, tuple())
            self._profession_strategies[item.profession] += (item,)
        self._entries: dict[str, tk.Entry] = {}
        self._targeting_entries: dict[str, tk.Entry] = {}
        self._strategy_entries: dict[str, tk.Entry] = {}
        self._strategy_toggles: dict[str, tk.BooleanVar] = {}
        self._strategy_choices: dict[str, tuple[tk.StringVar, dict[str, str]]] = {}
        self._capture_status_vars: dict[str, tk.StringVar] = {}
        self._capture_status_labels: dict[str, tk.Label] = {}
        self._capture_buttons: dict[str, tk.Button] = {}
        self._debug_item_buttons: dict[str, tuple[tk.Button, str]] = {}
        self._build()
        self._refresh_counts()
        self._load_entries(config)
        self._loading_settings = False
        self.root.after_idle(lambda: self._capture_canvas.yview_moveto(0))

        self._refresh_windows()
        self.overlay.hide()
        self.overlay.start_polling()
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        self._schedule_performance_refresh(delay_ms=0)
        self.root.after(250, self._tick)

    def _run_bot(self) -> None:
        try:
            self.bot.run(self.overlay, target=self._selected_target, close_overlay_on_exit=False)
        except BaseException as exc:
            self.worker_errors.append(exc)

    def _worker_failed(self, exc: BaseException) -> None:
        self._connection_message = f"连接已停止：{exc}。请重新选择窗口。"
        self.window_hint.set(self._connection_message)
        self.status.set("等待重新选择窗口")

    def _build_window_selector(self, parent: tk.Misc) -> None:
        card = RoundedCard(parent, padding=6)
        card.pack(fill="x", padx=8, pady=(0, 8))
        frame = card.body
        heading = tk.Frame(frame, bg=PANEL)
        heading.pack(fill="x", padx=6, pady=(3, 5))
        tk.Label(heading, text="作用窗口", bg=PANEL, fg=FG, font=FONT_SECTION).pack(side="left")
        row = tk.Frame(frame, bg=PANEL)
        row.pack(fill="x", padx=12)
        self.window_combo = ttk.Combobox(row, textvariable=self.window_choice, state="readonly", font=FONT)
        self.window_combo.pack(side="left", fill="x", expand=True)
        disable_combobox_mousewheel(self.window_combo)
        self.window_refresh_button = self._compact_button(row, "刷新", self._refresh_windows)
        self.window_refresh_button.pack(side="left", padx=(6, 0))
        actions = tk.Frame(frame, bg=PANEL)
        actions.pack(fill="x", padx=12, pady=(6, 0))
        self.window_connect_button = self._compact_button(actions, "连接", self._connect_window, accent=True)
        self.window_connect_button.pack(side="left", ipadx=14)
        self.window_disconnect_button = self._compact_button(actions, "断开", self._disconnect_window)
        self.window_disconnect_button.configure(state="disabled")
        self.window_disconnect_button.pack(side="left", padx=(6, 0))
        tk.Label(actions, text="连接后仍需手动启动挂机", bg=PANEL, fg=MUTED,
                 font=FONT_SMALL).pack(side="left", padx=10)
        hint = tk.Label(frame, textvariable=self.window_hint, bg=PANEL, fg=MUTED, font=FONT_SMALL,
                        anchor="w", justify="left")
        hint.pack(fill="x", padx=12, pady=(7, 9))
        hint.bind("<Configure>", lambda event: hint.configure(wraplength=max(200, event.width)))

    def _refresh_windows(self) -> None:
        previous = self._window_lookup.get(self.window_choice.get())
        try:
            candidates = window_candidates(load_config(self.config_path))
        except Exception as exc:
            self.window_hint.set(f"读取窗口失败：{exc}。可以稍后点击刷新。")
            return
        self._window_lookup = {
            f"{index}. {target.title} · {Path(target.process_path).name or '未知程序'}": target
            for index, target in enumerate(candidates, 1)
        }
        self.window_combo.configure(values=list(self._window_lookup))
        preferred = previous or self._selected_target
        label = next((label for label, target in self._window_lookup.items()
                      if preferred is not None and target.hwnd == preferred.hwnd
                      and target.pid == preferred.pid), "")
        if not label:
            label = next((label for label, target in self._window_lookup.items()
                          if target.score >= 100), "")
        self.window_choice.set(label)
        if not self._selected_target and not self._stopping_session:
            self.window_hint.set(self._connection_message if candidates else
                                 "尚未找到可选窗口。先打开目标程序，再点击刷新；可以先修改配置。")
        self._refresh_window_controls()

    def _refresh_window_controls(self) -> None:
        waiting = self._stopping_session or self._closing
        self.window_combo.configure(state="disabled" if waiting else "readonly")
        self.window_refresh_button.configure(state="disabled" if waiting else "normal")
        self.window_connect_button.configure(
            state="disabled" if waiting or not self._window_lookup else "normal",
            text="切换" if self._selected_target else "连接",
        )
        self.window_disconnect_button.configure(
            state="normal" if self._selected_target and not waiting else "disabled"
        )

    def _connect_window(self) -> None:
        if self.busy or self._closing or self._stopping_session:
            return
        target = self._window_lookup.get(self.window_choice.get())
        if target is None:
            self.window_hint.set("请先在下拉框中选择实际作用窗口。")
            return
        try:
            resolve_window_target(target)
        except Exception as exc:
            self.window_hint.set(f"暂时无法连接：{exc}")
            return
        same_window = (
            self._selected_target is not None
            and self._selected_target.hwnd == target.hwnd and self._selected_target.pid == target.pid
        )
        if same_window and self.worker is not None and self.worker.is_alive():
            self.window_hint.set("当前已经连接此窗口；点击启动挂机或按 F8 才会开始发键。")
            return
        if not self._persist_settings(apply_runtime=False, notify=False, show_error=True):
            return
        if self.worker is not None:
            self._stop_session(target)
        else:
            self._start_session(target)

    def _start_session(self, target: WindowTarget) -> None:
        # 只能在旧 worker 完全结束后创建：身份、路线及输入请求均为新会话。
        if self.worker is not None:
            raise RuntimeError("旧视觉线程尚未结束，不能连接新窗口")
        try:
            resolve_window_target(target)
            previous_bot = self.bot
            # 旧会话即使已经结束，也不能丢弃尚未确认释放的输入通道。
            previous_bot.keyboard.release_all()
            config = load_config(self.config_path)
            preference = config.setdefault("window", {})
            preference["preferred_title"] = target.title
            preference["preferred_executable"] = Path(target.process_path).name
            save_config(self.config_path, config)
            new_bot = BowmanBot(config, input_authorized=self._input_authorized)
            new_bot.calibration_overlay_visible = previous_bot.calibration_overlay_visible
            new_bot.calibration_overlay_hidden_items = previous_bot.calibration_overlay_hidden_items
            self.bot = new_bot
            self.overlay.reset_session()
            self.overlay.show()
            self.worker_errors.clear()
            self._selected_target = target
            self._pending_target = None
            self._connection_message = f"已连接：{target.title}；保持原校准，启动挂机仍需点击按钮或按 F8。"
            self.window_hint.set(self._connection_message)
            self.worker = threading.Thread(target=self._run_bot, name="MapleVisionWorker", daemon=False)
            self.worker.start()
        except Exception as exc:
            self.worker = None
            self._selected_target = None
            self.overlay.hide()
            self._worker_failed(exc)
        self._refresh_window_controls()

    def _stop_session(self, next_target: WindowTarget | None = None) -> None:
        self._pending_target = next_target
        self._stopping_session = True
        self.overlay.hide()
        self.window_hint.set("正在停止旧窗口并释放按键…")
        # 先发取消信号，再等动作锁，避免正在进行的混合输入等待 UI 抬键。
        self.bot.f9_requested.set()
        try:
            with self.bot.action_lock:
                self.bot.auto_potion.set_enabled(False)
                self.bot.potion_enabled_requested = None
                self.bot.suspend_vision()
                self.bot.f7_requested.clear()
                self.bot.f8_requested.clear()
        except Exception as exc:
            self._pending_target = None
            self.worker_errors.append(exc)
        finally:
            self.bot.request_exit()
        self._refresh_window_controls()

    def _disconnect_window(self) -> None:
        if self.busy or self._closing or self._stopping_session or self.worker is None:
            return
        self._stop_session()

    def _consume_worker_completion(self) -> bool:
        """由 Tk 消费退出；返回 False 表示正在关闭，不再安排下一轮。"""
        if self.busy or self.worker is None or self.worker.is_alive():
            return True
        self.worker.join(timeout=0)
        self.worker = None
        self._selected_target = None
        self.overlay.reset_session()
        errors = self.worker_errors[:]
        self.worker_errors.clear()
        pending = self._pending_target
        self._pending_target = None
        stopped = self._stopping_session
        self._stopping_session = False
        if errors:
            self._worker_failed(errors[0])
        elif stopped:
            self._connection_message = "已断开窗口；可以重新选择，原配置与采集图片已保留。"
            self.window_hint.set(self._connection_message)
            if pending is not None and not self._closing:
                self._start_session(pending)
        else:
            # 活跃会话正常返回来自 F9；窗口丢失走异常恢复路径。
            self.quit()
            return not self._closing
        self._refresh_window_controls()
        return True

    def _target_ready(self) -> bool:
        target = self._selected_target
        if (target is None or self.worker is None or not self.worker.is_alive()
                or self._stopping_session or self._closing):
            self.window_hint.set("请先连接实际作用窗口。")
            return False
        try:
            validate_window_target(target)
        except Exception as exc:
            self._stop_session()
            self.worker_errors.append(exc)
            return False
        return True

    def _build(self) -> None:
        self.root.grid_rowconfigure(1, weight=1)
        self.root.grid_columnconfigure(0, weight=1)

        topbar = tk.Frame(self.root, bg=BG)
        topbar.grid(row=0, column=0, sticky="ew")
        topbar.grid_columnconfigure(0, weight=1)
        brand = tk.Frame(topbar, bg=BG)
        brand.grid(row=0, column=0, sticky="w", padx=14, pady=(9, 0))
        tk.Label(brand, text="Maple Vision", bg=BG, fg=FG, font=FONT_TITLE).pack(side="left")
        tk.Label(brand, text=" / " + self.profile_label, bg=BG, fg=MUTED, font=FONT_SMALL).pack(side="left", padx=(6, 0))
        self._run_badge_label = tk.Label(topbar, textvariable=self.run_badge, bg=ACCENT_SOFT,
                                        fg=ACCENT, font=FONT_SMALL, padx=10, pady=4)
        self._run_badge_label.grid(row=0, column=1, sticky="e", padx=14, pady=(9, 0))
        self.status_label = tk.Label(topbar, textvariable=self.run_metrics, bg=BG, fg=MUTED,
                                     font=FONT_SMALL, anchor="w")
        self.status_label.grid(row=1, column=0, columnspan=2, sticky="ew", padx=14, pady=(2, 7))
        self._wrap_to_width(self.status_label)

        main = tk.Frame(self.root, bg=BG)
        main.grid(row=1, column=0, sticky="nsew", padx=10)
        main.grid_rowconfigure(0, weight=1)
        main.grid_columnconfigure(0, weight=1)
        style = ttk.Style(self.root)
        style.configure("Compact.MBV.TNotebook", tabmargins=(0, 0, 0, 4))
        style.configure("Compact.MBV.TNotebook.Tab", padding=(6, 7), font=FONT_SMALL)
        self.notebook = ttk.Notebook(main, style="Compact.MBV.TNotebook")
        self.notebook.grid(row=0, column=0, sticky="nsew")
        self._page_bodies: dict[str, tk.Frame] = {}
        self._page_canvases: dict[str, tk.Canvas] = {}
        self._page_titles: dict[str, str] = {}
        for key, label in (
            ("connection", "窗口连接"),
            ("capture", "区域校准"), ("templates", "模板采集"),
            ("strategy", "职业策略"), ("supply", "按键补给"),
            ("runtime", "运行设置"), ("performance", "性能监控"),
        ):
            self._create_page(key, label)
        tab_font = tkfont.Font(self.root, font=FONT_SMALL)
        self._full_tabs_width = sum(tab_font.measure(title) + 18 for title in self._page_titles.values()) + 4
        self._compact_tabs = False
        self.notebook.bind("<Configure>", self._fit_page_tabs, add="+")
        self._capture_canvas = self._page_canvases["capture"]
        self._content = self._page_bodies["capture"]
        self.root.bind("<MouseWheel>", self._scroll_page, add="+")
        self.root.bind("<Button-4>", self._scroll_page, add="+")
        self.root.bind("<Button-5>", self._scroll_page, add="+")
        self._build_run_bar()
        connection = self._page_bodies["connection"]
        self._page_intro(connection, "连接游戏窗口", "原配置作为默认值；刷新列表后选择实际作用窗口。")
        self._build_window_selector(connection)
        self._page_intro(self._content, "从区域校准开始", "先采集基础区域，再添加怪物与人物模板。")
        counts_label = tk.Label(self._content, textvariable=self.counts, bg=BG, fg=MUTED,
                                font=FONT_SMALL, anchor="w", justify="left")
        counts_label.pack(fill="x", padx=14, pady=(0, 5))
        self._wrap_to_width(counts_label)

        self._section("基础区域")
        capture = self._last_body
        self._capture_item_row(
            capture,
            "血条区域",
            "hp_bar",
            lambda: self._capture_status_item("hp_bar", "血条区域"),
        )
        self._capture_item_row(
            capture,
            "蓝条区域",
            "mp_bar",
            lambda: self._capture_status_item("mp_bar", "蓝条区域"),
        )
        self._capture_item_row(
            capture,
            "小地图区域",
            "minimap",
            lambda: self._capture_status_item("minimap", "小地图区域"),
        )
        self._capture_item_row(
            capture,
            "小地图玩家标记",
            "player_marker",
            self._capture_player_marker_item,
        )

        self._section("战斗识别")
        capture = self._last_body
        self._capture_item_row(
            capture,
            "战斗识别区域",
            "combat_region",
            self._capture_combat_region_item,
        )
        self._capture_item_row(
            capture,
            "平台安全点（小地图）",
            "platform_center",
            self._capture_platform_center_item,
            show=False,
        )
        self.capture_target_area_button = self._capture_item_row(
            capture,
            "通用索敌范围",
            "targeting_range",
            self._capture_target_range,
        )

        self._content = self._page_bodies["templates"]
        self._page_intro(self._content, "采集与管理模板", "分类管理怪物、姓名板、头部与称号图片，删除后仍可恢复。")
        self._section("模板采集")
        templates = self._last_body
        capture = tk.Frame(templates, bg=PANEL)
        capture.pack(fill="x", padx=6, pady=(4, 7))
        tk.Label(
            capture,
            text="怪物模板",
            bg=PANEL,
            fg=FG,
            font=FONT_SECTION,
            anchor="w",
        ).pack(fill="x", padx=8, pady=(7, 2))
        category_row = tk.Frame(capture, bg=PANEL)
        category_row.pack(fill="x", padx=8, pady=(5, 2))
        tk.Label(
            category_row,
            text="当前识别分类",
            bg=PANEL,
            fg=MUTED,
            font=FONT_SMALL,
            anchor="w",
        ).pack(side="left")
        self.monster_category_combo = ttk.Combobox(
            category_row,
            textvariable=self.monster_category,
            state="readonly",
            width=22,
            font=FONT,
        )
        self.monster_category_combo.pack(side="right", fill="x", expand=True, padx=(8, 0))
        disable_combobox_mousewheel(self.monster_category_combo)
        self.monster_category_combo.bind("<<ComboboxSelected>>", self._monster_category_changed)
        category_buttons = tk.Frame(capture, bg=PANEL)
        category_buttons.pack(fill="x", padx=8, pady=2)
        for label, command in (
            ("新建分类", self._add_monster_category),
            ("重命名", self._rename_monster_category),
            ("删除分类", self._delete_monster_category),
        ):
            RoundedButton(
                category_buttons,
                text=label,
                command=command,
                bg=BUTTON_BG,
                fg=FG,
                activebackground=BUTTON_ACTIVE,
                activeforeground=FG,
                relief="flat",
                font=FONT_SMALL,
                cursor="hand2",
            ).pack(side="left", fill="x", expand=True, padx=2, ipady=2)
        template_actions = tk.Frame(capture, bg=PANEL)
        template_actions.pack(fill="x", padx=8, pady=2)
        self._compact_button(template_actions, "新增怪物模板", lambda: self._capture("monster")).pack(side="left", fill="x", expand=True, padx=(0, 2))
        self._compact_button(template_actions, "采集过滤项", lambda: self._capture("filter")).pack(side="left", fill="x", expand=True, padx=2)
        self._compact_button(
            template_actions,
            "管理怪物模板",
            lambda: self._manage_templates("monster"),
        ).pack(side="left", fill="x", expand=True, padx=(2, 0))
        monster_debug_button = self._compact_button(
            capture,
            "怪物框：开",
            lambda: self._show_capture_item("怪物识别", "monster"),
        )
        monster_debug_button.pack(fill="x", padx=8, pady=2, ipady=2)
        self._debug_item_buttons["monster"] = (monster_debug_button, "怪物框")
        tk.Label(
            capture,
            textvariable=self.monster_category_summary,
            bg=PANEL,
            fg=MUTED,
            font=FONT_SMALL,
            anchor="w",
            justify="left",
        ).pack(fill="x", padx=8, pady=(2, 5))

        divider = tk.Frame(templates, bg=BORDER, height=1)
        divider.pack(fill="x", padx=12, pady=(0, 4))
        player_capture = tk.Frame(templates, bg=PANEL)
        player_capture.pack(fill="x", padx=6, pady=(0, 5))
        tk.Label(
            player_capture,
            text="人物模板",
            bg=PANEL,
            fg=FG,
            font=FONT_SECTION,
            anchor="w",
        ).pack(fill="x", padx=8, pady=(7, 2))
        self._capture_item_row(
            player_capture,
            "玩家姓名板",
            "player",
            lambda: self._capture("player"),
        )
        player_actions = tk.Frame(player_capture, bg=PANEL)
        player_actions.pack(fill="x", padx=8, pady=2)
        self._compact_button(player_actions, "采集头部", lambda: self._capture("head")).pack(
            side="left", fill="x", expand=True, padx=(0, 2)
        )
        self._compact_button(player_actions, "采集称号勋章", lambda: self._capture("title")).pack(
            side="left", fill="x", expand=True, padx=2
        )
        self._compact_button(
            player_actions,
            "管理人物模板",
            lambda: self._manage_templates("player"),
        ).pack(side="left", fill="x", expand=True, padx=(2, 0))
        self._content = self._page_bodies["strategy"]
        self._page_intro(self._content, "职业与战斗方式", "选择职业和策略，调整索敌范围与专属参数。")
        self._section("职业与策略")
        settings = self._last_body
        strategy_row = tk.Frame(settings, bg=PANEL)
        strategy_row.pack(fill="x", padx=8, pady=(3, 2))
        tk.Label(
            strategy_row,
            text="当前职业",
            bg=PANEL,
            fg=MUTED,
            font=FONT_SMALL,
            width=10,
            anchor="w",
        ).pack(side="left")
        self.profession_combo = ttk.Combobox(
            strategy_row,
            textvariable=self.profession_name,
            state="readonly",
            values=list(self._profession_strategies),
            font=FONT,
        )
        self.profession_combo.pack(side="left", fill="x", expand=True)
        disable_combobox_mousewheel(self.profession_combo)
        self.profession_combo.bind("<<ComboboxSelected>>", self._profession_changed)
        strategy_select_row = tk.Frame(settings, bg=PANEL)
        strategy_select_row.pack(fill="x", padx=8, pady=(3, 2))
        tk.Label(
            strategy_select_row,
            text="基础策略",
            bg=PANEL,
            fg=MUTED,
            font=FONT_SMALL,
            width=10,
            anchor="w",
        ).pack(side="left")
        self.strategy_combo = ttk.Combobox(
            strategy_select_row,
            textvariable=self.strategy_name,
            state="readonly",
            font=FONT,
        )
        self.strategy_combo.pack(side="left", fill="x", expand=True)
        disable_combobox_mousewheel(self.strategy_combo)
        self.strategy_combo.bind("<<ComboboxSelected>>", self._strategy_changed)
        tk.Label(
            settings,
            textvariable=self.strategy_description,
            bg=PANEL,
            fg=MUTED,
            font=FONT_SMALL,
            wraplength=330,
            justify="left",
            anchor="w",
        ).pack(fill="x", padx=8, pady=(2, 5))
        self.strategy_settings_body = tk.Frame(settings, bg=PANEL)
        self.strategy_settings_body.pack(fill="x")

        self._section("通用索敌范围")
        settings = self._last_body
        for path, label in (
            ("box.forward", "索敌区前方"),
            ("box.back", "索敌区后方"),
            ("box.up", "索敌区上方"),
            ("box.down", "索敌区下方"),
        ):
            self._labeled_entry(
                settings,
                f"targeting.{path}",
                label,
                entries=self._targeting_entries,
                adjust_step=0.01,
                minimum=0.0,
                maximum=1.0,
                on_adjust=lambda text, field_path=path: self._preview_targeting_setting(field_path, text),
            )
        self._content = self._page_bodies["performance"]
        self._page_intro(self._content, "性能监控", "查看帧率、处理耗时与资源占用，不影响当前运行。")
        self._build_performance_monitor(self._content)
        self._content = self._page_bodies["supply"]
        self._page_intro(self._content, "按键与自动补给", "配置游戏按键、喝药阈值与定时 Buff；会话开关仍需手动开启。")
        self._section("游戏按键")
        settings = self._last_body
        fields = [
            ("keys.attack", "攻击键", None, None, None),
            ("keys.jump", "跳跃键", None, None, None),
            ("keys.down", "下跳方向键", None, None, None),
            ("keys.pickup", "拾取键", None, None, None),
            ("keys.hp_potion", "HP 药键", None, None, None),
            ("keys.mp_potion", "MP 药键", None, None, None),
            ("behavior.attack_interval_seconds", "攻击间隔秒", 0.01, 0.01, 10.0),
            ("behavior.player_lost_move_seconds", "姓名板丢失单向位移秒", 0.05, 0.05, 2.0),
            ("behavior.max_runtime_minutes", "最长运行分钟，0=不限", 1.0, 0.0, 10080.0),
            ("vision.monster_template_threshold", "怪物识别阈值", 0.01, 0.0, 1.0),
            ("vision.monster_structure_weight", "怪物轮廓权重", 0.05, 0.0, 0.9),
            ("vision.monster_filter_threshold", "过滤项识别阈值", 0.01, 0.0, 1.0),
            ("vision.player_name_identity_threshold", "玩家姓名确认阈值", 0.01, 0.0, 1.0),
        ]
        for key, label, step, minimum, maximum in fields:
            if key == "behavior.attack_interval_seconds":
                self._content = self._page_bodies["runtime"]
                self._page_intro(self._content, "识别与运行设置", "调整行为、识别阈值和输入方式，修改后自动保存。")
                self._section("识别与行为")
                settings = self._last_body
            self._labeled_entry(
                settings,
                key,
                label,
                capture=key.startswith("keys."),
                adjust_step=step,
                minimum=minimum,
                maximum=maximum,
                on_adjust=(
                    None
                    if step is None
                    else lambda text, field_path=key: self._preview_common_setting(field_path, text)
                ),
            )
        tk.Checkbutton(
            settings,
            text="姓名板丢失时交替左右位移",
            variable=self.player_lost_recovery,
            command=self._schedule_settings_save,
            bg=PANEL,
            fg=FG,
            selectcolor=ENTRY_BG,
            activebackground=PANEL,
            activeforeground=FG,
            font=FONT,
            anchor="w",
            takefocus=False,
        ).pack(fill="x", padx=8, pady=(5, 2))
        tk.Checkbutton(
            settings,
            text="小地图主导航与遮挡辅助定位",
            variable=self.minimap_assist,
            command=self._schedule_settings_save,
            bg=PANEL,
            fg=FG,
            selectcolor=ENTRY_BG,
            activebackground=PANEL,
            activeforeground=FG,
            font=FONT,
            anchor="w",
            takefocus=False,
        ).pack(fill="x", padx=8, pady=(5, 2))
        tk.Label(
            settings,
            text="仅在本人身份确认且小地图标记唯一时辅助导航；到位等待视觉恢复。遮挡到期、位置异常立即停用旧坐标，不滚动续时。",
            bg=PANEL,
            fg=MUTED,
            font=FONT_SMALL,
            wraplength=360,
            justify="left",
            anchor="w",
        ).pack(fill="x", padx=8, pady=(0, 4))
        for key, label, maximum in (
            ("player_minimap_occlusion_seconds", "遮挡静止补位秒", 5.0),
            ("player_minimap_navigation_seconds", "遮挡导航最长秒", 30.0),
        ):
            self._labeled_entry(
                settings, "vision." + key, label, adjust_step=0.5,
                minimum=0.0, maximum=maximum, direct_numeric_input=True,
                on_adjust=lambda _text: self._schedule_settings_save(),
            )
        self._content = self._page_bodies["supply"]
        self._section("自动补给")
        settings = self._last_body
        self._threshold_control(settings, self.hp_threshold_percent, "HP 自动喝药阈值")
        self._threshold_control(settings, self.mp_threshold_percent, "MP 自动喝药阈值")
        tk.Label(
            settings,
            text="底部开关控制本次会话：关闭不发药键；暂停时仅在游戏前台补药。",
            bg=PANEL,
            fg=MUTED,
            font=FONT_SMALL,
            wraplength=360,
            justify="left",
            anchor="w",
        ).pack(fill="x", padx=8, pady=(0, 5))
        if self.profile_label == "NewMaple":
            RoundedButton(
                settings, text="试听验证提示音", command=self._test_verification_sound,
                bg=BUTTON_BG, fg=FG, relief="flat", font=FONT_SMALL, takefocus=False,
            ).pack(anchor="w", padx=8, pady=2)
            tk.Label(
                settings, text="底部狩猎验证开关仅检测并响铃，不暂停、不答题。人工输入前请暂停挂机并关闭自动喝药。",
                bg=PANEL, fg=MUTED, font=FONT_SMALL, wraplength=360, justify="left",
            ).pack(fill="x", padx=8, pady=(0, 5))
        tk.Label(
            settings,
            text="定时补 Buff（仅挂机运行时）",
            bg=PANEL,
            fg=FG,
            font=FONT_SECTION,
            anchor="w",
        ).pack(fill="x", padx=8, pady=(8, 2))
        tk.Label(
            settings,
            text="各项独立开关，关闭保留配置。首次启动立即施放；F8 暂停保留计时，恢复只补到期项目。",
            bg=PANEL,
            fg=MUTED,
            font=FONT_SMALL,
            wraplength=360,
            justify="left",
            anchor="w",
        ).pack(fill="x", padx=8, pady=(0, 4))
        for index in range(1, 4):
            slot = f"buff_{index}"
            prefix = f"buffs.{slot}"
            toggle = tk.Checkbutton(
                settings,
                text=f"Buff {index}：关闭",
                variable=self.buff_enabled[slot],
                command=lambda selected_slot=slot: self._toggle_buff_slot(selected_slot),
                indicatoron=False,
                bg=BUTTON_BG,
                fg=FG,
                selectcolor=ACCENT_SOFT,
                activebackground=BUTTON_ACTIVE,
                activeforeground=FG,
                relief="flat",
                font=FONT,
                cursor="hand2",
                takefocus=False,
            )
            toggle.pack(fill="x", padx=8, pady=(5, 1), ipady=4)
            self._buff_toggle_buttons[slot] = toggle
            self._labeled_entry(
                settings,
                f"{prefix}.key",
                f"Buff {index} 按键",
                capture=True,
            )
            self._labeled_entry(
                settings,
                f"{prefix}.interval_seconds",
                f"Buff {index} 间隔秒",
                adjust_step=1.0,
                minimum=0.0,
                maximum=86400.0,
                direct_numeric_input=True,
                on_adjust=lambda text, field_path=f"{prefix}.interval_seconds":
                    self._preview_common_setting(field_path, text),
            )
        self._content = self._page_bodies["runtime"]
        self._section("输入与窗口")
        settings = self._last_body
        tk.Checkbutton(
            settings,
            text="没有目标时左右巡逻",
            variable=self.fallback_patrol,
            command=self._schedule_settings_save,
            bg=PANEL,
            fg=FG,
            selectcolor=ENTRY_BG,
            activebackground=PANEL,
            activeforeground=FG,
            font=FONT,
            anchor="w",
        ).pack(fill="x", padx=8, pady=2)
        tk.Checkbutton(
            settings,
            text="目标丢失后拾取一次",
            variable=self.pickup_lost,
            command=self._schedule_settings_save,
            bg=PANEL,
            fg=FG,
            selectcolor=ENTRY_BG,
            activebackground=PANEL,
            activeforeground=FG,
            font=FONT,
            anchor="w",
        ).pack(fill="x", padx=8, pady=2)
        tk.Checkbutton(
            settings,
            text="挂机时强制游戏窗口置顶",
            variable=self.topmost_while_armed,
            command=self._schedule_settings_save,
            bg=PANEL,
            fg=FG,
            selectcolor=ENTRY_BG,
            activebackground=PANEL,
            activeforeground=FG,
            font=FONT,
            anchor="w",
        ).pack(fill="x", padx=8, pady=2)
        tk.Label(
            self._content,
            text="F7 总开关 Debug 框｜各区域按钮可独立控制对应框｜F8 启动/暂停｜F9 或 Ctrl+Shift+Q 退出。",
            bg=BG,
            fg=MUTED,
            font=FONT_SMALL,
            wraplength=400,
            justify="left",
            anchor="w",
        ).pack(fill="x", padx=12, pady=10)

        tk.Label(settings, text="按键投递方式", bg=PANEL, fg=MUTED, font=FONT_SMALL).pack(anchor="w", padx=8, pady=(3, 5))
        delivery_combo = ttk.Combobox(
            settings,
            textvariable=self.delivery,
            values=tuple(DELIVERY_LABELS.values()),
            state="readonly",
            width=22,
        )
        disable_combobox_mousewheel(delivery_combo)
        delivery_combo.bind(
            "<<ComboboxSelected>>",
            lambda _event: self._schedule_settings_save(reconfigure=True),
        )
        delivery_combo.pack(fill="x", padx=8, pady=(0, 5))
        self._content = self._page_bodies["capture"]

        self._refresh_strategy_choices()
        self._wrap_descriptions(self.root)

    @staticmethod
    def _wrap_to_width(label: tk.Label) -> None:
        label.bind("<Configure>", lambda event: label.configure(wraplength=max(120, event.width)), add="+")

    def _wrap_descriptions(self, parent: tk.Misc) -> None:
        for widget in parent.winfo_children():
            if isinstance(widget, tk.Label) and int(widget.cget("wraplength")) > 0:
                self._wrap_to_width(widget)
            self._wrap_descriptions(widget)

    def _page_intro(self, parent: tk.Misc, title: str, subtitle: str) -> None:
        # 页签和卡片已有标题，仅保留一行用途，避免重复标题挤占内容区。
        hint = tk.Label(parent, text=subtitle, bg=BG, fg=MUTED, font=FONT_SMALL, anchor="w", justify="left")
        hint.pack(fill="x", padx=10, pady=(7, 5))
        self._wrap_to_width(hint)

    def _create_page(self, key: str, title: str) -> None:
        shell = tk.Frame(self.notebook, bg=BG)
        self.notebook.add(shell, text=title)
        canvas = tk.Canvas(shell, bg=BG, width=1, height=1, highlightthickness=0, bd=0, yscrollincrement=24)
        scrollbar = ttk.Scrollbar(shell, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y", pady=8)
        canvas.pack(side="left", fill="both", expand=True)
        body = tk.Frame(canvas, bg=BG)
        content_id = canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(content_id, width=event.width))
        self._page_bodies[key] = body
        self._page_canvases[key] = canvas
        self._page_titles[key] = title

    def _fit_page_tabs(self, event: tk.Event) -> None:
        """窄窗口只缩短页签标题，不缩字体、不改变当前页面或其控件。"""
        compact = int(event.width) < self._full_tabs_width
        if compact == self._compact_tabs:
            return
        self._compact_tabs = compact
        short_titles = {
            "connection": "窗口连接", "capture": "校准", "templates": "模板",
            "strategy": "策略", "supply": "补给", "runtime": "设置", "performance": "性能",
        }
        for key, canvas in self._page_canvases.items():
            self.notebook.tab(canvas.master, text=short_titles[key] if compact else self._page_titles[key])

    def _scroll_page(self, event: tk.Event) -> str | None:
        widget = event.widget
        if isinstance(widget, (ttk.Combobox, tk.Listbox, tk.Scale)):
            return None
        # 只路由属于本页的控件，不接管模板管理器或其它 Toplevel 的滚轮。
        while widget is not None:
            for canvas in self._page_canvases.values():
                if widget is canvas:
                    if canvas.yview() != (0.0, 1.0):
                        delta = -1 if getattr(event, "num", None) == 4 else 1 if getattr(event, "num", None) == 5 else -int(event.delta / 120)
                        canvas.yview_scroll(delta, "units")
                    return "break"
            widget = getattr(widget, "master", None)
        return None

    def _build_run_bar(self) -> None:
        self._run_bar = RoundedCard(self.root, padding=6)
        self._run_bar.grid(row=2, column=0, sticky="ew", padx=10, pady=(6, 8))
        footer = self._run_bar.body
        self._notice_label = tk.Label(footer, textvariable=self.run_notice, bg=PANEL, fg=MUTED,
                                      font=FONT_SMALL, anchor="w", justify="left")
        self._wrap_to_width(self._notice_label)
        self._quick_controls = tk.Frame(footer, bg=PANEL)
        self._quick_controls.pack(fill="x", padx=3, pady=(1, 4))
        self.debug_button = tk.Checkbutton(
            self._quick_controls, text="识别框：全部", variable=self.debug_boxes,
            command=self._toggle_debug_boxes, indicatoron=False,
            bg=BUTTON_BG, fg=MUTED, selectcolor=ACCENT_SOFT,
            activebackground=BUTTON_ACTIVE, activeforeground=FG,
            relief="flat", font=FONT_SMALL, cursor="hand2", takefocus=False,
        )
        self.debug_button.pack(side="left", padx=(0, 6), ipady=3, ipadx=3)
        self.potion_button = tk.Checkbutton(
            self._quick_controls, text="自动喝药：关闭", variable=self.auto_potion_enabled,
            command=self._toggle_auto_potion, indicatoron=False,
            bg=BUTTON_BG, fg=FG, selectcolor=ACCENT_SOFT,
            activebackground=BUTTON_ACTIVE, activeforeground=FG,
            relief="flat", font=FONT_SMALL, cursor="hand2", takefocus=False,
        )
        self.potion_button.pack(side="left", padx=(0, 6), ipady=3, ipadx=3)
        self.verification_alert_button = None
        if self.profile_label == "NewMaple":
            self.verification_alert_button = tk.Checkbutton(
                self._quick_controls, text="狩猎验证响铃", variable=self.verification_alert_enabled,
                command=self._toggle_verification_alert,
                bg=PANEL, fg=FG, selectcolor=ENTRY_BG, activebackground=PANEL,
                activeforeground=FG, font=FONT_SMALL, takefocus=False,
            )
            self.verification_alert_button.pack(side="left", padx=(2, 0), ipady=3)
        actions = tk.Frame(footer, bg=PANEL)
        actions.pack(fill="x", padx=3, pady=(0, 1))
        self.arm_button = self._compact_button(actions, "启动挂机", self._toggle_arm, accent=True)
        self.arm_button.pack(side="right", ipadx=14)
        self.save_button = self._compact_button(actions, "保存配置", self._save_settings)
        self.save_button.pack(side="right", padx=6)
        self.exit_button = self._compact_button(actions, "退出", self.quit)
        self.exit_button.pack(side="left")
        tk.Label(actions, text="F8 启停 · F9 退出", bg=PANEL, fg=MUTED, font=FONT_SMALL).pack(side="left", padx=8)

    def _build_performance_monitor(self, parent: tk.Misc) -> None:
        self._performance_shell = tk.Frame(parent, bg=BG)
        self._performance_shell.pack(fill="x", padx=14, pady=(0, 7))

        self._performance_card = RoundedCard(self._performance_shell, padding=6)
        heading = tk.Frame(self._performance_card.body, bg=PANEL)
        heading.pack(fill="x", padx=6, pady=(3, 3))
        tk.Label(
            heading,
            text="性能监控",
            bg=PANEL,
            fg=FG,
            font=FONT_SMALL,
            anchor="w",
        ).pack(side="left")
        summary = tk.Label(
            self._performance_card.body,
            textvariable=self.performance_text,
            bg=PANEL,
            fg=FG,
            font=FONT_SMALL,
            anchor="w",
            justify="left",
            wraplength=500,
        )
        summary.pack(fill="x", padx=12, pady=(0, 10))
        self._wrap_to_width(summary)
        RoundedButton(
            heading,
            text="×",
            command=lambda: self._set_performance_monitor_visible(False),
            bg=PANEL,
            fg=MUTED,
            activebackground=BUTTON_ACTIVE,
            activeforeground=FG,
            relief="flat",
            bd=0,
            font=FONT,
            cursor="hand2",
            takefocus=False,
        ).pack(side="right", padx=(4, 0))

        self._performance_collapsed_button = RoundedButton(
            self._performance_shell,
            text="显示性能监控",
            command=lambda: self._set_performance_monitor_visible(True),
            bg=BUTTON_BG,
            fg=MUTED,
            activebackground=BUTTON_ACTIVE,
            activeforeground=FG,
            relief="flat",
            bd=0,
            font=FONT_SMALL,
            cursor="hand2",
            takefocus=False,
        )
        self._render_performance_monitor_visibility()

    def _render_performance_monitor_visibility(self) -> None:
        self._performance_card.pack_forget()
        self._performance_collapsed_button.pack_forget()
        if bool(self.performance_visible.get()):
            self._performance_card.pack(fill="x")
        else:
            self._performance_collapsed_button.pack(anchor="e", ipadx=6, ipady=2)

    def _set_performance_monitor_visible(self, visible: bool, *, persist: bool = True) -> None:
        self.performance_visible.set(bool(visible))
        self._render_performance_monitor_visibility()
        if visible:
            self._schedule_performance_refresh(delay_ms=0)
        else:
            self._cancel_performance_refresh()
        if persist:
            self._schedule_settings_save()

    def _cancel_performance_refresh(self) -> None:
        after_id = getattr(self, "_performance_after_id", None)
        if after_id is None:
            return
        self._performance_after_id = None
        try:
            self.root.after_cancel(after_id)
        except tk.TclError:
            pass

    def _schedule_performance_refresh(self, *, delay_ms: int | None = None) -> None:
        self._cancel_performance_refresh()
        visible = getattr(self, "performance_visible", None)
        if visible is None or not bool(visible.get()):
            return
        delay = self._performance_interval_ms if delay_ms is None else max(0, int(delay_ms))
        try:
            self._performance_after_id = self.root.after(delay, self._refresh_performance_monitor)
        except tk.TclError:
            self._performance_after_id = None

    def _refresh_performance_monitor(self) -> None:
        self._performance_after_id = None
        visible = getattr(self, "performance_visible", None)
        if visible is None or not bool(visible.get()):
            return
        summary = "性能数据暂不可用"
        try:
            performance = object.__getattribute__(self.bot, "performance")
            snapshot_method = object.__getattribute__(performance, "snapshot")
            if callable(snapshot_method):
                summary = str(format_performance_summary(snapshot_method()))
        except Exception:
            pass
        performance_text = getattr(self, "performance_text", None)
        if performance_text is not None:
            performance_text.set(summary)
        self._schedule_performance_refresh()

    def _load_performance_monitor_settings(self, config: dict[str, Any]) -> None:
        performance_monitor = config["performance_monitor"]
        visible = bool(performance_monitor["visible"])
        self.performance_visible.set(visible)
        self._performance_interval_ms = int(
            round(float(performance_monitor["refresh_interval_seconds"]) * 1000)
        )
        self._render_performance_monitor_visibility()
        if visible:
            self._schedule_performance_refresh(delay_ms=0)
        else:
            self._cancel_performance_refresh()

    def _compact_button(
        self,
        parent: tk.Misc,
        text: str,
        command: Callable[[], None],
        *,
        accent: bool = False,
    ) -> tk.Button:
        button = RoundedButton(
            parent,
            text=text,
            command=command,
            bg=ACCENT if accent else BUTTON_BG,
            fg=PANEL if accent else FG,
            activebackground=ACCENT_HOVER if accent else BUTTON_ACTIVE,
            activeforeground=PANEL if accent else FG,
            relief="flat",
            bd=0,
            font=FONT,
            cursor="hand2",
        )
        style_button(button, primary=accent)
        return button

    def _capture_item_row(
        self,
        parent: tk.Misc,
        label: str,
        key: str,
        command: Callable[[], None],
        *,
        show: bool = True,
    ) -> tk.Button:
        row = tk.Frame(parent, bg=PANEL)
        row.pack(fill="x", padx=8, pady=2)
        tk.Label(row, text=label, bg=PANEL, fg=FG, font=FONT, anchor="w").pack(
            side="left", fill="x", expand=True, padx=(5, 4), pady=3
        )
        variable = tk.StringVar(value="未采集")
        status = tk.Label(row, textvariable=variable, bg=PANEL, fg=MUTED, font=FONT_SMALL, width=7, anchor="e")
        status.pack(side="left", padx=4)
        self._capture_status_vars[key] = variable
        self._capture_status_labels[key] = status
        show_button = self._compact_button(
            row,
            "显示",
            lambda selected=label, selected_key=key: self._show_capture_item(selected, selected_key),
        )
        button = self._compact_button(
            row,
            "采集",
            lambda selected=label, action=command: self._select_capture_item(selected, action),
        )
        button.pack(side="right", padx=4, pady=2, ipadx=3)
        if show:
            show_button.pack(side="right", padx=(2, 0), pady=2, ipadx=2)
            self._debug_item_buttons[key] = (show_button, "框")
        self._capture_buttons[key] = button
        return button

    def _select_capture_item(self, label: str, command: Callable[[], None]) -> None:
        command()

    def _show_capture_item(self, label: str, key: str) -> None:
        visible = self.bot.toggle_calibration_overlay_item(key)
        self.debug_boxes.set(self.bot.calibration_overlay_visible)
        self._refresh_debug_item_buttons()
        self.bot.notify(f"{label} Debug 框已{'开启' if visible else '关闭'}", 2.0)

    def _refresh_debug_item_buttons(self) -> None:
        stale: list[str] = []
        for key, (button, label) in self._debug_item_buttons.items():
            try:
                visible = self.bot.calibration_overlay_item_visible(key)
                button.configure(
                    text=f"{label}：{'开' if visible else '关'}",
                    bg=ACCENT_SOFT if visible else BUTTON_BG,
                    fg=ACCENT if visible else MUTED,
                    activebackground=BUTTON_ACTIVE if visible else BUTTON_ACTIVE,
                    activeforeground=ACCENT if visible else FG,
                )
            except tk.TclError:
                stale.append(key)
        for key in stale:
            self._debug_item_buttons.pop(key, None)

    def _refresh_strategy_choices(self) -> None:
        profession = self.profession_name.get()
        strategies = self._profession_strategies.get(profession, ())
        names = [item.display_name for item in strategies]
        self.strategy_combo.configure(values=names)
        if names and self.strategy_name.get() not in names:
            self.strategy_name.set(names[0])

    def _profession_changed(self, _event: tk.Event | None = None) -> None:
        self._refresh_strategy_choices()
        self._strategy_changed()

    @staticmethod
    def _item_complete(config: dict[str, Any], key: str) -> bool:
        value = config.get("calibration", {}).get("items", {}).get(key, {})
        if isinstance(value, dict):
            return bool(value.get("complete"))
        return bool(value)

    def _refresh_capture_status(self, config: dict[str, Any]) -> None:
        counts = template_counts(config)
        status_values: dict[str, tuple[str, str]] = {}
        for key in ("hp_bar", "mp_bar", "minimap", "player_marker", "combat_region", "platform_center", "targeting_range"):
            complete = self._item_complete(config, key)
            status_values[key] = ("已通过" if complete else "未采集", SUCCESS if complete else MUTED)
        status_values["player"] = (
            (f"{counts['player']} 张" if counts["player"] else "未采集"),
            SUCCESS if counts["player"] else MUTED,
        )
        for key, (text, color) in status_values.items():
            variable = self._capture_status_vars.get(key)
            label = self._capture_status_labels.get(key)
            if variable is not None:
                variable.set(text)
            if label is not None:
                label.configure(fg=color)
            button = self._capture_buttons.get(key)
            if button is not None:
                button.configure(text="重采" if color == SUCCESS else "采集")

    def _section(self, title: str) -> None:
        wrap = RoundedCard(self._content, padding=6)
        wrap.pack(fill="x", padx=8, pady=(4, 4))
        tk.Label(wrap.body, text=title, bg=PANEL, fg=FG, font=FONT_SECTION, anchor="w").pack(fill="x", padx=6, pady=(1, 3))
        body = tk.Frame(wrap.body, bg=PANEL)
        body.pack(fill="x", pady=(0, 1))
        self._last_body = body

    def _row_button(self, parent: tk.Misc, text: str, command: Callable[[], None]) -> tk.Button:
        button = RoundedButton(
            parent,
            text=text,
            command=command,
            bg=BUTTON_BG,
            fg=FG,
            activebackground=BUTTON_ACTIVE,
            activeforeground=FG,
            relief="flat",
            font=FONT,
            cursor="hand2",
        )
        button.pack(fill="x", padx=8, pady=3, ipady=3)
        return button

    def _labeled_entry(
        self,
        parent: tk.Misc,
        key: str,
        label: str,
        capture: bool = False,
        entries: dict[str, tk.Entry] | None = None,
        adjust_step: float | None = None,
        minimum: float | None = None,
        maximum: float | None = None,
        on_adjust: Callable[[str], None] | None = None,
        direct_numeric_input: bool = False,
    ) -> None:
        row = tk.Frame(parent, bg=PANEL)
        row.pack(fill="x", padx=8, pady=2)
        label_widget = tk.Label(row, text=label, bg=PANEL, fg=FG, font=FONT_SMALL,
                               width=18, wraplength=175, justify="left", anchor="w")
        label_widget.pack(side="left", padx=(0, 8))
        entry = tk.Entry(
            row,
            bg=ENTRY_BG,
            fg=FG,
            insertbackground=FG,
            relief="flat",
            font=FONT,
        )
        if capture:
            RoundedButton(
                row,
                text="采集",
                command=lambda: self._capture_key(key),
                bg=BUTTON_BG,
                fg=FG,
                activebackground=BUTTON_ACTIVE,
                activeforeground=FG,
                relief="flat",
                font=FONT_SMALL,
                cursor="hand2",
                width=4,
                padx=5,
            ).pack(side="right", padx=(4, 0))
        if direct_numeric_input:
            RoundedButton(
                row,
                text="输入",
                command=lambda: self._prompt_numeric_entry(entry, label, minimum, maximum, on_adjust),
                bg=BUTTON_BG,
                fg=FG,
                activebackground=BUTTON_ACTIVE,
                activeforeground=FG,
                relief="flat",
                font=FONT_SMALL,
                cursor="hand2",
                takefocus=False,
                width=4,
                padx=5,
            ).pack(side="right", padx=(4, 0))
        if adjust_step is not None:
            def adjust(delta: float) -> None:
                previous = entry.get()
                try:
                    text = adjusted_numeric_text(previous, delta, minimum, maximum)
                except ValueError:
                    messagebox.showerror("参数格式错误", f"“{label}”不是有效数字", parent=self.root)
                    return
                try:
                    if on_adjust is not None:
                        on_adjust(text)
                except Exception as exc:
                    messagebox.showerror("保存策略参数失败", str(exc), parent=self.root)
                    return
                entry.delete(0, "end")
                entry.insert(0, text)

            RoundedButton(
                row,
                text="+",
                command=lambda: adjust(float(adjust_step)),
                bg=BUTTON_BG,
                fg=FG,
                activebackground=BUTTON_ACTIVE,
                activeforeground=FG,
                relief="flat",
                font=FONT_SMALL,
                takefocus=False,
                width=2,
                padx=4,
            ).pack(side="right", padx=(2, 0))
            RoundedButton(
                row,
                text="−",
                command=lambda: adjust(-float(adjust_step)),
                bg=BUTTON_BG,
                fg=FG,
                activebackground=BUTTON_ACTIVE,
                activeforeground=FG,
                relief="flat",
                font=FONT_SMALL,
                takefocus=False,
                width=2,
                padx=4,
            ).pack(side="right", padx=(4, 0))
        entry.pack(side="left", fill="x", expand=True, ipady=3)
        if capture:
            entry.bind("<Button-1>", lambda _event: self._capture_key(key))
        (self._entries if entries is None else entries)[key] = entry

    def _prompt_numeric_entry(
        self,
        entry: tk.Entry,
        label: str,
        minimum: float | None,
        maximum: float | None,
        on_adjust: Callable[[str], None] | None,
    ) -> None:
        try:
            initial_value = float(entry.get().strip())
        except ValueError:
            initial_value = minimum if minimum is not None else 0.0
        value = simpledialog.askfloat(
            "输入数字",
            f"请输入{label}：",
            parent=self.root,
            initialvalue=initial_value,
            minvalue=minimum,
            maxvalue=maximum,
        )
        if value is None:
            return
        text = adjusted_numeric_text(str(value), 0.0, minimum, maximum)
        try:
            if on_adjust is not None:
                on_adjust(text)
        except Exception as exc:
            messagebox.showerror("保存参数失败", str(exc), parent=self.root)
            return
        entry.delete(0, "end")
        entry.insert(0, text)

    def _threshold_control(self, parent: tk.Misc, variable: tk.IntVar, label: str) -> None:
        wrap = tk.Frame(parent, bg=PANEL)
        wrap.pack(fill="x", padx=8, pady=4)
        header = tk.Frame(wrap, bg=PANEL)
        header.pack(fill="x")
        tk.Label(header, text=label, bg=PANEL, fg=MUTED, font=FONT_SMALL, anchor="w").pack(side="left")
        value_label = tk.Label(header, bg=PANEL, fg=FG, font=FONT, width=5, anchor="e")
        value_label.pack(side="right")

        def refresh(*_args: Any) -> None:
            value_label.configure(text=f"{int(variable.get())}%")
            self._schedule_settings_save()

        def adjust(delta: int) -> None:
            variable.set(max(0, min(100, int(variable.get()) + delta)))

        controls = tk.Frame(wrap, bg=PANEL)
        controls.pack(fill="x", pady=(2, 0))
        RoundedButton(
            controls,
            text="−1%",
            command=lambda: adjust(-1),
            bg=BUTTON_BG,
            fg=FG,
            activebackground=BUTTON_ACTIVE,
            activeforeground=FG,
            relief="flat",
            font=FONT_SMALL,
            takefocus=False,
            width=5,
        ).pack(side="left")
        tk.Scale(
            controls,
            from_=0,
            to=100,
            orient="horizontal",
            variable=variable,
            showvalue=False,
            resolution=1,
            bg=PANEL,
            fg=FG,
            troughcolor=ENTRY_BG,
            highlightthickness=0,
            bd=0,
            takefocus=False,
        ).pack(side="left", fill="x", expand=True, padx=6)
        RoundedButton(
            controls,
            text="+1%",
            command=lambda: adjust(1),
            bg=BUTTON_BG,
            fg=FG,
            activebackground=BUTTON_ACTIVE,
            activeforeground=FG,
            relief="flat",
            font=FONT_SMALL,
            takefocus=False,
            width=5,
        ).pack(side="right")
        variable.trace_add("write", refresh)
        refresh()

    def _nested(self, config: dict[str, Any], dotted: str, value: Any | None = None) -> Any:
        parts = dotted.split(".")
        cursor: Any = config
        for part in parts[:-1]:
            if value is not None and part not in cursor:
                cursor[part] = {}
            cursor = cursor[part]
        name = parts[-1]
        if value is None:
            return cursor[name]
        cursor[name] = value
        return value

    def _load_entries(self, config: dict[str, Any]) -> None:
        previous_loading = self._loading_settings
        self._loading_settings = True
        try:
            strategy = active_strategy(config)
            self.profession_name.set(strategy.profession)
            self.strategy_name.set(strategy.display_name)
            self._refresh_strategy_choices()
            self._render_strategy_settings(config)
            for key, entry in self._entries.items():
                value = self._nested(config, key)
                entry.delete(0, "end")
                entry.insert(0, str(value))
            for key, entry in self._strategy_entries.items():
                value = self._nested(config, key)
                entry.delete(0, "end")
                entry.insert(0, str(value))
            for key, variable in self._strategy_toggles.items():
                variable.set(bool(self._nested(config, key)))
            for key, entry in self._targeting_entries.items():
                value = self._nested(config, key)
                entry.delete(0, "end")
                entry.insert(0, str(value))
            self.delivery.set(DELIVERY_LABELS[input_delivery(config)])
            self.hp_threshold_percent.set(int(round(float(config["behavior"]["hp_threshold"]) * 100)))
            self.mp_threshold_percent.set(int(round(float(config["behavior"]["mp_threshold"]) * 100)))
            self.fallback_patrol.set(bool(config["behavior"].get("fallback_patrol")))
            self.pickup_lost.set(bool(config["behavior"].get("pickup_after_target_lost")))
            self.player_lost_recovery.set(
                bool(config["behavior"].get("player_lost_recovery_enabled", True))
            )
            minimap_assist = getattr(self, "minimap_assist", None)
            if minimap_assist is not None:
                minimap_assist.set(bool(config["vision"].get("player_minimap_assist_enabled", True)))
            for slot, variable in getattr(self, "buff_enabled", {}).items():
                variable.set(bool(config["buffs"][slot].get("enabled", False)))
            self._refresh_buff_toggle_buttons()
            self._load_performance_monitor_settings(config)
            alert_enabled = getattr(self, "verification_alert_enabled", None)
            if alert_enabled is not None:
                alert_enabled.set(bool(config["verification_alert"]["enabled"]))
        finally:
            self._loading_settings = previous_loading

    def _selected_strategy(self):
        key = self._strategy_lookup.get(self.strategy_name.get())
        if key is None:
            raise RuntimeError("请选择职业策略")
        return get_strategy(key)

    def _render_strategy_settings(self, config: dict[str, Any]) -> None:
        for item in list_strategies():
            for field in item.capture_fields:
                self._debug_item_buttons.pop(field.recognition_key, None)
        for child in self.strategy_settings_body.winfo_children():
            child.destroy()
        self._strategy_entries.clear()
        self._strategy_toggles.clear()
        self._strategy_choices.clear()
        strategy = self._selected_strategy()
        self.strategy_description.set(strategy.description)
        prefix = f"strategy.options.{strategy.key}."
        for field in strategy.toggle_fields:
            variable = tk.BooleanVar(value=bool(self._nested(config, prefix + field.path)))
            self._strategy_toggles[prefix + field.path] = variable
            tk.Checkbutton(
                self.strategy_settings_body,
                text=field.label,
                variable=variable,
                command=lambda path=field.path, selected=variable: self._preview_strategy_toggle(path, selected),
                bg=PANEL,
                fg=FG,
                selectcolor=ENTRY_BG,
                activebackground=PANEL,
                activeforeground=FG,
                font=FONT,
                anchor="w",
            ).pack(fill="x", padx=8, pady=2)
        for field in strategy.setting_fields:
            self._labeled_entry(
                self.strategy_settings_body,
                prefix + field.path,
                field.label,
                entries=self._strategy_entries,
                adjust_step=field.step,
                minimum=field.minimum,
                maximum=field.maximum,
                capture=field.capture_key,
                direct_numeric_input=field.direct_numeric_input,
                on_adjust=lambda text, path=field.path: self._preview_strategy_setting(path, text),
            )
        for field in strategy.choice_fields:
            row = tk.Frame(self.strategy_settings_body, bg=PANEL)
            row.pack(fill="x", padx=8, pady=2)
            tk.Label(
                row,
                text=field.label,
                bg=PANEL,
                fg=MUTED,
                font=FONT_SMALL,
                width=18,
                anchor="w",
            ).pack(side="left")
            value_to_label = dict(field.choices)
            label_to_value = {label: value for value, label in field.choices}
            selected_value = str(self._nested(config, prefix + field.path))
            variable = tk.StringVar(value=value_to_label.get(selected_value, selected_value))
            combo = ttk.Combobox(
                row,
                textvariable=variable,
                state="readonly",
                values=list(label_to_value),
                font=FONT,
            )
            combo.pack(side="right", fill="x", expand=True)
            disable_combobox_mousewheel(combo)
            combo.bind(
                "<<ComboboxSelected>>",
                lambda _event, path=field.path, selected=variable, choices=label_to_value:
                    self._preview_strategy_choice(path, selected, choices),
            )
            self._strategy_choices[prefix + field.path] = (variable, label_to_value)
        for field in strategy.capture_fields:
            row = tk.Frame(self.strategy_settings_body, bg=PANEL)
            row.pack(fill="x", padx=8, pady=3)
            capture_button = self._compact_button(
                row,
                (field.button_label + (" ✓" if config.get("recognition", {}).get(
                    f"{field.recognition_key}_captured") else "（待采）"))
                if field.capture_kind == "point" else field.button_label,
                lambda selected=field: self._capture_strategy_area(selected),
            )
            capture_button.pack(side="left", fill="x", expand=True, ipady=3)
            debug_button = self._compact_button(
                row,
                "框：开",
                lambda selected=field: self._show_capture_item(
                    selected.debug_label,
                    selected.recognition_key,
                ),
            )
            debug_button.pack(side="right", padx=(5, 0), ipadx=4, ipady=3)
            self._debug_item_buttons[field.recognition_key] = (debug_button, "框")
            if field.multiple and field.settings_path:
                self._render_strategy_regions(config, strategy.key, field)
        self._refresh_debug_item_buttons()
        self._wrap_descriptions(self.strategy_settings_body)

    def _render_strategy_regions(
        self,
        config: dict[str, Any],
        strategy_key: str,
        field: StrategyCaptureField,
    ) -> None:
        if not field.settings_path:
            return
        raw_regions = self._nested(
            config,
            f"strategy.options.{strategy_key}.{field.settings_path}",
        )
        regions = raw_regions if isinstance(raw_regions, list) else []
        if not regions:
            tk.Label(
                self.strategy_settings_body,
                text="尚未框选索敌区；启用此功能后至少需要一个已启用区域。",
                bg=PANEL,
                fg=WARNING,
                font=FONT_SMALL,
                anchor="w",
                wraplength=500,
            ).pack(fill="x", padx=12, pady=(0, 4))
            return
        for index, region in enumerate(regions, start=1):
            if not isinstance(region, dict):
                continue
            region_id = str(region.get("id", f"region_{index}"))
            card = tk.Frame(
                self.strategy_settings_body,
                bg=PANEL,
                highlightbackground=BORDER,
                highlightthickness=1,
            )
            card.pack(fill="x", padx=8, pady=2)
            enabled = tk.BooleanVar(value=bool(region.get("enabled", True)))
            tk.Checkbutton(
                card,
                text=str(region.get("name", "")).strip() or f"索敌区 {index}",
                variable=enabled,
                command=lambda selected=enabled, selected_id=region_id, selected_key=strategy_key, selected_field=field:
                    self._update_strategy_region(
                        selected_key,
                        selected_field,
                        selected_id,
                        enabled=bool(selected.get()),
                    ),
                bg=PANEL,
                fg=FG,
                selectcolor=ENTRY_BG,
                activebackground=PANEL,
                activeforeground=FG,
                font=FONT,
                anchor="w",
            ).pack(side="left", fill="x", expand=True, padx=(6, 2), pady=4)
            tk.Label(
                card,
                text=f"优先级 {int(region.get('priority', index))}",
                bg=PANEL,
                fg=MUTED,
                font=FONT_SMALL,
            ).pack(side="left", padx=3)
            for label, command in (
                (
                    "改名",
                    lambda selected_id=region_id, selected_key=strategy_key, selected_field=field:
                        self._rename_strategy_region(selected_key, selected_field, selected_id),
                ),
                (
                    "优先级",
                    lambda selected_id=region_id, selected_key=strategy_key, selected_field=field:
                        self._change_strategy_region_priority(selected_key, selected_field, selected_id),
                ),
                (
                    "重框",
                    lambda selected_id=region_id, selected_field=field:
                        self._capture_strategy_area(selected_field, selected_id),
                ),
                (
                    "删除",
                    lambda selected_id=region_id, selected_key=strategy_key, selected_field=field:
                        self._delete_strategy_region(selected_key, selected_field, selected_id),
                ),
            ):
                self._compact_button(card, label, command).pack(side="left", padx=2, pady=3, ipadx=2)

    def _update_strategy_region(
        self,
        strategy_key: str,
        field: StrategyCaptureField,
        region_id: str,
        **updates: Any,
    ) -> None:
        if not field.settings_path:
            return
        config = load_config(self.config_path)
        regions = self._nested(
            config,
            f"strategy.options.{strategy_key}.{field.settings_path}",
        )
        if not isinstance(regions, list):
            return
        for region in regions:
            if isinstance(region, dict) and str(region.get("id")) == region_id:
                region.update(updates)
                break
        save_config(self.config_path, config)
        if strategy_key == self.bot.strategy.key:
            self.bot.apply_config(config)
        self._load_entries(config)

    def _rename_strategy_region(
        self,
        strategy_key: str,
        field: StrategyCaptureField,
        region_id: str,
    ) -> None:
        name = simpledialog.askstring("重命名索敌区", "输入索敌区名称：", parent=self.root)
        if name is not None and name.strip():
            self._update_strategy_region(strategy_key, field, region_id, name=name.strip())

    def _change_strategy_region_priority(
        self,
        strategy_key: str,
        field: StrategyCaptureField,
        region_id: str,
    ) -> None:
        priority = simpledialog.askinteger(
            "调整索敌区优先级",
            "输入 0～999；数值越小越优先：",
            parent=self.root,
            minvalue=0,
            maxvalue=999,
        )
        if priority is not None:
            self._update_strategy_region(strategy_key, field, region_id, priority=int(priority))

    def _delete_strategy_region(
        self,
        strategy_key: str,
        field: StrategyCaptureField,
        region_id: str,
    ) -> None:
        if not field.settings_path or not messagebox.askyesno(
            "删除索敌区",
            "确定删除这个策略索敌区吗？",
            parent=self.root,
        ):
            return
        config = load_config(self.config_path)
        regions = self._nested(
            config,
            f"strategy.options.{strategy_key}.{field.settings_path}",
        )
        if not isinstance(regions, list):
            return
        remaining = [
            region
            for region in regions
            if not isinstance(region, dict) or str(region.get("id")) != region_id
        ]
        self._nested(
            config,
            f"strategy.options.{strategy_key}.{field.settings_path}",
            remaining,
        )
        if not remaining:
            item = config.setdefault("calibration", {}).setdefault("items", {}).setdefault(
                field.recognition_key,
                {},
            )
            if isinstance(item, dict):
                item["complete"] = False
        save_config(self.config_path, config)
        if strategy_key == self.bot.strategy.key:
            self.bot.apply_config(config)
        self._load_entries(config)

    def _strategy_changed(self, _event: tk.Event | None = None) -> None:
        try:
            config = load_config(self.config_path)
            strategy = self._selected_strategy()
            config["strategy"]["active"] = strategy.key
            save_config(self.config_path, config)
            self.bot.apply_config(config)
            self._render_strategy_settings(config)
            for key, entry in self._strategy_entries.items():
                entry.delete(0, "end")
                entry.insert(0, str(self._nested(config, key)))
            for key, variable in self._strategy_toggles.items():
                variable.set(bool(self._nested(config, key)))
        except Exception as exc:
            messagebox.showerror("切换职业策略失败", str(exc), parent=self.root)

    def _preview_strategy_setting(self, path: str, text: str) -> None:
        strategy = self._selected_strategy()
        value = float(text)
        config = load_config(self.config_path)
        self._nested(config, f"strategy.options.{strategy.key}.{path}", value)
        # 微调按钮是无焦点控件；每次点击直接持久化，避免用户重启后丢失。
        save_config(self.config_path, config)
        if strategy.key == self.bot.strategy.key:
            self.bot.preview_strategy_setting(path, value)

    def _preview_strategy_toggle(self, path: str, variable: tk.BooleanVar) -> None:
        strategy = self._selected_strategy()
        value = bool(variable.get())
        config = load_config(self.config_path)
        self._nested(config, f"strategy.options.{strategy.key}.{path}", value)
        save_config(self.config_path, config)
        if strategy.key == self.bot.strategy.key:
            if any(field.path == path and field.live_preview for field in strategy.toggle_fields):
                self.bot.preview_strategy_setting(path, value)
            else:
                self.bot.apply_config(config)

    def _preview_strategy_choice(
        self,
        path: str,
        variable: tk.StringVar,
        choices: dict[str, str],
    ) -> None:
        strategy = self._selected_strategy()
        label = variable.get()
        if label not in choices:
            return
        config = load_config(self.config_path)
        self._nested(config, f"strategy.options.{strategy.key}.{path}", choices[label])
        save_config(self.config_path, config)
        if strategy.key == self.bot.strategy.key:
            self.bot.apply_config(config)

    def _preview_targeting_setting(self, path: str, text: str) -> None:
        value = float(text)
        config = load_config(self.config_path)
        self._nested(config, f"targeting.{path}", value)
        save_config(self.config_path, config)
        self.bot.preview_targeting_setting(path, value)

    def _preview_common_setting(self, path: str, text: Any) -> None:
        config = load_config(self.config_path)
        current = self._nested(config, path)
        if isinstance(current, bool):
            value: Any = bool(text)
        else:
            value = float(text)
            if isinstance(current, int) and path.endswith("minutes"):
                value = int(float(text))
        self._nested(config, path, value)
        save_config(self.config_path, config)
        self.bot.preview_config_setting(path, value)

    def _refresh_buff_toggle_buttons(self) -> None:
        for slot, button in getattr(self, "_buff_toggle_buttons", {}).items():
            variable = self.buff_enabled.get(slot)
            if variable is None:
                continue
            enabled = bool(variable.get())
            index = slot.rsplit("_", 1)[-1]
            button.configure(
                text=f"Buff {index}：{'开启' if enabled else '关闭'}",
                bg=ACCENT_SOFT if enabled else BUTTON_BG,
                fg=ACCENT if enabled else FG,
                activebackground=BUTTON_ACTIVE if enabled else BUTTON_ACTIVE,
                activeforeground=ACCENT if enabled else FG,
            )

    def _toggle_buff_slot(self, slot: str) -> None:
        variable = getattr(self, "buff_enabled", {}).get(slot)
        if variable is None:
            return
        enabled = bool(variable.get())
        if self.busy:
            variable.set(not enabled)
            self._refresh_buff_toggle_buttons()
            return
        try:
            self._preview_common_setting(f"buffs.{slot}.enabled", enabled)
        except Exception as exc:
            variable.set(not enabled)
            messagebox.showerror("保存 Buff 开关失败", str(exc), parent=self.root)
            self._refresh_buff_toggle_buttons()
            return
        self._refresh_buff_toggle_buttons()
        self.bot.notify(f"Buff {slot.rsplit('_', 1)[-1]} 已{'开启' if enabled else '关闭'}", 3.0)

    def _schedule_settings_save(self, reconfigure: bool = False) -> None:
        if self._loading_settings:
            return
        self._autosave_reconfigure = self._autosave_reconfigure or bool(reconfigure)
        if self._autosave_after_id is not None:
            try:
                self.root.after_cancel(self._autosave_after_id)
            except tk.TclError:
                pass
        self._autosave_after_id = self.root.after(
            180,
            self._run_scheduled_settings_save,
        )

    def _run_scheduled_settings_save(self) -> None:
        reconfigure = self._autosave_reconfigure
        self._autosave_after_id = None
        self._autosave_reconfigure = False
        self._persist_settings(apply_runtime=reconfigure, notify=False, show_error=True)

    def _refresh_counts(self) -> None:
        selected_category = self._refresh_monster_categories()
        if selected_category != self.bot.active_monster_category:
            self._activate_monster_category(selected_category, notify=False)
        config = load_config(self.config_path)
        counts = template_counts(config)
        calibration = config.get("calibration", {})
        status_ready = "状态区✓" if calibration.get("status_regions_complete") else "状态区待采"
        recognition_ready = "识别区✓" if calibration.get("recognition_region_complete") else "识别区待采"
        center_ready = "平台安全点✓" if config["recognition"].get("platform_center_captured") else "平台安全点待采"
        self.counts.set(
            f"{status_ready}｜{recognition_ready}｜{center_ready}｜怪物 {counts['monster']}（{counts['category']} 类）｜"
            f"过滤 {counts['filter']}｜"
            f"姓名板 {counts['player']}｜"
            f"头部 {counts['head']}｜称号 {counts['title']}"
        )
        self._refresh_capture_status(config)

    def _refresh_monster_categories(self, preferred: str | None = None) -> str:
        roots = self.bot.template_roots
        categories = list_monster_categories(roots.monster, roots.filter)
        current = self.bot.active_monster_category if preferred is None else preferred
        self._monster_category_lookup = {item.label: item.name for item in categories}
        values = list(self._monster_category_lookup)
        self.monster_category_combo.configure(values=values)
        selected = next((item for item in categories if item.name == current), categories[0])
        self.monster_category.set(selected.label)
        self.monster_category_summary.set(
            f"当前识别分类：怪物模板 {selected.monster_count}｜过滤项 {selected.filter_count}。"
            "只会识别这个分类。"
        )
        return selected.name

    def _selected_monster_category(self) -> str:
        label = self.monster_category.get()
        if label not in self._monster_category_lookup:
            raise RuntimeError("请先选择当前怪物识别分类")
        return self._monster_category_lookup[label]

    def _monster_category_changed(self, _event: tk.Event | None = None) -> None:
        if self.busy:
            self._refresh_monster_categories()
            return
        category = self._selected_monster_category()
        try:
            self._activate_monster_category(category)
            self._refresh_monster_categories(category)
        except Exception as exc:
            self._refresh_monster_categories()
            messagebox.showerror("切换怪物分类失败", str(exc), parent=self.root)

    def _activate_monster_category(self, category: str, *, notify: bool = True) -> None:
        selected = str(category).strip()
        config = load_config(self.config_path)
        config["vision"]["active_monster_category"] = selected
        save_config(self.config_path, config)
        self.bot.apply_config(config)
        self.bot.reload_templates()
        if notify:
            self.bot.notify(f"当前只识别怪物分类：{selected or UNCATEGORIZED_LABEL}", 4.0)

    def _add_monster_category(self) -> None:
        if self.busy:
            return
        created: list[str] = []

        def action() -> None:
            name = simpledialog.askstring("新建怪物分类", "输入分类名称：", parent=self.root)
            if name is None or not name.strip():
                raise RuntimeError("已取消新建怪物分类")
            roots = self.bot.template_roots
            created.append(
                create_monster_category(
                    name,
                    monster_root=roots.monster,
                    filter_root=roots.filter,
                )
            )

        self._run_tool("新建怪物分类", action, requires_window=False)
        if created:
            self._activate_monster_category(created[0])
            self._refresh_monster_categories(created[0])

    def _rename_monster_category(self) -> None:
        if self.busy:
            return
        category = self._selected_monster_category()
        if not category:
            messagebox.showinfo(
                "无法重命名",
                f'系统分类“{UNCATEGORIZED_LABEL}”用于兼容旧模板，不能重命名。',
                parent=self.root,
            )
            return
        renamed: list[str] = []

        def action() -> None:
            name = simpledialog.askstring(
                "重命名怪物分类",
                "输入新的分类名称：",
                initialvalue=category,
                parent=self.root,
            )
            if name is None or not name.strip():
                raise RuntimeError("已取消重命名怪物分类")
            roots = self.bot.template_roots
            renamed.append(
                rename_monster_category(
                    category,
                    name,
                    monster_root=roots.monster,
                    filter_root=roots.filter,
                )
            )

        self._run_tool("分类重命名", action, requires_window=False)
        if renamed:
            self._activate_monster_category(renamed[0])
            self._refresh_monster_categories(renamed[0])

    def _delete_monster_category(self) -> None:
        if self.busy:
            return
        category = self._selected_monster_category()
        if not category:
            messagebox.showinfo(
                "无法删除分类",
                f'系统分类“{UNCATEGORIZED_LABEL}”不能删除；可以点击“管理所有采集图片”删除其中的图片。',
                parent=self.root,
            )
            return
        def action() -> None:
            roots = self.bot.template_roots
            item = next(
                (
                    entry
                    for entry in list_monster_categories(roots.monster, roots.filter)
                    if entry.name == category
                ),
                None,
            )
            if item is None:
                raise RuntimeError(f'怪物分类“{category}”不存在')
            confirmed = messagebox.askyesno(
                "删除怪物分类",
                f'确定删除“{category}”吗？\n\n怪物模板 {item.monster_count} 个，过滤项 {item.filter_count} 个。'
                "文件会移入项目内的模板回收目录，可以手动恢复。",
                parent=self.root,
            )
            if not confirmed:
                raise RuntimeError("已取消删除怪物分类")
            trash_monster_category(
                category,
                monster_root=roots.monster,
                filter_root=roots.filter,
                trash_root=self.bot.template_trash_dir,
            )

        self._run_tool("分类删除", action, requires_window=False)

    def _manage_templates(self, family: str = "monster") -> None:
        if self.busy:
            return
        selected_family = "player" if family == "player" else "monster"
        category = self._selected_monster_category()
        self.busy = True
        dialog: tk.Toplevel | None = None
        try:
            self.overlay.hide()
            self.bot.suspend_vision()
            dialog = tk.Toplevel(self.root)
            dialog.title("管理所有采集图片")
            dialog.configure(bg=BG)
            dialog.geometry("980x560")
            dialog.minsize(900, 480)
            dialog.transient(self.root)
            dialog.wm_attributes("-topmost", True)
            dialog.update_idletasks()
            _exclude_from_capture(_top_level_hwnd(dialog))
            dialog.protocol("WM_DELETE_WINDOW", lambda: self._close_template_manager(dialog))
            self._build_template_manager(dialog, category, selected_family)
            dialog.grab_set()
            dialog.focus_force()
        except Exception as exc:
            self._close_template_manager(dialog)
            try:
                messagebox.showerror("打开模板管理失败", str(exc), parent=self.root)
            except tk.TclError:
                pass

    def _manage_monster_templates(self) -> None:
        """兼容旧入口；当前管理器已覆盖全部五类采集图片。"""
        self._manage_templates("monster")

    def _close_template_manager(self, dialog: tk.Toplevel | None) -> None:
        if dialog is not None and bool(getattr(dialog, "_mbv_closed", False)):
            return
        if dialog is not None:
            setattr(dialog, "_mbv_closed", True)
        try:
            if dialog is not None:
                try:
                    dialog.grab_release()
                except tk.TclError:
                    pass
                try:
                    dialog.destroy()
                except tk.TclError:
                    pass
        finally:
            try:
                self.bot.resume_vision()
            finally:
                try:
                    worker = getattr(self, "worker", None)
                    if worker is not None and worker.is_alive() and not self._stopping_session:
                        self.overlay.show()
                except (tk.TclError, RuntimeError):
                    pass
                finally:
                    self.busy = False
                    try:
                        if self.root.winfo_exists():
                            self.root.lift()
                            self.root.focus_force()
                    except tk.TclError:
                        pass

    def _build_template_manager(
        self,
        dialog: tk.Toplevel,
        category: str,
        initial_family: str = "monster",
    ) -> None:
        groups: dict[str, dict[str, Any]] = {}
        preview_photo: list[ImageTk.PhotoImage | None] = [None]
        active_kind: list[str | None] = [None]
        preview_title = tk.StringVar(value="选择左侧图片以预览")
        preview_info = tk.StringVar(value="")

        preview_card = RoundedCard(dialog, padding=6)
        preview_card.pack(side="right", fill="both", padx=(0, 8), pady=8)
        preview_frame = preview_card.body
        tk.Label(
            preview_frame,
            textvariable=preview_title,
            bg=PANEL,
            fg=FG,
            font=FONT_SECTION,
            wraplength=280,
            justify="center",
        ).pack(fill="x", padx=10, pady=(10, 4))
        preview_label = tk.Label(
            preview_frame,
            text="暂无预览",
            bg=ENTRY_BG,
            fg=MUTED,
            font=FONT,
            width=34,
            height=16,
            compound="center",
        )
        preview_label.pack(fill="both", expand=True, padx=10, pady=4)
        tk.Label(
            preview_frame,
            textvariable=preview_info,
            bg=PANEL,
            fg=MUTED,
            font=FONT_SMALL,
            wraplength=280,
            justify="center",
        ).pack(fill="x", padx=10, pady=(4, 10))

        def clear_preview(message: str = "选择左侧图片以预览") -> None:
            preview_photo[0] = None
            preview_title.set(message)
            preview_info.set("")
            preview_label.configure(image="", text="暂无预览")

        def show_preview(kind: str) -> None:
            group = groups[kind]
            indexes = group["listbox"].curselection()
            if not indexes:
                if active_kind[0] == kind:
                    active_kind[0] = None
                    clear_preview()
                return
            active_kind[0] = kind
            for other_kind, other_group in groups.items():
                if other_kind != kind:
                    other_group["listbox"].selection_clear(0, "end")
            template = group["items"][int(indexes[0])]
            try:
                source = template_preview_image(template.path)
                photo = ImageTk.PhotoImage(source, master=dialog)
                preview_photo[0] = photo
                preview_label.configure(image=photo, text="")
                preview_title.set(template.filename)
                with Image.open(template.path) as original:
                    width, height = original.size
                details = [TEMPLATE_GROUP_LABELS[kind]]
                if kind in {"monster", "filter"}:
                    details.append(f"分类 {group['category'] or UNCATEGORIZED_LABEL}")
                details.append(f"原图 {width}×{height}")
                preview_info.set("｜".join(details))
            except Exception as exc:
                clear_preview(f"无法预览：{template.filename}")
                preview_info.set(str(exc))

        def reload_lists(preferred: tuple[str, int] | None = None) -> None:
            active_kind[0] = None
            for kind, group in groups.items():
                listbox = group["listbox"]
                items = list_template_items(
                    kind,
                    group["category"],
                    roots=self.bot.template_roots,
                )
                group["items"] = items
                listbox.delete(0, "end")
                for template in items:
                    listbox.insert("end", template.filename)
                group["notebook"].tab(
                    group["frame"],
                    text=f"{TEMPLATE_GROUP_LABELS[kind]} ({len(items)})",
                )
            clear_preview()
            if preferred is not None:
                kind, index = preferred
                items = groups[kind]["items"]
                if items:
                    selected_index = min(index, len(items) - 1)
                    family_notebook.select(family_frames[groups[kind]["family"]])
                    groups[kind]["notebook"].select(groups[kind]["frame"])
                    groups[kind]["listbox"].selection_set(selected_index)
                    groups[kind]["listbox"].see(selected_index)
                    show_preview(kind)

        def capture_new(kind: str) -> None:
            self._close_template_manager(dialog)

            def capture_and_reopen() -> None:
                self._capture(kind)
                try:
                    if self.root.winfo_exists():
                        reopened_family = "monster" if kind in {"monster", "filter"} else "player"
                        self._manage_templates(reopened_family)
                except tk.TclError:
                    pass

            self.root.after_idle(capture_and_reopen)

        def delete_selected(kind: str) -> None:
            group = groups[kind]
            indexes = group["listbox"].curselection()
            if not indexes:
                messagebox.showinfo("删除采集图片", "请先选择一张图片。", parent=dialog)
                return
            index = int(indexes[0])
            template = group["items"][index]
            kind_label = TEMPLATE_GROUP_LABELS[kind]
            consequence = ""
            if kind == "player" and len(group["items"]) == 1:
                consequence = "\n\n这是最后一张姓名板；删除后必须重新采集才能启动挂机。"
            if not messagebox.askyesno(
                "删除采集图片",
                f"确定删除{kind_label} {template.filename} 吗？"
                f"{consequence}\n\n文件会移入模板回收目录，可以手动恢复。",
                parent=dialog,
            ):
                return
            try:
                trash_template(
                    kind,
                    template.filename,
                    group["category"],
                    roots=self.bot.template_roots,
                    trash_root=self.bot.template_trash_dir,
                )
            except Exception as exc:
                messagebox.showerror("删除采集图片失败", str(exc), parent=dialog)
                return

            try:
                refresh_errors: list[str] = []
                try:
                    self.bot.reload_templates()
                except Exception as exc:
                    refresh_errors.append(f"识别模板重载失败：{exc}")
                try:
                    self._refresh_counts()
                except Exception as exc:
                    refresh_errors.append(f"统计刷新失败：{exc}")
                try:
                    reload_lists((kind, index))
                except Exception as exc:
                    refresh_errors.append(f"列表刷新失败：{exc}")

                if refresh_errors:
                    messagebox.showwarning(
                        "图片已删除，但刷新未完成",
                        f"{template.filename} 已移入模板回收目录。\n\n" + "\n".join(refresh_errors),
                        parent=dialog,
                    )
                else:
                    self.bot.notify(f"已删除{kind_label}：{template.filename}", 3.0)
            finally:
                try:
                    if dialog.winfo_exists():
                        dialog.lift()
                        dialog.grab_set()
                except tk.TclError:
                    pass

        left_frame = tk.Frame(dialog, bg=BG)
        left_frame.pack(side="left", fill="both", expand=True, padx=8, pady=8)
        tk.Label(
            left_frame,
            text=f"怪物与过滤项使用当前分类：{category or UNCATEGORIZED_LABEL}",
            bg=BG,
            fg=MUTED,
            font=FONT_SMALL,
            anchor="w",
        ).pack(fill="x", pady=(0, 5))
        family_notebook = ttk.Notebook(left_frame)
        family_notebook.pack(fill="both", expand=True)
        family_frames = {
            "monster": tk.Frame(family_notebook, bg=PANEL),
            "player": tk.Frame(family_notebook, bg=PANEL),
        }
        family_notebook.add(family_frames["monster"], text="怪物模板")
        family_notebook.add(family_frames["player"], text="人物模板")
        family_notebooks: dict[str, ttk.Notebook] = {}
        for family_key, family_frame in family_frames.items():
            inner_notebook = ttk.Notebook(family_frame)
            inner_notebook.pack(fill="both", expand=True, padx=6, pady=6)
            family_notebooks[family_key] = inner_notebook

        for kind, title in TEMPLATE_GROUPS:
            family_key = "monster" if kind in {"monster", "filter"} else "player"
            notebook = family_notebooks[family_key]
            frame = tk.Frame(notebook, bg=PANEL)
            notebook.add(frame, text=title)
            list_wrap = tk.Frame(frame, bg=PANEL)
            list_wrap.pack(fill="both", expand=True, padx=8, pady=8)
            scrollbar = ttk.Scrollbar(list_wrap, orient="vertical")
            scrollbar.pack(side="right", fill="y")
            listbox = tk.Listbox(
                list_wrap,
                bg=ENTRY_BG,
                fg=FG,
                selectbackground=ACCENT_SOFT,
                selectforeground=FG,
                font=FONT_SMALL,
                activestyle="none",
                exportselection=False,
                yscrollcommand=scrollbar.set,
            )
            listbox.pack(side="left", fill="both", expand=True)
            scrollbar.configure(command=listbox.yview)
            groups[kind] = {
                "listbox": listbox,
                "items": [],
                "category": category if kind in {"monster", "filter"} else "",
                "frame": frame,
                "notebook": notebook,
                "family": family_key,
            }
            listbox.bind("<<ListboxSelect>>", lambda _event, selected_kind=kind: show_preview(selected_kind))
            buttons = tk.Frame(frame, bg=PANEL)
            buttons.pack(fill="x", padx=8, pady=(0, 8))
            RoundedButton(
                buttons,
                text="新增采集…",
                command=lambda selected_kind=kind: capture_new(selected_kind),
                bg=ACCENT,
                fg="white",
                activebackground=ACCENT_HOVER,
                activeforeground="white",
                relief="flat",
                font=FONT,
                cursor="hand2",
            ).pack(side="left", fill="x", expand=True, padx=(0, 4), ipady=3)
            RoundedButton(
                buttons,
                text="删除选中项",
                command=lambda selected_kind=kind: delete_selected(selected_kind),
                bg=BUTTON_BG,
                fg=ARMED,
                activebackground=BUTTON_ACTIVE,
                activeforeground=ARMED,
                relief="flat",
                font=FONT,
                cursor="hand2",
            ).pack(side="left", fill="x", expand=True, padx=(4, 0), ipady=3)

        def selected_tab_changed(notebook: ttk.Notebook) -> None:
            selected_frame = str(notebook.select())
            for kind, group in groups.items():
                if group["notebook"] is not notebook:
                    continue
                if str(group["frame"]) != selected_frame:
                    continue
                if group["listbox"].curselection():
                    show_preview(kind)
                else:
                    active_kind[0] = None
                    clear_preview(f"选择{TEMPLATE_GROUP_LABELS[kind]}图片以预览")
                break

        for notebook in family_notebooks.values():
            notebook.bind(
                "<<NotebookTabChanged>>",
                lambda _event, selected_notebook=notebook: selected_tab_changed(selected_notebook),
            )
        selected_family = "player" if initial_family == "player" else "monster"
        family_notebook.select(family_frames[selected_family])
        reload_lists()

    def _tick(self) -> None:
        if self._closing or not self._consume_worker_completion():
            return
        now = time.monotonic()
        armed = self.bot.armed
        state = STATE_LABELS.get(self.bot.state, self.bot.state)
        hp = self.bot.ui_hp
        mp = self.bot.ui_mp
        connected = self._selected_target is not None and not self._stopping_session
        if not connected:
            mode = "正在断开" if self._stopping_session else "等待选择窗口"
            color = MUTED
            self.arm_button.configure(text="启动挂机")
        elif armed:
            mode = "挂机中"
            color = FG
            self.arm_button.configure(text="暂停挂机")
        elif self.bot.input_authorized:
            mode = "输入待命"
            color = ACCENT
            self.arm_button.configure(text="启动挂机")
        else:
            mode = "按键未授权"
            color = ACCENT
            self.arm_button.configure(text="启动挂机")
        self.debug_boxes.set(self.bot.calibration_overlay_visible)
        hidden_items = getattr(self.bot, "calibration_overlay_hidden_items", frozenset())
        debug_mode = (
            "关"
            if not self.bot.calibration_overlay_visible
            else ("全部" if not hidden_items else "按需")
        )
        self.debug_button.configure(
            text=f"识别框：{debug_mode}",
            fg=ACCENT if self.bot.calibration_overlay_visible else MUTED,
        )
        self._refresh_debug_item_buttons()
        potion_enabled = self.bot.auto_potion.enabled
        potion_state = self.bot.auto_potion.display_state(now)
        self.auto_potion_enabled.set(potion_enabled)
        self.potion_button.configure(
            text=f"自动喝药：{potion_state}",
            fg=ACCENT if potion_enabled else FG,
        )
        self.arm_button.configure(state="normal" if connected else "disabled")
        self.potion_button.configure(state="normal" if connected else "disabled")
        notice = self.bot.notice if self.bot.notice and self.bot.notice_until >= now else ""
        text = f"{mode}｜{state}｜血 {hp:.0%} 蓝 {mp:.0%}"
        if potion_enabled:
            text += f"｜自动喝药 {potion_state}"
        if notice:
            text += f"\n{notice}"
        alert_status = getattr(self.bot, "verification_alert_status", "")
        if alert_status:
            text += f"\n{alert_status}"
        self.status.set(text)
        self.status_label.configure(fg=color)
        self.run_badge.set(mode if connected else "未连接")
        self._run_badge_label.configure(fg=SUCCESS if armed else ACCENT)
        self.run_metrics.set(
            f"{state}    ·    HP {hp:.0%}    /    MP {mp:.0%}" if connected
            else "请在“窗口连接”页选择目标，连接后手动启动。"
        )
        self.run_notice.set("\n".join(value for value in (notice, alert_status) if value))
        if self.run_notice.get():
            self._notice_label.pack(fill="x", padx=9, pady=(6, 0), before=self._quick_controls)
        else:
            self._notice_label.pack_forget()
        self.arm_button.configure(bg=ARMED if armed else ACCENT,
                                  highlightbackground=ARMED if armed else ACCENT,
                                  activebackground=DANGER_HOVER if armed else ACCENT_HOVER,
                                  fg=PANEL, activeforeground=PANEL)
        self.root.after(250, self._tick)

    def _run_tool(
        self, title: str, action: Callable[[], Any], *, requires_window: bool = True,
    ) -> None:
        if self.busy or (requires_window and not self._target_ready()):
            return
        self.busy = True
        self.overlay.hide()
        try:
            self.bot.suspend_vision()
            # 校准函数各自重新 load_config；作用窗口仅在本次调用链中精确传递。
            scope = selected_window(self._selected_target) if requires_window else nullcontext()
            with scope:
                action()
            self.bot.reload_from_disk(self.config_path)
            self._refresh_counts()
            self._load_entries(load_config(self.config_path))
            self.bot.notify(f"{title}完成", 4.0)
        except Exception as exc:
            message = str(exc)
            if "已取消" in message:
                self.bot.notify(message, 3.0)
            else:
                messagebox.showerror("冒险岛弓箭手", message)
                self.bot.notify(message, 5.0)
        finally:
            self.bot.resume_vision()
            if self.worker is not None and self.worker.is_alive() and not self._stopping_session:
                self.overlay.show()
            self.busy = False

    def _calibrate(self) -> None:
        self._run_tool("状态栏与小地图校准", lambda: calibrate(self.config_path, parent=self.root))

    def _capture_status_item(self, key: str, label: str) -> None:
        self._run_tool(
            f"{label}采集",
            lambda: capture_status_region(self.config_path, key, label, parent=self.root),
        )

    def _capture_player_marker_item(self) -> None:
        self._run_tool(
            "小地图玩家标记采集",
            lambda: capture_player_marker(self.config_path, parent=self.root),
        )

    def _capture_combat_region_item(self) -> None:
        self._run_tool(
            "战斗识别区域采集",
            lambda: capture_combat_region(self.config_path, parent=self.root),
        )

    def _capture_platform_center_item(self) -> None:
        self._run_tool(
            "小地图平台安全点采集",
            lambda: capture_platform_center(self.config_path, parent=self.root),
        )

    def _capture_recognition_region(self) -> None:
        self._run_tool(
            "识别区域与小地图平台安全点采集",
            lambda: capture_recognition_region(self.config_path, parent=self.root),
        )

    def _capture(self, kind: str) -> None:
        if kind == "monster":
            category = self._selected_monster_category()
            self._run_tool(
                "怪物采集",
                lambda: capture_template(self.config_path, parent=self.root, category=category),
            )
        elif kind == "filter":
            category = self._selected_monster_category()
            self._run_tool(
                "过滤项采集",
                lambda: capture_monster_filter(self.config_path, parent=self.root, category=category),
            )
        elif kind == "player":
            self._run_tool("姓名板采集", lambda: capture_player_template(self.config_path, parent=self.root))
        else:
            self._run_tool(
                "模板采集",
                lambda: capture_player_aux_template(self.config_path, kind, parent=self.root),
            )

    def _capture_key(self, dotted: str) -> None:
        def action() -> None:
            name = capture_key_name(load_config(self.config_path), parent=self.root)
            config = load_config(self.config_path)
            self._nested(config, dotted, name)
            save_config(self.config_path, config)

        self._run_tool("按键采集", action)

    def _capture_target_range(self) -> None:
        player_box = None
        raw_box = None
        player_track = getattr(self.bot, "player_track", None)
        anchor = player_track.anchor if player_track is not None else None
        if anchor is not None:
            player_box = anchor.box
            raw_box = anchor.raw_box
        def action() -> None:
            capture_target_range(
                self.config_path,
                parent=self.root,
                player_box=player_box,
                raw_box=raw_box,
                player_anchor=self.bot.last_attack_anchor,
                facing=self.bot.direction,
            )

        self._run_tool(
            "通用索敌范围框选",
            action,
        )

    def _capture_strategy_area(
        self,
        field: StrategyCaptureField,
        region_id: str | None = None,
    ) -> None:
        strategy = self._selected_strategy()
        player_box = None
        raw_box = None
        player_track = getattr(self.bot, "player_track", None)
        anchor = player_track.anchor if player_track is not None else None
        if anchor is not None:
            player_box = anchor.box
            raw_box = anchor.raw_box
        stable_anchor = self.bot.last_attack_anchor

        def action() -> None:
            if field.capture_kind == "point" and field.coordinate_space == "minimap":
                capture_minimap_point(self.config_path, field.recognition_key, field.prompt,
                                      parent=self.root)
            elif field.multiple and field.settings_path:
                capture_strategy_region(
                    self.config_path,
                    strategy.key,
                    field.settings_path,
                    field.recognition_key,
                    field.prompt,
                    parent=self.root,
                    region_id=region_id,
                    player_box=player_box,
                    raw_box=raw_box,
                    player_anchor=stable_anchor,
                )
            else:
                capture_strategy_area(
                    self.config_path,
                    field.recognition_key,
                    field.prompt,
                    parent=self.root,
                    coordinate_space=field.coordinate_space,
                )
            if field.enable_setting:
                config = load_config(self.config_path)
                self._nested(
                    config,
                    f"strategy.options.{strategy.key}.{field.enable_setting}",
                    True,
                )
                save_config(self.config_path, config)

        self._run_tool(
            field.button_label,
            action,
        )

    def _toggle_arm(self) -> None:
        if self.busy:
            return
        if self.bot.armed:
            self.bot.request_toggle()
            return
        if not self._target_ready():
            return
        if not self.bot.input_authorized:
            self.bot.notify("按键未授权，请从唯一入口 Start.bat 启动。", 5.0)
            return
        if not is_elevated() or not self.bot.integrity_ok:
            messagebox.showwarning(
                "冒险岛弓箭手",
                "当前进程没有足够的输入权限。请关闭程序后，从唯一入口 Start.bat 重新启动并在 UAC 中选择“是”。",
                parent=self.root,
            )
            return
        self.bot.request_toggle()

    def _toggle_debug_boxes(self) -> None:
        if self.busy:
            self.debug_boxes.set(self.bot.calibration_overlay_visible)
            return
        self.bot.set_calibration_overlay_visible(bool(self.debug_boxes.get()))
        self._refresh_debug_item_buttons()

    def _toggle_auto_potion(self) -> None:
        variable = getattr(self, "auto_potion_enabled", None)
        if variable is None:
            variable = self.standalone_potion
        if self.busy:
            variable.set(self.bot.auto_potion.enabled)
            return
        if not self._target_ready():
            variable.set(False)
            return
        request = getattr(self.bot, "request_auto_potion", None)
        if request is None:
            request = self.bot.request_standalone_potion
        request(bool(variable.get()))

    def _toggle_verification_alert(self) -> None:
        enabled = bool(self.verification_alert_enabled.get())
        try:
            self._preview_common_setting("verification_alert.enabled", enabled)
        except Exception as exc:
            self.verification_alert_enabled.set(not enabled)
            messagebox.showerror("验证提醒", f"保存失败：{exc}")

    def _test_verification_sound(self) -> None:
        from mbv.verification_alert import play_alert_sound
        try:
            play_alert_sound()
        except Exception as exc:
            messagebox.showerror("验证提醒", f"提示音播放失败：{exc}")

    def _toggle_standalone_potion(self) -> None:
        """兼容旧调用；开关现在控制全部自动喝药。"""
        self._toggle_auto_potion()

    def _save_settings(self) -> None:
        self._persist_settings(apply_runtime=True, notify=True, show_error=True)

    def _persist_settings(
        self,
        *,
        apply_runtime: bool,
        notify: bool,
        show_error: bool,
    ) -> bool:
        try:
            if self._autosave_after_id is not None:
                try:
                    self.root.after_cancel(self._autosave_after_id)
                except tk.TclError:
                    pass
                self._autosave_after_id = None
                self._autosave_reconfigure = False
            config = load_config(self.config_path)
            for key, entry in self._entries.items():
                raw = entry.get().strip()
                current = self._nested(config, key)
                if isinstance(current, (int, float)) and not isinstance(current, bool):
                    value: Any = float(raw)
                    if isinstance(current, int) and key.endswith("minutes"):
                        value = int(float(raw))
                else:
                    value = raw.lower()
                    is_buff_key = key.startswith("buffs.") and key.endswith(".key")
                    if key.startswith("keys.") or (is_buff_key and value):
                        vk_for(value)
                self._nested(config, key, value)
            for slot, variable in getattr(self, "buff_enabled", {}).items():
                self._nested(config, f"buffs.{slot}.enabled", bool(variable.get()))
            strategy = self._selected_strategy()
            config["strategy"]["active"] = strategy.key
            strategy_key_paths = {
                f"strategy.options.{strategy.key}.{field.path}"
                for field in strategy.setting_fields
                if field.capture_key
            }
            for key, entry in self._strategy_entries.items():
                raw = entry.get().strip()
                current = self._nested(config, key)
                if isinstance(current, (int, float)) and not isinstance(current, bool):
                    value = float(raw)
                else:
                    value = raw.lower() if key in strategy_key_paths else raw
                    if key in strategy_key_paths and value:
                        vk_for(value)
                self._nested(config, key, value)
            for key, variable in self._strategy_toggles.items():
                self._nested(config, key, bool(variable.get()))
            for key, (variable, choices) in self._strategy_choices.items():
                label = variable.get()
                if label in choices:
                    self._nested(config, key, choices[label])
            for key, entry in self._targeting_entries.items():
                raw = entry.get().strip()
                current = self._nested(config, key)
                value = float(raw) if isinstance(current, (int, float)) and not isinstance(current, bool) else raw
                self._nested(config, key, value)
            config.setdefault("input", {})
            config["input"]["delivery"] = next(
                key for key, label in DELIVERY_LABELS.items() if label == self.delivery.get()
            )
            config.setdefault("window", {})
            config["window"]["topmost_while_armed"] = bool(self.topmost_while_armed.get())
            performance_monitor = config.setdefault("performance_monitor", {})
            performance_visible = getattr(self, "performance_visible", None)
            if performance_visible is not None:
                performance_monitor["visible"] = bool(performance_visible.get())
            alert_enabled = getattr(self, "verification_alert_enabled", None)
            if alert_enabled is not None:
                config["verification_alert"]["enabled"] = bool(alert_enabled.get())
            interval_ms = int(getattr(self, "_performance_interval_ms", 1000))
            performance_monitor["refresh_interval_seconds"] = max(500, min(5000, interval_ms)) / 1000.0
            minimap_assist = getattr(self, "minimap_assist", None)
            if minimap_assist is not None:
                config["vision"]["player_minimap_assist_enabled"] = bool(minimap_assist.get())
            config["behavior"]["fallback_patrol"] = bool(self.fallback_patrol.get())
            config["behavior"]["pickup_after_target_lost"] = bool(self.pickup_lost.get())
            config["behavior"]["player_lost_recovery_enabled"] = bool(
                self.player_lost_recovery.get()
            )
            config["behavior"]["hp_threshold"] = max(0, min(100, int(self.hp_threshold_percent.get()))) / 100.0
            config["behavior"]["mp_threshold"] = max(0, min(100, int(self.mp_threshold_percent.get()))) / 100.0
            input_delivery(config)
            save_config(self.config_path, config)
            if apply_runtime:
                self.bot.apply_config(config)
            else:
                # 常规参数可无停机刷新；输入投递切换由 apply_runtime 路径重建键盘。
                for key in (
                    "behavior.hp_threshold",
                    "behavior.mp_threshold",
                    "behavior.fallback_patrol",
                    "behavior.pickup_after_target_lost",
                    "behavior.player_lost_recovery_enabled",
                    "window.topmost_while_armed",
                    "vision.player_minimap_assist_enabled",
                    "vision.player_minimap_occlusion_seconds",
                    "vision.player_minimap_navigation_seconds",
                    "verification_alert.enabled",
                ):
                    self.bot.preview_config_setting(key, self._nested(config, key))
                for slot in getattr(self, "buff_enabled", {}):
                    key = f"buffs.{slot}.enabled"
                    self.bot.preview_config_setting(key, self._nested(config, key))
            if notify:
                self.bot.notify("配置已保存", 3.0)
            return True
        except Exception as exc:
            if show_error:
                messagebox.showerror("冒险岛弓箭手", f"保存失败：{exc}")
            return False

    def quit(self) -> None:
        if getattr(self, "_closing", False):
            return
        if not self._persist_settings(apply_runtime=False, notify=False, show_error=True):
            return
        self._closing = True
        self._cancel_performance_refresh()
        if self.worker is not None:
            self._stop_session()
        else:
            self.bot.request_exit()
        self.overlay.close()
        self.root.after(200, self._destroy)

    def _destroy(self) -> None:
        self._cancel_performance_refresh()
        self._persist_settings(apply_runtime=False, notify=False, show_error=False)
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def mainloop(self) -> None:
        self.root.mainloop()
        if self.worker is not None:
            self.worker.join(timeout=3.0)


def run_control_panel(config_path: Path, enable_input: bool) -> int:
    panel = ControlPanel(config_path, enable_input=enable_input)
    panel.mainloop()
    return 0
