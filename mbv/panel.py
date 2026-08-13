from __future__ import annotations

import ctypes
from pathlib import Path
import threading
import time
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from typing import Any, Callable

from PIL import Image, ImageTk

from mbv.bot import STATE_LABELS, BowmanBot
from mbv.calibrate import (
    calibrate,
    capture_target_range,
    capture_key_name,
    capture_monster_filter,
    capture_player_aux_template,
    capture_player_template,
    capture_recognition_region,
    capture_template,
)
from mbv.config import load_config, save_config, template_counts
from mbv.input import input_delivery, vk_for
from mbv.overlay import RuntimeOverlay, _exclude_from_capture, _top_level_hwnd, prevent_window_activate
from mbv.strategies import active_strategy, get_strategy, list_strategies
from mbv.template_store import (
    UNCATEGORIZED_LABEL,
    create_monster_category,
    list_monster_categories,
    list_template_items,
    rename_monster_category,
    trash_monster_category,
    trash_template,
)

BG = "#1b1b1b"
PANEL = "#242424"
ENTRY_BG = "#111111"
FG = "#f2f2f2"
MUTED = "#9a9a9a"
ACCENT = "#4cff79"
ARMED = "#ff4545"
BUTTON_BG = "#2f2f2f"
BUTTON_ACTIVE = "#3a3a3a"
FONT = ("Microsoft YaHei UI", 10)
FONT_TITLE = ("Microsoft YaHei UI", 14, "bold")
FONT_SECTION = ("Microsoft YaHei UI", 11, "bold")
FONT_SMALL = ("Microsoft YaHei UI", 9)
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


