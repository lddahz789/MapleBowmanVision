from __future__ import annotations

import ctypes
from pathlib import Path
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox
from typing import Any, Callable

import maple_bowman as bot
from mbv.overlay import RuntimeOverlay, _exclude_from_capture, _top_level_hwnd, prevent_window_activate


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


def is_elevated() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_elevated(config_path: Path) -> bool:
    script = Path(bot.__file__).resolve()
    params = f'"{script}" --enable-input --config "{config_path}"'
    code = int(
        ctypes.windll.shell32.ShellExecuteW(
            None,
            "runas",
            sys.executable,
            params,
            str(script.parent),
            1,
        )
    )
    return code > 32


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

        config = bot.load_config(config_path)
        self.bot = bot.BowmanBot(config, input_authorized=enable_input)
        self.overlay = RuntimeOverlay(self.root)
        self.overlay.set_exit_handler(self.quit)
        self.worker_errors: list[BaseException] = []
        self.busy = False

        self.status = tk.StringVar(value="正在连接游戏窗口…")
        self.counts = tk.StringVar(value="")
        self.delivery = tk.BooleanVar(value=bot.input_delivery(config) == "background")
        self.fallback_patrol = tk.BooleanVar(value=bool(config["behavior"].get("fallback_patrol")))
        self.pickup_lost = tk.BooleanVar(value=bool(config["behavior"].get("pickup_after_target_lost")))
        self._entries: dict[str, tk.Entry] = {}

        self._build()
        self._refresh_counts()
        self._load_entries(config)

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
        self._row_button(capture, "画面校准", self._calibrate)
        self._row_button(capture, "采集怪物模板", lambda: self._capture("monster"))
        self._row_button(capture, "采集姓名板", lambda: self._capture("player"))
        self._row_button(capture, "采集头部", lambda: self._capture("head"))
        self._row_button(capture, "采集称号勋章", lambda: self._capture("title"))

        self._section("运行")
        run = self._last_body
        self.arm_button = self._row_button(run, "启动挂机", self._toggle_arm)
        self._row_button(run, "退出程序", self.quit)
        tk.Checkbutton(
            run,
            text="后台按键（始终扫描码；面板不抢游戏焦点）",
            variable=self.delivery,
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
        fields = [
            ("keys.attack", "攻击键"),
            ("keys.pickup", "拾取键"),
            ("keys.hp_potion", "HP 药键"),
            ("keys.mp_potion", "MP 药键"),
            ("behavior.hp_threshold", "HP 阈值 0–1"),
            ("behavior.mp_threshold", "MP 阈值 0–1"),
            ("behavior.bow_attack_box.forward", "攻击区前方"),
            ("behavior.bow_attack_box.back", "攻击区后方"),
            ("behavior.bow_attack_box.up", "攻击区上方"),
            ("behavior.bow_attack_box.down", "攻击区下方"),
            ("behavior.attack_interval_seconds", "攻击间隔秒"),
            ("behavior.max_runtime_minutes", "最长运行分钟，0=不限"),
            ("vision.monster_template_threshold", "怪物识别阈值"),
        ]
        for key, label in fields:
            self._labeled_entry(settings, key, label, capture=key.startswith("keys."))
        self._row_button(settings, "框选攻击范围", self._capture_attack_range)
        tk.Checkbutton(
            settings,
            text="没有目标时左右巡逻",
            variable=self.fallback_patrol,
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
            text="F7 显隐校对信息｜F8 启动/暂停｜F9 或 Ctrl+Shift+Q 退出。采集时请把游戏露出来。"
            "按键点「采集」或点输入框后，在游戏画面上按下要绑定的键。"
            "攻击范围可点「框选攻击范围」按角色中心和面向拖框，框多大就是攻击区多大。"
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

    def _labeled_entry(self, parent: tk.Misc, key: str, label: str, capture: bool = False) -> None:
        row = tk.Frame(parent, bg=PANEL)
        row.pack(fill="x", padx=8, pady=2)
        tk.Label(row, text=label, bg=PANEL, fg=MUTED, font=FONT_SMALL, width=18, anchor="w").pack(side="left")
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
        entry = tk.Entry(
            row,
            bg=ENTRY_BG,
            fg=FG,
            insertbackground=FG,
            relief="flat",
            font=FONT,
        )
        entry.pack(side="left", fill="x", expand=True, ipady=3)
        if capture:
            entry.bind("<Button-1>", lambda _event: self._capture_key(key))
        self._entries[key] = entry

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
        for key, entry in self._entries.items():
            value = self._nested(config, key)
            entry.delete(0, "end")
            entry.insert(0, str(value))
        self.delivery.set(bot.input_delivery(config) == "background")
        self.fallback_patrol.set(bool(config["behavior"].get("fallback_patrol")))
        self.pickup_lost.set(bool(config["behavior"].get("pickup_after_target_lost")))

    def _refresh_counts(self) -> None:
        counts = bot.template_counts()
        calibrated = "已校准" if bot.load_config(self.config_path).get("calibrated") else "未校准"
        self.counts.set(
            f"{calibrated}｜怪物 {counts['monster']}｜姓名板 {counts['player']}｜"
            f"头部 {counts['head']}｜称号 {counts['title']}"
        )

    def _tick(self) -> None:
        if self.worker_errors:
            return
        armed = self.bot.armed
        state = bot.STATE_LABELS.get(self.bot.state, self.bot.state)
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
            mode = "观察中"
            color = ACCENT
            self.arm_button.configure(text="启动挂机")
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
            self._load_entries(bot.load_config(self.config_path))
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
            self.busy = False
            self.root.lift()
            self.root.focus_force()

    def _calibrate(self) -> None:
        self._run_tool("画面校准", lambda: bot.calibrate(self.config_path, parent=self.root))

    def _capture(self, kind: str) -> None:
        if kind == "monster":
            self._run_tool("怪物采集", lambda: bot.capture_template(self.config_path, parent=self.root))
        elif kind == "player":
            self._run_tool("姓名板采集", lambda: bot.capture_player_template(self.config_path, parent=self.root))
        else:
            self._run_tool(
                "模板采集",
                lambda: bot.capture_player_aux_template(self.config_path, kind, parent=self.root),
            )

    def _capture_key(self, dotted: str) -> None:
        def action() -> None:
            name = bot.capture_key_name(bot.load_config(self.config_path), parent=self.root)
            config = bot.load_config(self.config_path)
            self._nested(config, dotted, name)
            bot.save_config(self.config_path, config)

        self._run_tool("按键采集", action)

    def _capture_attack_range(self) -> None:
        player_box = None
        raw_box = None
        anchor = self.bot.last_player_anchor
        if anchor is not None:
            player_box = anchor.box
            raw_box = anchor.raw_box
        self._run_tool(
            "攻击范围框选",
            lambda: bot.capture_attack_range(
                self.config_path,
                parent=self.root,
                player_box=player_box,
                raw_box=raw_box,
                facing=self.bot.direction,
            ),
        )

    def _toggle_arm(self) -> None:
        if self.busy:
            return
        if self.bot.armed:
            self.bot.request_toggle()
            return
        if not self.bot.input_authorized or not self.bot.integrity_ok:
            if is_elevated() and not self.bot.input_authorized:
                self.bot.input_authorized = True
                self.bot.request_toggle()
                return
            if not is_elevated():
                if messagebox.askyesno("冒险岛弓箭手", "启动挂机需要管理员权限，以便向游戏发送按键。现在弹出 UAC 吗？"):
                    if relaunch_elevated(self.config_path):
                        self.quit()
                    else:
                        messagebox.showwarning("冒险岛弓箭手", "没有获得管理员权限，仍保持观察模式。")
                return
        self.bot.request_toggle()

    def _save_settings(self) -> None:
        try:
            config = bot.load_config(self.config_path)
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
                        bot.vk_for(value)
                self._nested(config, key, value)
            config.setdefault("input", {})
            config["input"]["delivery"] = "background" if self.delivery.get() else "foreground"
            config["behavior"]["fallback_patrol"] = bool(self.fallback_patrol.get())
            config["behavior"]["pickup_after_target_lost"] = bool(self.pickup_lost.get())
            bot.input_delivery(config)
            bot.save_config(self.config_path, config)
            self.bot.apply_config(config)
            self.bot.notify("配置已保存", 3.0)
        except Exception as exc:
            messagebox.showerror("冒险岛弓箭手", f"保存失败：{exc}")

    def quit(self) -> None:
        self.bot.request_exit()
        self.overlay.close()
        self.root.after(200, self._destroy)

    def _destroy(self) -> None:
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