class ControlPanel:
    def __init__(self, config_path: Path, enable_input: bool) -> None:
        self.config_path = config_path
        self.root = tk.Tk()
        self.root.title("冒险岛弓箭手")
        self.root.configure(bg=BG)
        self.root.minsize(380, 720)
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        height = min(820, max(560, screen_h - 70))
        self.root.geometry(f"400x{height}+{max(40, screen_w - 430)}+20")
        self.root.wm_attributes("-topmost", True)
        self.root.update_idletasks()
        panel_hwnd = _top_level_hwnd(self.root)
        _exclude_from_capture(panel_hwnd)
        prevent_window_activate(panel_hwnd)

        config = load_config(config_path)
        self.bot = BowmanBot(config, input_authorized=enable_input)
        self.overlay = RuntimeOverlay(self.root)
        self.overlay.set_exit_handler(self.quit)
        self.worker_errors: list[BaseException] = []
        self.busy = False
        self._loading_settings = True
        self._autosave_after_id: str | None = None
        self._autosave_reconfigure = False

        self.status = tk.StringVar(value="正在连接游戏窗口…")
        self.counts = tk.StringVar(value="")
        self.monster_category = tk.StringVar(value=UNCATEGORIZED_LABEL)
        self.monster_category_summary = tk.StringVar(value="")
        self._monster_category_lookup: dict[str, str] = {UNCATEGORIZED_LABEL: ""}
        self.debug_boxes = tk.BooleanVar(value=self.bot.calibration_overlay_visible)
        self.hp_threshold_percent = tk.IntVar(value=int(round(float(config["behavior"]["hp_threshold"]) * 100)))
        self.mp_threshold_percent = tk.IntVar(value=int(round(float(config["behavior"]["mp_threshold"]) * 100)))
        self.delivery = tk.BooleanVar(value=input_delivery(config) == "background")
        self.fallback_patrol = tk.BooleanVar(value=bool(config["behavior"].get("fallback_patrol")))
        self.pickup_lost = tk.BooleanVar(value=bool(config["behavior"].get("pickup_after_target_lost")))
        selected_strategy = active_strategy(config)
        self.strategy_name = tk.StringVar(value=selected_strategy.display_name)
        self.strategy_description = tk.StringVar(value=selected_strategy.description)
        self._strategy_lookup = {item.display_name: item.key for item in list_strategies()}
        self._entries: dict[str, tk.Entry] = {}
        self._targeting_entries: dict[str, tk.Entry] = {}
        self._strategy_entries: dict[str, tk.Entry] = {}

        self._build()
        self._refresh_counts()
        self._load_entries(config)
        self._loading_settings = False

        self.worker = threading.Thread(target=self._run_bot, name="MapleVisionWorker", daemon=False)
        self.worker.start()
        self.overlay.start_polling()
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        self.root.after(250, self._tick)

    def _run_bot(self) -> None:
        try:
            self.bot.run(self.overlay)
        except BaseException as exc:
            self.worker_errors.append(exc)
            self.overlay.close()
            self.root.after(0, lambda: self._worker_failed(exc))

    def _worker_failed(self, exc: BaseException) -> None:
        messagebox.showerror("冒险岛弓箭手", f"视觉线程异常：{exc}")
        self.quit()

    def _build(self) -> None:
        outer = tk.Frame(self.root, bg=BG)
        outer.pack(fill="both", expand=True)
        canvas = tk.Canvas(outer, bg=BG, highlightthickness=0, bd=0)
        scroll = tk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        self._content = tk.Frame(canvas, bg=BG)
        content_id = canvas.create_window((0, 0), window=self._content, anchor="nw")

        def _sync(_event: tk.Event | None = None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfigure(content_id, width=canvas.winfo_width())

        self._content.bind("<Configure>", _sync)
        canvas.bind("<Configure>", _sync)

        def _on_wheel(event: tk.Event) -> None:
            canvas.yview_scroll(int(-event.delta / 120), "units")

        canvas.bind("<Enter>", lambda _event: canvas.bind_all("<MouseWheel>", _on_wheel))
        canvas.bind("<Leave>", lambda _event: canvas.unbind_all("<MouseWheel>"))

        pad = {"padx": 12, "pady": (10, 0)}
        tk.Label(self._content, text="冒险岛弓箭手", bg=BG, fg=FG, font=FONT_TITLE, anchor="w").pack(fill="x", **pad)
        self.status_label = tk.Label(
            self._content,
            textvariable=self.status,
            bg=BG,
            fg=ACCENT,
            font=FONT,
            anchor="w",
            wraplength=340,
            justify="left",
        )
        self.status_label.pack(fill="x", padx=12)
        tk.Label(
            self._content,
            textvariable=self.counts,
            bg=BG,
            fg=MUTED,
            font=FONT_SMALL,
            anchor="w",
            wraplength=340,
            justify="left",
        ).pack(fill="x", padx=12, pady=(0, 6))

        self._section("采集")
        capture = self._last_body
        self._row_button(capture, "校准状态栏与小地图", self._calibrate)
        self._row_button(capture, "采集识别区域与平台中心", self._capture_recognition_region)
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
        self.monster_category_combo.bind("<<ComboboxSelected>>", self._monster_category_changed)
        category_buttons = tk.Frame(capture, bg=PANEL)
        category_buttons.pack(fill="x", padx=8, pady=2)
        for label, command in (
            ("新建分类", self._add_monster_category),
            ("重命名", self._rename_monster_category),
            ("删除分类", self._delete_monster_category),
        ):
            tk.Button(
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
        self._row_button(capture, "采集怪物模板", lambda: self._capture("monster"))
        self._row_button(capture, "采集过滤项（排除误识别）", lambda: self._capture("filter"))
        self._row_button(capture, "管理所有采集图片…", self._manage_templates)
        tk.Label(
            capture,
            textvariable=self.monster_category_summary,
            bg=PANEL,
            fg=MUTED,
            font=FONT_SMALL,
            anchor="w",
            justify="left",
        ).pack(fill="x", padx=8, pady=(2, 5))
        self._row_button(capture, "采集姓名板", lambda: self._capture("player"))
        self._row_button(capture, "采集头部", lambda: self._capture("head"))
        self._row_button(capture, "采集称号勋章", lambda: self._capture("title"))

        self._section("运行")
        run = self._last_body
        self.debug_button = tk.Checkbutton(
            run,
            text="显示 Debug 框",
            variable=self.debug_boxes,
            command=self._toggle_debug_boxes,
            indicatoron=False,
            onvalue=True,
            offvalue=False,
            bg=BUTTON_BG,
            fg=ACCENT,
            selectcolor="#244b30",
            activebackground=BUTTON_ACTIVE,
            activeforeground=FG,
            relief="flat",
            font=FONT,
            cursor="hand2",
        )
        self.debug_button.pack(fill="x", padx=8, pady=3, ipady=4)
        self.arm_button = self._row_button(run, "启动挂机", self._toggle_arm)
        self._row_button(run, "退出程序", self.quit)
        tk.Checkbutton(
            run,
            text="后台按键（始终扫描码；面板不抢游戏焦点）",
            variable=self.delivery,
            command=lambda: self._schedule_settings_save(reconfigure=True),
            bg=PANEL,
            fg=FG,
            selectcolor=ENTRY_BG,
            activebackground=PANEL,
            activeforeground=FG,
            font=FONT,
            anchor="w",
        ).pack(fill="x", padx=8, pady=2)

        self._section("挂机配置")
        settings = self._last_body
        tk.Label(
            settings,
            text="通用索敌区",
            bg=PANEL,
            fg=FG,
            font=FONT_SECTION,
            anchor="w",
        ).pack(fill="x", padx=8, pady=(3, 2))
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
        self.capture_target_area_button = self._row_button(
            settings,
            "框选通用索敌范围",
            self._capture_target_range,
        )
        tk.Label(
            settings,
            text="职业策略",
            bg=PANEL,
            fg=FG,
            font=FONT_SECTION,
            anchor="w",
        ).pack(fill="x", padx=8, pady=(8, 2))
        strategy_row = tk.Frame(settings, bg=PANEL)
        strategy_row.pack(fill="x", padx=8, pady=(3, 2))
        tk.Label(
            strategy_row,
            text="职业策略",
            bg=PANEL,
            fg=MUTED,
            font=FONT_SMALL,
            width=18,
            anchor="w",
        ).pack(side="left")
        self.strategy_combo = ttk.Combobox(
            strategy_row,
            textvariable=self.strategy_name,
            state="readonly",
            values=list(self._strategy_lookup),
            font=FONT,
        )
        self.strategy_combo.pack(side="left", fill="x", expand=True)
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
        fields = [
            ("keys.attack", "攻击键", None, None, None),
            ("keys.pickup", "拾取键", None, None, None),
            ("keys.hp_potion", "HP 药键", None, None, None),
            ("keys.mp_potion", "MP 药键", None, None, None),
            ("behavior.attack_interval_seconds", "攻击间隔秒", 0.01, 0.01, 10.0),
            ("behavior.max_runtime_minutes", "最长运行分钟，0=不限", 1.0, 0.0, 10080.0),
            ("vision.monster_template_threshold", "怪物识别阈值", 0.01, 0.0, 1.0),
            ("vision.monster_filter_threshold", "过滤项识别阈值", 0.01, 0.0, 1.0),
        ]
        for key, label, step, minimum, maximum in fields:
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
        self._threshold_control(settings, self.hp_threshold_percent, "HP 自动喝药阈值")
        self._threshold_control(settings, self.mp_threshold_percent, "MP 自动喝药阈值")
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
        self._row_button(settings, "保存配置", self._save_settings)
        tk.Label(
            self._content,
            text="F7 显隐 Debug 框｜F8 启动/暂停｜F9 或 Ctrl+Shift+Q 退出。采集时请把游戏露出来。"
            "按键点「采集」或点输入框后，在游戏画面上按下要绑定的键。"
            "先分别采集状态栏/小地图和识别区域/平台中心。策略说明及专属参数会随下拉选项切换。"
            "通用索敌区供所有职业策略共享，可按角色位置和面向拖框。"
            "改完其它项后点「保存配置」。",
            bg=BG,
            fg=MUTED,
            font=FONT_SMALL,
            wraplength=340,
            justify="left",
            anchor="w",
        ).pack(fill="x", padx=12, pady=10)

    def _section(self, title: str) -> None:
        wrap = tk.Frame(self._content, bg=PANEL, highlightbackground="#333333", highlightthickness=1)
        wrap.pack(fill="x", padx=12, pady=(8, 0))
        tk.Label(wrap, text=title, bg=PANEL, fg=FG, font=FONT_SECTION, anchor="w").pack(fill="x", padx=8, pady=(6, 2))
        body = tk.Frame(wrap, bg=PANEL)
        body.pack(fill="x", padx=4, pady=(0, 8))
        self._last_body = body

    def _row_button(self, parent: tk.Misc, text: str, command: Callable[[], None]) -> tk.Button:
        button = tk.Button(
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
    ) -> None:
        row = tk.Frame(parent, bg=PANEL)
        row.pack(fill="x", padx=8, pady=2)
        tk.Label(row, text=label, bg=PANEL, fg=MUTED, font=FONT_SMALL, width=18, anchor="w").pack(side="left")
        entry = tk.Entry(
            row,
            bg=ENTRY_BG,
            fg=FG,
            insertbackground=FG,
            relief="flat",
            font=FONT,
        )
        if capture:
            tk.Button(
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
                width=6,
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

            tk.Button(
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
                width=3,
            ).pack(side="right", padx=(2, 0))
            tk.Button(
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
                width=3,
            ).pack(side="right", padx=(4, 0))
        entry.pack(side="left", fill="x", expand=True, ipady=3)
        if capture:
            entry.bind("<Button-1>", lambda _event: self._capture_key(key))
        (self._entries if entries is None else entries)[key] = entry

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
        tk.Button(
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
        tk.Button(
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
            self.strategy_name.set(strategy.display_name)
            self._render_strategy_settings(config)
            for key, entry in self._entries.items():
                value = self._nested(config, key)
                entry.delete(0, "end")
                entry.insert(0, str(value))
            for key, entry in self._strategy_entries.items():
                value = self._nested(config, key)
                entry.delete(0, "end")
                entry.insert(0, str(value))
            for key, entry in self._targeting_entries.items():
                value = self._nested(config, key)
                entry.delete(0, "end")
                entry.insert(0, str(value))
            self.delivery.set(input_delivery(config) == "background")
            self.hp_threshold_percent.set(int(round(float(config["behavior"]["hp_threshold"]) * 100)))
            self.mp_threshold_percent.set(int(round(float(config["behavior"]["mp_threshold"]) * 100)))
            self.fallback_patrol.set(bool(config["behavior"].get("fallback_patrol")))
            self.pickup_lost.set(bool(config["behavior"].get("pickup_after_target_lost")))
        finally:
            self._loading_settings = previous_loading

    def _selected_strategy(self):
        key = self._strategy_lookup.get(self.strategy_name.get())
        if key is None:
            raise RuntimeError("请选择职业策略")
        return get_strategy(key)

    def _render_strategy_settings(self, config: dict[str, Any]) -> None:
        for child in self.strategy_settings_body.winfo_children():
            child.destroy()
        self._strategy_entries.clear()
        strategy = self._selected_strategy()
        self.strategy_description.set(strategy.description)
        prefix = f"strategy.options.{strategy.key}."
        for field in strategy.setting_fields:
            self._labeled_entry(
                self.strategy_settings_body,
                prefix + field.path,
                field.label,
                entries=self._strategy_entries,
                adjust_step=field.step,
                minimum=field.minimum,
                maximum=field.maximum,
                on_adjust=lambda text, path=field.path: self._preview_strategy_setting(path, text),
            )

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

    def _preview_targeting_setting(self, path: str, text: str) -> None:
        value = float(text)
        config = load_config(self.config_path)
        self._nested(config, f"targeting.{path}", value)
        save_config(self.config_path, config)
        self.bot.preview_targeting_setting(path, value)

    def _preview_common_setting(self, path: str, text: str) -> None:
        config = load_config(self.config_path)
        current = self._nested(config, path)
        value: Any = float(text)
        if isinstance(current, int) and path.endswith("minutes"):
            value = int(float(text))
        self._nested(config, path, value)
        save_config(self.config_path, config)
        self.bot.preview_config_setting(path, value)

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
        counts = template_counts()
        config = load_config(self.config_path)
        calibration = config.get("calibration", {})
        status_ready = "状态区✓" if calibration.get("status_regions_complete") else "状态区待采"
        recognition_ready = "识别区✓" if calibration.get("recognition_region_complete") else "识别区待采"
        center_ready = "平台中心✓" if config["recognition"].get("platform_center_captured") else "平台中心默认值"
        self.counts.set(
            f"{status_ready}｜{recognition_ready}｜{center_ready}｜怪物 {counts['monster']}（{counts['category']} 类）｜"
            f"过滤 {counts['filter']}｜"
            f"姓名板 {counts['player']}｜"
            f"头部 {counts['head']}｜称号 {counts['title']}"
        )

    def _refresh_monster_categories(self, preferred: str | None = None) -> str:
        categories = list_monster_categories()
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
            created.append(create_monster_category(name))

        self._run_tool("新建怪物分类", action)
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
            renamed.append(rename_monster_category(category, name))

        self._run_tool("分类重命名", action)
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
            item = next((entry for entry in list_monster_categories() if entry.name == category), None)
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
            trash_monster_category(category)

        self._run_tool("分类删除", action)

    def _manage_templates(self) -> None:
        if self.busy:
            return
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
            self._build_template_manager(dialog, category)
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
        self._manage_templates()

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

    def _build_template_manager(self, dialog: tk.Toplevel, category: str) -> None:
        groups: dict[str, dict[str, Any]] = {}
        preview_photo: list[ImageTk.PhotoImage | None] = [None]
        active_kind: list[str | None] = [None]
        preview_title = tk.StringVar(value="选择左侧图片以预览")
        preview_info = tk.StringVar(value="")

        preview_frame = tk.Frame(dialog, bg=PANEL, highlightbackground="#333333", highlightthickness=1)
        preview_frame.pack(side="right", fill="both", padx=(0, 8), pady=8)
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
                items = list_template_items(kind, group["category"])
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
                        self._manage_templates()
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
                trash_template(kind, template.filename, group["category"])
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
        notebook = ttk.Notebook(left_frame)
        notebook.pack(fill="both", expand=True)

        for kind, title in TEMPLATE_GROUPS:
            frame = tk.Frame(notebook, bg=PANEL)
            notebook.add(frame, text=title)
            list_wrap = tk.Frame(frame, bg=PANEL)
            list_wrap.pack(fill="both", expand=True, padx=8, pady=8)
            scrollbar = tk.Scrollbar(list_wrap, orient="vertical")
            scrollbar.pack(side="right", fill="y")
            listbox = tk.Listbox(
                list_wrap,
                bg=ENTRY_BG,
                fg=FG,
                selectbackground="#365b43",
                selectforeground="white",
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
            }
            listbox.bind("<<ListboxSelect>>", lambda _event, selected_kind=kind: show_preview(selected_kind))
            buttons = tk.Frame(frame, bg=PANEL)
            buttons.pack(fill="x", padx=8, pady=(0, 8))
            tk.Button(
                buttons,
                text="新增采集…",
                command=lambda selected_kind=kind: capture_new(selected_kind),
                bg="#235b32",
                fg="white",
                activebackground="#2d7540",
                activeforeground="white",
                relief="flat",
                font=FONT,
                cursor="hand2",
            ).pack(side="left", fill="x", expand=True, padx=(0, 4), ipady=3)
            tk.Button(
                buttons,
                text="删除选中项",
                command=lambda selected_kind=kind: delete_selected(selected_kind),
                bg="#7a2020",
                fg="white",
                activebackground="#a52828",
                activeforeground="white",
                relief="flat",
                font=FONT,
                cursor="hand2",
            ).pack(side="left", fill="x", expand=True, padx=(4, 0), ipady=3)

        def selected_tab_changed(_event: tk.Event | None = None) -> None:
            selected_frame = str(notebook.select())
            for kind, group in groups.items():
                if str(group["frame"]) != selected_frame:
                    continue
                if group["listbox"].curselection():
                    show_preview(kind)
                else:
                    active_kind[0] = None
                    clear_preview(f"选择{TEMPLATE_GROUP_LABELS[kind]}图片以预览")
                break

        notebook.bind("<<NotebookTabChanged>>", selected_tab_changed)
        reload_lists()

    def _tick(self) -> None:
        if self.worker_errors:
            return
        armed = self.bot.armed
        state = STATE_LABELS.get(self.bot.state, self.bot.state)
        hp = self.bot.ui_hp
        mp = self.bot.ui_mp
        if armed:
            mode = "挂机中"
            color = ARMED
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
        self.debug_button.configure(
            text="显示 Debug 框：开" if self.bot.calibration_overlay_visible else "显示 Debug 框：关",
            fg=ACCENT if self.bot.calibration_overlay_visible else MUTED,
        )
        notice = self.bot.notice if self.bot.notice and self.bot.notice_until >= time.monotonic() else ""
        text = f"{mode}｜{state}｜血 {hp:.0%} 蓝 {mp:.0%}"
        if notice:
            text += f"\n{notice}"
        self.status.set(text)
        self.status_label.configure(fg=color)
        self.root.after(250, self._tick)

    def _run_tool(self, title: str, action: Callable[[], Any]) -> None:
        if self.busy:
            return
        self.busy = True
        self.overlay.hide()
        self.bot.suspend_vision()
        try:
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
            self.overlay.show()
            self.busy = False

    def _calibrate(self) -> None:
        self._run_tool("状态栏与小地图校准", lambda: calibrate(self.config_path, parent=self.root))

    def _capture_recognition_region(self) -> None:
        self._run_tool(
            "识别区域与平台中心采集",
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
        anchor = self.bot.last_player_anchor
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

    def _toggle_arm(self) -> None:
        if self.busy:
            return
        if self.bot.armed:
            self.bot.request_toggle()
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
                    if key.startswith("keys."):
                        vk_for(value)
                self._nested(config, key, value)
            strategy = self._selected_strategy()
            config["strategy"]["active"] = strategy.key
            for key, entry in self._strategy_entries.items():
                raw = entry.get().strip()
                current = self._nested(config, key)
                value = float(raw) if isinstance(current, (int, float)) and not isinstance(current, bool) else raw
                self._nested(config, key, value)
            for key, entry in self._targeting_entries.items():
                raw = entry.get().strip()
                current = self._nested(config, key)
                value = float(raw) if isinstance(current, (int, float)) and not isinstance(current, bool) else raw
                self._nested(config, key, value)
            config.setdefault("input", {})
            config["input"]["delivery"] = "background" if self.delivery.get() else "foreground"
            config["behavior"]["fallback_patrol"] = bool(self.fallback_patrol.get())
            config["behavior"]["pickup_after_target_lost"] = bool(self.pickup_lost.get())
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
                ):
                    self.bot.preview_config_setting(key, self._nested(config, key))
            if notify:
                self.bot.notify("配置已保存", 3.0)
            return True
        except Exception as exc:
            if show_error:
                messagebox.showerror("冒险岛弓箭手", f"保存失败：{exc}")
            return False

    def quit(self) -> None:
        if not self._persist_settings(apply_runtime=False, notify=False, show_error=True):
            return
        self.bot.request_exit()
        self.overlay.close()
        self.root.after(200, self._destroy)

    def _destroy(self) -> None:
        self._persist_settings(apply_runtime=False, notify=False, show_error=False)
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def mainloop(self) -> None:
        self.root.mainloop()
        self.worker.join(timeout=3.0)
        if self.worker_errors:
            raise self.worker_errors[0]


def run_control_panel(config_path: Path, enable_input: bool) -> int:
    panel = ControlPanel(config_path, enable_input=enable_input)
    panel.mainloop()
    return 0
