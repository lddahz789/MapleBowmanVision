from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import tkinter as tk
from tkinter import font as tkfont, ttk
import unittest
from unittest.mock import MagicMock, patch

from mbv.config import load_config, save_config
from mbv import panel_theme
from mbv.panel import ControlPanel
from mbv.template_store import UNCATEGORIZED_LABEL
from mbv.window import WindowTarget


ROOT = Path(__file__).resolve().parents[1]


class PanelLayoutTests(unittest.TestCase):
    """只构造隐藏 Tk 控件；窗口枚举、模板读取、HUD 和全部键盘通道隔离。"""

    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        temporary = self.stack.enter_context(TemporaryDirectory())
        self.config_path = Path(temporary) / "config.json"
        config = load_config(ROOT / "profiles" / "newmaple" / "config.example.json")
        config["strategy"]["active"] = "bowman_dynamic"
        save_config(self.config_path, config)
        self.root = tk.Tk()
        self.root.withdraw()
        self.tk_callback_errors: list[tuple[str, str]] = []
        self.root.report_callback_exception = lambda kind, error, _traceback: self.tk_callback_errors.append(
            (kind.__name__, str(error))
        )
        self.addCleanup(lambda: self.assertEqual(self.tk_callback_errors, []))
        self.addCleanup(self._destroy_root)
        # 构造时确定隐藏窗口尺寸；withdrawn 窗口后续 wm geometry 不一定重新排版。
        self.stack.enter_context(patch.object(self.root, "winfo_screenwidth", return_value=640))
        self.stack.enter_context(patch.object(self.root, "winfo_screenheight", return_value=820))
        self.stack.enter_context(patch("mbv.panel.tk.Tk", return_value=self.root))
        self.app_identity = self.stack.enter_context(patch("mbv.panel.configure_app_identity", return_value=True))
        self.stack.enter_context(patch("mbv.panel._top_level_hwnd", return_value=0))
        self.stack.enter_context(patch("mbv.panel._exclude_from_capture"))
        self.stack.enter_context(patch("mbv.panel.prevent_window_activate"))
        self.stack.enter_context(patch("mbv.panel.RuntimeOverlay"))
        self.stack.enter_context(patch("mbv.panel.window_candidates", return_value=[]))
        self.stack.enter_context(patch("mbv.bot.load_templates", return_value=[]))
        self.keyboard = self.stack.enter_context(patch("mbv.bot.Keyboard")).return_value
        self.stack.enter_context(patch("mbv.bot.SessionLog"))
        self.run = self.stack.enter_context(patch("mbv.bot.BowmanBot.run"))
        category = SimpleNamespace(name="", label=UNCATEGORIZED_LABEL, monster_count=0, filter_count=0)
        self.stack.enter_context(patch("mbv.panel.list_monster_categories", return_value=[category]))
        self.stack.enter_context(patch("mbv.panel.template_counts", return_value={
            "monster": 0, "filter": 0, "category": 1, "player": 0, "head": 0, "title": 0,
        }))
        self.errors = self.stack.enter_context(patch("mbv.panel.messagebox.showerror"))
        self.panel = ControlPanel(self.config_path, enable_input=False)
        self.panel._cancel_performance_refresh()
        self.root.update_idletasks()

    def _destroy_root(self) -> None:
        # 不留下指向已销毁 Tk 解释器的 after 回调，也不调用真实退出/输入逻辑。
        # 这里只取消调度；各子控件 destroy 时自行删除它注册的 Tcl command。
        for after_id in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.tk.call("after", "cancel", after_id)
        self.root.destroy()

    @staticmethod
    def descendants(parent: tk.Misc) -> list[tk.Misc]:
        result: list[tk.Misc] = []
        for child in parent.winfo_children():
            result.append(child)
            result.extend(PanelLayoutTests.descendants(child))
        return result

    @staticmethod
    def belongs_to(widget: tk.Misc, ancestor: tk.Misc) -> bool:
        while widget is not None:
            if widget is ancestor:
                return True
            widget = getattr(widget, "master", None)
        return False

    def test_panel_installs_its_own_icon_without_connecting_or_starting(self) -> None:
        self.app_identity.assert_called_once_with()
        icon = self.root._mbv_app_icon
        self.assertEqual((icon.width(), icon.height()), (63, 58))
        self.assertIn(str(icon), self.root.tk.call("image", "names"))
        self.assertEqual(self.root.state(), "withdrawn")
        self.assertIsNone(self.panel._selected_target)
        self.run.assert_not_called()

    def test_optional_skill_clear_button_saves_reloads_and_survives_exit_save(self) -> None:
        panel = self.panel
        for strategy_key in ("stationary_attack", "bowman_dynamic", "dragon_roar"):
            with self.subTest(strategy=strategy_key):
                config = load_config(self.config_path)
                field = "skill_key" if strategy_key == "dragon_roar" else "melee_skill_key"
                config["strategy"]["active"] = strategy_key
                config["strategy"]["options"][strategy_key][field] = "f"
                save_config(self.config_path, config)
                panel._load_entries(config)
                panel.bot.apply_config(config)
                dotted = f"strategy.options.{strategy_key}.{field}"
                entry = panel._strategy_entries[dotted]
                clear = next(child for child in entry.master.winfo_children()
                             if isinstance(child, tk.Button) and child.cget("text") == "清空")
                with patch("mbv.panel.capture_key_name") as capture, \
                        patch.object(panel, "_target_ready", return_value=False) as target:
                    clear.invoke()
                    capture.assert_not_called()
                    target.assert_not_called()
                self.assertEqual(load_config(self.config_path)["strategy"]["options"][strategy_key][field], "")
                self.assertEqual(panel.bot.config["strategy"]["options"][strategy_key][field], "")
                self.assertEqual(panel._strategy_entries[dotted].get(), "")
                self.assertTrue(panel._persist_settings(apply_runtime=False, notify=False, show_error=True))
                loaded = load_config(self.config_path)
                self.assertEqual(loaded["strategy"]["options"][strategy_key][field], "")
                self.assertEqual(loaded["keys"], config["keys"])
                self.assertEqual(loaded["buffs"], config["buffs"])
        self.errors.assert_not_called()
        self.run.assert_not_called()

    def test_required_keys_cannot_be_cleared_by_strategy_action(self) -> None:
        with patch.object(self.panel, "_run_tool") as run_tool:
            self.panel._clear_strategy_key("keys.attack")
            run_tool.assert_not_called()

    def test_arrow_rain_is_selectable_with_independent_stationary_options(self) -> None:
        panel = self.panel
        config = load_config(self.config_path)
        config["strategy"]["active"] = "bowman_arrow_rain"
        config["strategy"]["options"]["stationary_attack"]["melee_skill_key"] = "q"
        save_config(self.config_path, config)
        panel._load_entries(config)
        self.assertEqual(panel.profession_name.get(), "弓箭手")
        self.assertEqual(panel.strategy_name.get(), "箭雨")
        self.assertIn("箭雨", panel.strategy_combo.cget("values"))
        self.assertIn("弓箭手动态", panel.strategy_combo.cget("values"))
        path = "strategy.options.bowman_arrow_rain.melee_skill_key"
        panel._strategy_entries[path].insert(0, "f")
        range_entry = panel._strategy_entries["strategy.options.bowman_arrow_rain.attack_range_px"]
        self.assertEqual(float(range_entry.get()), 300.)
        range_entry.delete(0, "end")
        range_entry.insert(0, "280")
        self.assertTrue(panel._persist_settings(apply_runtime=False, notify=False, show_error=True))
        saved = load_config(self.config_path)
        self.assertEqual(saved["strategy"]["active"], "bowman_arrow_rain")
        self.assertEqual(saved["strategy"]["options"]["bowman_arrow_rain"]["melee_skill_key"], "f")
        self.assertEqual(saved["strategy"]["options"]["bowman_arrow_rain"]["attack_range_px"], 280.)
        self.assertEqual(saved["strategy"]["options"]["stationary_attack"]["melee_skill_key"], "q")
        self.errors.assert_not_called()

    def test_arrow_rain_periodic_step_toggle_hot_saves_without_pausing(self) -> None:
        panel = self.panel
        config = load_config(self.config_path)
        config["strategy"]["active"] = "bowman_arrow_rain"
        save_config(self.config_path, config)
        panel.bot.apply_config(config)
        panel._load_entries(config)
        path = "strategy.options.bowman_arrow_rain.periodic_step_enabled"
        variable = panel._strategy_toggles[path]
        self.assertTrue(variable.get())
        button = next(child for child in panel.strategy_settings_body.winfo_children()
                      if isinstance(child, tk.Checkbutton) and child.cget("text") == "定时向右小步")
        with patch.object(panel.bot, "apply_config") as apply, patch.object(panel.bot, "disarm") as disarm:
            button.invoke()
            self.assertFalse(variable.get())
            self.assertFalse(panel.bot.config["strategy"]["options"]["bowman_arrow_rain"]["periodic_step_enabled"])
            self.assertTrue(panel._persist_settings(apply_runtime=False, notify=False, show_error=True))
            saved = load_config(self.config_path)
            self.assertFalse(saved["strategy"]["options"]["bowman_arrow_rain"]["periodic_step_enabled"])
            panel._load_entries(saved)
            self.assertFalse(panel._strategy_toggles[path].get())
            apply.assert_not_called()
            disarm.assert_not_called()
        self.errors.assert_not_called()

    def test_function_pages_have_separate_scroll_bodies_and_correct_controls(self) -> None:
        panel = self.panel
        panel._fit_page_tabs(SimpleNamespace(width=700))
        self.assertEqual([panel.notebook.tab(tab, "text") for tab in panel.notebook.tabs()],
                         ["窗口连接", "区域校准", "模板采集", "职业策略", "按键补给", "运行设置", "性能监控"])
        self.assertEqual(set(panel._page_bodies), {"connection", "capture", "templates", "strategy", "supply", "runtime", "performance"})
        self.assertEqual(len({id(canvas) for canvas in panel._page_canvases.values()}), 7)
        self.assertIs(panel._capture_canvas, panel._page_canvases["capture"])
        self.assertIs(panel._content, panel._page_bodies["capture"])
        controls = (
            (panel.window_combo, "connection"),
            (panel.window_refresh_button, "connection"),
            (panel.window_connect_button, "connection"),
            (panel.window_disconnect_button, "connection"),
            (panel._capture_buttons["hp_bar"], "capture"),
            (panel._capture_buttons["player"], "templates"),
            (panel.monster_category_combo, "templates"),
            (panel.strategy_settings_body, "strategy"),
            (panel._targeting_entries["targeting.box.forward"], "strategy"),
            (panel._entries["keys.attack"], "supply"),
            (panel._entries["buffs.buff_1.key"], "supply"),
            (panel._entries["behavior.attack_interval_seconds"], "runtime"),
            (panel._performance_shell, "performance"),
        )
        for widget, page in controls:
            with self.subTest(widget=str(widget), page=page):
                self.assertTrue(self.belongs_to(widget, panel._page_bodies[page]))
        self.assertEqual(self.root.state(), "withdrawn")
        self.run.assert_not_called()

    def test_minimum_window_leaves_room_for_content_and_all_horizontal_tabs(self) -> None:
        panel = self.panel
        panel._fit_page_tabs(SimpleNamespace(width=540))
        self.root.update_idletasks()
        self.assertEqual((self.root.winfo_width(), self.root.winfo_height()), (560, 720))
        self.assertEqual(self.root.state(), "withdrawn")
        # 字体/DPI 会改变完整页签的需求宽度，540 px 不一定需要短标题。
        expected_titles = (["窗口连接", "校准", "模板", "策略", "补给", "设置", "性能"]
                           if 540 < panel._full_tabs_width else
                           ["窗口连接", "区域校准", "模板采集", "职业策略", "按键补给", "运行设置", "性能监控"])
        self.assertEqual([panel.notebook.tab(tab, "text") for tab in panel.notebook.tabs()],
                         expected_titles)
        self.assertLessEqual(panel.notebook.winfo_reqwidth(), 540)
        self.assertLessEqual(panel._run_bar.winfo_reqwidth(), 540)
        self.assertLessEqual(panel._run_bar.winfo_reqheight(), 115)
        self.assertGreaterEqual(panel.notebook.winfo_height(), 480)
        self.assertLessEqual(panel._quick_controls.winfo_reqwidth() + 20, 540)
        selected = panel.notebook.select()
        panel._fit_page_tabs(SimpleNamespace(width=panel._full_tabs_width - 1))
        self.assertEqual([panel.notebook.tab(tab, "text") for tab in panel.notebook.tabs()],
                         ["窗口连接", "校准", "模板", "策略", "补给", "设置", "性能"])
        panel._fit_page_tabs(SimpleNamespace(width=700))
        self.assertEqual(panel.notebook.tab(panel._page_canvases["performance"].master, "text"), "性能监控")
        self.assertEqual(panel.notebook.select(), selected)
        self.run.assert_not_called()

    def test_card_bodies_and_action_roles_use_shared_theme_tokens(self) -> None:
        panel = self.panel
        self.assertEqual(self.root.cget("bg"), panel_theme.BG)
        for control in (panel.window_combo, panel._capture_buttons["hp_bar"], panel.potion_button):
            ancestor = control.master
            while ancestor is not None and type(ancestor).__name__ != "RoundedCard":
                ancestor = getattr(ancestor, "master", None)
            with self.subTest(control=str(control)):
                self.assertIsNotNone(ancestor)
                self.assertTrue(self.belongs_to(control, ancestor.body))
                self.assertEqual(ancestor.body.cget("bg"), panel_theme.PANEL)
        self.assertEqual(panel.arm_button.cget("bg"), panel_theme.ACCENT)
        self.assertEqual(panel.arm_button.cget("fg"), panel_theme.PANEL)
        for button in (panel.save_button, panel.exit_button, panel.window_refresh_button):
            with self.subTest(button=str(button)):
                self.assertIsInstance(button, tk.Button)
                self.assertEqual(button.cget("bg"), panel_theme.BUTTON_BG)
                self.assertEqual(button.cget("fg"), panel_theme.FG)
        style = ttk.Style(self.root)
        notebook_style = str(panel.notebook.cget("style"))
        self.assertEqual(style.lookup(notebook_style + ".Tab", "background", ("selected",)), panel_theme.PANEL)
        self.assertEqual(style.lookup("TCombobox", "fieldbackground", ("readonly",)), panel_theme.ENTRY_BG)
        self.run.assert_not_called()

    def test_minimum_width_chinese_actions_remain_legible_inside_cards(self) -> None:
        panel = self.panel
        pages = {
            "connection": (panel.window_refresh_button, panel.window_connect_button, panel.window_disconnect_button),
            "capture": (panel._capture_buttons["hp_bar"], panel._capture_buttons["combat_region"]),
            "templates": (panel._capture_buttons["player"],),
        }
        for page, buttons in pages.items():
            panel.notebook.select(panel._page_canvases[page].master)
            self.root.update_idletasks()
            for button in buttons:
                font = tkfont.Font(self.root, font=button.cget("font"))
                with self.subTest(page=page, text=button.cget("text")):
                    self.assertGreaterEqual(button.winfo_width(), font.measure(button.cget("text")) + 8)
                    self.assertGreaterEqual(button.winfo_height(), font.metrics("linespace") + 4)
                    self.assertLessEqual(button.winfo_x() + button.winfo_width(), button.master.winfo_width())
        for button in (panel.arm_button, panel.save_button, panel.exit_button):
            font = tkfont.Font(self.root, font=button.cget("font"))
            with self.subTest(fixed_button=button.cget("text")):
                self.assertGreaterEqual(button.winfo_width(), font.measure(button.cget("text")) + 8)
                self.assertLessEqual(button.winfo_x() + button.winfo_width(), button.master.winfo_width())
        self.assertEqual(self.root.state(), "withdrawn")
        self.run.assert_not_called()

    def test_action_styling_and_disabled_invoke_preserve_native_button_semantics(self) -> None:
        panel = self.panel
        command = MagicMock()
        panel.arm_button.configure(command=command)
        self.keyboard.reset_mock()
        original_bot = panel.bot
        with patch("mbv.panel.BowmanBot") as create, patch.object(original_bot, "apply_config") as apply:
            panel._tick()
            self.root.update_idletasks()
            self.assertEqual(str(panel.arm_button.cget("state")), "disabled")
            panel.arm_button.invoke()
            command.assert_not_called()
            panel.arm_button.configure(state="normal", bg=panel_theme.ACCENT, fg=panel_theme.PANEL)
            self.root.update_idletasks()
            command.assert_not_called()
            panel.arm_button.invoke()
            command.assert_called_once_with()
            command.reset_mock()
            panel._selected_target = WindowTarget(1001, 2002, "测试窗口", "test.exe")
            panel.worker = MagicMock()
            panel.worker.is_alive.return_value = True
            original_bot.armed = True
            panel._tick()
            self.root.update_idletasks()
            self.assertEqual(panel.arm_button.cget("bg"), panel_theme.ARMED)
            self.assertEqual(panel.arm_button.cget("highlightbackground"), panel_theme.ARMED)
            original_bot.armed = False
            panel._tick()
            self.root.update_idletasks()
            self.assertEqual(panel.arm_button.cget("bg"), panel_theme.ACCENT)
            self.assertEqual(panel.arm_button.cget("highlightbackground"), panel_theme.ACCENT)
            command.assert_not_called()
            self.assertIs(panel.bot, original_bot)
            create.assert_not_called()
            apply.assert_not_called()
        self.assertEqual(self.keyboard.mock_calls, [])
        self.run.assert_not_called()

    def test_waiting_or_unavailable_potion_state_keeps_global_verification_toggle_visible(self) -> None:
        panel = self.panel
        panel._selected_target = WindowTarget(1001, 2002, "测试窗口", "test.exe")
        panel.worker = MagicMock()
        panel.worker.is_alive.return_value = True
        potion = panel.bot.auto_potion
        potion.enabled = True
        self.keyboard.reset_mock()
        for waiting, reason in ((True, ""), (False, "窗口不可用"), (False, "权限不足")):
            potion.waiting_foreground = waiting
            potion.unavailable_reason = reason
            panel._tick()
            self.root.update_idletasks()
            with self.subTest(waiting=waiting, reason=reason):
                expected = "等待游戏前台" if waiting else reason
                self.assertIn(expected, panel.potion_button.cget("text"))
                self.assertLessEqual(panel._quick_controls.winfo_reqwidth(), panel._quick_controls.winfo_width())
                previous_right = 0
                for button in (panel.debug_button, panel.potion_button, panel.verification_alert_button):
                    self.assertTrue(button.winfo_manager())
                    self.assertGreaterEqual(button.winfo_width(), button.winfo_reqwidth())
                    self.assertGreaterEqual(button.winfo_x(), previous_right)
                    previous_right = button.winfo_x() + button.winfo_width()
                    self.assertLessEqual(previous_right, panel._quick_controls.winfo_width())
                self.assertGreaterEqual(panel.notebook.winfo_height(), 480)
        self.assertEqual(self.root.state(), "withdrawn")
        self.assertEqual(self.keyboard.mock_calls, [])
        self.run.assert_not_called()

    def test_dragon_roar_controls_stay_in_strategy_page_and_save_across_tabs(self) -> None:
        panel = self.panel
        original_bot = panel.bot
        config = load_config(self.config_path)
        config["strategy"]["active"] = "dragon_roar"
        panel._load_entries(config)
        panel.notebook.select(panel._page_canvases["strategy"].master)
        self.root.update_idletasks()
        self.assertEqual(panel.profession_name.get(), "战士·龙骑士")
        self.assertEqual(panel.strategy_name.get(), "龙咆哮·定点")
        fields = {
            "strategy.options.dragon_roar.skill_key": "X",
            "strategy.options.dragon_roar.monster_count_threshold": "4",
            "strategy.options.dragon_roar.cast_interval_seconds": "1.7",
        }
        for path, value in fields.items():
            entry = panel._strategy_entries[path]
            self.assertTrue(self.belongs_to(entry, panel._page_bodies["strategy"]))
            entry.delete(0, "end")
            entry.insert(0, value)
        for path, entry in panel._strategy_entries.items():
            with self.subTest(entry=path):
                self.assertGreaterEqual(entry.winfo_width(), 100)
                self.assertGreater(entry.winfo_height(), 20)
        buttons = [widget for widget in self.descendants(panel.strategy_settings_body)
                   if isinstance(widget, tk.Button)]
        for title in ("采集龙咆哮定点", "新增龙咆哮攻击范围"):
            self.assertTrue(any(str(button.cget("text")).startswith(title) for button in buttons))
        for path in ("return_tolerance_x", "monster_count_threshold"):
            entry = panel._strategy_entries["strategy.options.dragon_roar." + path]
            label = next(widget for widget in entry.master.winfo_children() if isinstance(widget, tk.Label))
            self.assertGreater(int(label.cget("wraplength")), 0)
            self.assertTrue(label.bind("<Configure>"))
            self.assertLessEqual(int(label.cget("wraplength")), label.winfo_reqwidth())
        with patch("mbv.panel.BowmanBot") as create, patch.object(original_bot, "apply_config") as apply:
            panel.notebook.select(panel._page_canvases["connection"].master)
            self.assertTrue(panel._persist_settings(apply_runtime=False, notify=False, show_error=True))
            saved = load_config(self.config_path)
            self.assertEqual(saved["strategy"]["active"], "dragon_roar")
            self.assertEqual(saved["strategy"]["options"]["dragon_roar"]["skill_key"], "x")
            self.assertEqual(saved["strategy"]["options"]["dragon_roar"]["monster_count_threshold"], 4)
            self.assertEqual(saved["strategy"]["options"]["dragon_roar"]["cast_interval_seconds"], 1.7)
            self.assertIs(panel.bot, original_bot)
            create.assert_not_called()
            apply.assert_not_called()
        self.run.assert_not_called()

    def test_run_controls_and_verification_toggle_stay_outside_scrolling_pages(self) -> None:
        panel = self.panel
        for widget in (panel.arm_button, panel.potion_button, panel.debug_button,
                       panel.status_label, panel.verification_alert_button,
                       panel.save_button, panel.exit_button):
            with self.subTest(widget=str(widget)):
                self.assertFalse(any(self.belongs_to(widget, body) for body in panel._page_bodies.values()))
                self.assertTrue(widget.winfo_manager())
        self.assertIs(panel.potion_button.master, panel._quick_controls)
        self.assertIs(panel.verification_alert_button.master, panel._quick_controls)
        self.assertFalse(any(self.belongs_to(panel._notice_label, body) for body in panel._page_bodies.values()))
        self.assertEqual(panel._notice_label.winfo_manager(), "")

    def test_first_page_is_connection_and_switching_or_refreshing_never_rebuilds_bot(self) -> None:
        panel = self.panel
        self.assertEqual(panel.notebook.tab(panel.notebook.select(), "text"), "窗口连接")
        original_bot = panel.bot
        with patch("mbv.panel.BowmanBot") as create, patch.object(panel.bot, "apply_config") as apply, \
                patch.object(panel.bot, "request_toggle") as toggle:
            for name, canvas in panel._page_canvases.items():
                panel.notebook.select(canvas.master)
                selected = panel.notebook.select()
                panel._refresh_windows()
                self.root.update_idletasks()
                with self.subTest(page=name):
                    self.assertEqual(panel.notebook.select(), selected)
                    self.assertIs(panel.bot, original_bot)
            create.assert_not_called()
            apply.assert_not_called()
            toggle.assert_not_called()
        self.run.assert_not_called()

    def test_global_verification_toggle_hot_updates_on_every_page_without_interrupting(self) -> None:
        panel = self.panel
        original_bot = panel.bot
        original_bot.armed = True
        checks = [widget for widget in self.descendants(self.root)
                  if isinstance(widget, tk.Checkbutton)
                  and str(widget.cget("variable")) == str(panel.verification_alert_enabled)]
        self.assertEqual(checks, [panel.verification_alert_button])
        expected = bool(panel.verification_alert_enabled.get())
        self.keyboard.reset_mock()
        with patch("mbv.panel.BowmanBot") as create, \
                patch.object(original_bot, "apply_config") as apply, \
                patch.object(original_bot, "disarm") as disarm, \
                patch.object(original_bot, "request_toggle") as toggle, \
                patch.object(original_bot, "preview_config_setting", wraps=original_bot.preview_config_setting) as preview:
            for name, canvas in panel._page_canvases.items():
                panel.notebook.select(canvas.master)
                panel.verification_alert_button.invoke()
                expected = not expected
                with self.subTest(page=name):
                    self.assertEqual(load_config(self.config_path)["verification_alert"]["enabled"], expected)
                    self.assertEqual(original_bot.config["verification_alert"]["enabled"], expected)
                    self.assertTrue(original_bot.armed)
                    self.assertIs(panel.bot, original_bot)
            self.assertEqual(preview.call_count, 7)
            create.assert_not_called()
            apply.assert_not_called()
            disarm.assert_not_called()
            toggle.assert_not_called()
        self.keyboard.assert_not_called()
        self.assertEqual(self.keyboard.mock_calls, [])
        self.run.assert_not_called()
        self.errors.assert_not_called()

    def test_hidden_page_entries_save_without_selecting_their_tabs(self) -> None:
        panel = self.panel
        selected_before = panel.notebook.select()
        changes = {
            "behavior.attack_interval_seconds": "0.37",
            "targeting.box.forward": "0.43",
            "strategy.options.bowman_dynamic.aoe_skill_key": "X",
            "strategy.options.bowman_dynamic.aoe_cluster_distance_multiplier": "1.35",
        }
        entries = {**panel._entries, **panel._targeting_entries, **panel._strategy_entries}
        for key, text in changes.items():
            entries[key].delete(0, "end")
            entries[key].insert(0, text)
        panel.hp_threshold_percent.set(41)
        panel.buff_enabled["buff_1"].set(True)
        self.assertTrue(panel._persist_settings(apply_runtime=False, notify=False, show_error=True))
        config = load_config(self.config_path)
        self.assertEqual(config["behavior"]["attack_interval_seconds"], 0.37)
        self.assertEqual(config["targeting"]["box"]["forward"], 0.43)
        self.assertEqual(config["strategy"]["options"]["bowman_dynamic"]["aoe_skill_key"], "x")
        self.assertEqual(config["strategy"]["options"]["bowman_dynamic"]["aoe_cluster_distance_multiplier"], 1.35)
        self.assertEqual(config["behavior"]["hp_threshold"], 0.41)
        self.assertTrue(config["buffs"]["buff_1"]["enabled"])
        self.assertEqual(panel.notebook.select(), selected_before)
        self.errors.assert_not_called()

    def test_strategy_rebuild_does_not_destroy_other_page_entries(self) -> None:
        panel = self.panel
        key_entry = panel._entries["keys.attack"]
        target_entry = panel._targeting_entries["targeting.box.forward"]
        capture_button = panel._capture_buttons["player"]
        old_strategy_entries = list(panel._strategy_entries.values())
        panel._render_strategy_settings(load_config(self.config_path))
        self.assertIs(panel._entries["keys.attack"], key_entry)
        self.assertIs(panel._targeting_entries["targeting.box.forward"], target_entry)
        self.assertIs(panel._capture_buttons["player"], capture_button)
        self.assertTrue(all(widget.winfo_exists() for widget in (key_entry, target_entry, capture_button)))
        self.assertTrue(all(not widget.winfo_exists() for widget in old_strategy_entries))
        self.assertTrue(all(self.belongs_to(widget, panel._page_bodies["strategy"])
                            for widget in panel._strategy_entries.values()))

    def test_unconnected_panel_keeps_action_gate_and_hides_previous_hp_mp(self) -> None:
        panel = self.panel
        panel.bot.ui_hp = 0.61
        panel.bot.ui_mp = 0.72
        panel._tick()
        self.assertEqual(panel.run_badge.get(), "未连接")
        self.assertNotIn("61%", panel.run_metrics.get())
        self.assertNotIn("72%", panel.run_metrics.get())
        self.assertIn("连接", panel.run_metrics.get())
        self.assertEqual(str(panel.arm_button.cget("state")), "disabled")
        self.assertEqual(str(panel.potion_button.cget("state")), "disabled")
        with patch.object(panel.bot, "request_toggle") as toggle, \
                patch.object(panel.bot, "request_auto_potion") as potion:
            panel.arm_button.invoke()
            panel.potion_button.invoke()
            panel._toggle_arm()
            panel._toggle_auto_potion()
        toggle.assert_not_called()
        potion.assert_not_called()
        self.run.assert_not_called()
        self.assertIsNone(panel.worker)

    def test_connected_status_and_both_notices_are_separate_from_top_badge(self) -> None:
        panel = self.panel
        panel._selected_target = WindowTarget(1001, 2002, "测试目标", "test.exe")
        panel.worker = MagicMock()
        panel.worker.is_alive.return_value = True
        panel.bot.ui_hp = 0.61
        panel.bot.ui_mp = 0.72
        panel.bot.notice = "配置已经保存；此处为完整提示。"
        panel.bot.notice_until = float("inf")
        panel.bot.verification_alert_status = "检测到狩猎验证，请人工处理（挂机未暂停）。"
        panel._tick()
        self.assertIn("61%", panel.run_metrics.get())
        self.assertIn("72%", panel.run_metrics.get())
        self.assertEqual(panel.run_notice.get(), panel.bot.notice + "\n" + panel.bot.verification_alert_status)
        self.assertEqual(str(panel._notice_label.cget("textvariable")), str(panel.run_notice))
        self.assertEqual(str(panel._run_badge_label.cget("textvariable")), str(panel.run_badge))
        self.assertNotIn("\n", panel.run_badge.get())
        self.assertIsNot(panel._notice_label.master, panel._run_badge_label.master)
        self.assertTrue(panel._notice_label.winfo_manager())
        self.assertTrue(panel._notice_label.bind("<Configure>"))
        # withdrawn 控件不接收窗口系统 Configure，直接调用同一个已绑定处理器。
        with patch.object(panel._notice_label, "bind") as bind:
            panel._wrap_to_width(panel._notice_label)
        bind.call_args.args[1](SimpleNamespace(width=350))
        self.assertEqual(int(panel._notice_label.cget("wraplength")), 350)

    def test_wheel_scrolls_only_the_event_page(self) -> None:
        panel = self.panel
        with ExitStack() as stack:
            scrolls = {}
            for name, canvas in panel._page_canvases.items():
                stack.enter_context(patch.object(canvas, "yview", return_value=(0.0, 0.3)))
                scrolls[name] = stack.enter_context(patch.object(canvas, "yview_scroll"))
            result = panel._scroll_page(SimpleNamespace(widget=panel._entries["keys.attack"], delta=-120, num=None))
            self.assertEqual(result, "break")
            scrolls["supply"].assert_called_once_with(1, "units")
            for page, scroll in scrolls.items():
                if page != "supply":
                    scroll.assert_not_called()

    def test_linux_wheel_events_route_up_and_down_without_delta(self) -> None:
        canvas = self.panel._page_canvases["capture"]
        widget = self.panel._capture_buttons["hp_bar"]
        with patch.object(canvas, "yview", return_value=(0.0, 0.3)), \
                patch.object(canvas, "yview_scroll") as scroll:
            self.assertEqual(self.panel._scroll_page(SimpleNamespace(widget=widget, num=4)), "break")
            self.assertEqual(self.panel._scroll_page(SimpleNamespace(widget=widget, num=5)), "break")
        self.assertEqual([call.args for call in scroll.call_args_list], [(-1, "units"), (1, "units")])

    def test_wheel_does_not_scroll_full_page_or_native_controls_or_dialog(self) -> None:
        panel = self.panel
        runtime_body = panel._page_bodies["runtime"]
        listbox = tk.Listbox(runtime_body)
        scale = tk.Scale(runtime_body)
        dialog = tk.Toplevel(self.root)
        dialog.withdraw()
        label = tk.Label(dialog, text="独立模板管理")
        with ExitStack() as stack:
            scrolls = []
            for canvas in panel._page_canvases.values():
                stack.enter_context(patch.object(canvas, "yview", return_value=(0.0, 1.0)))
                scrolls.append(stack.enter_context(patch.object(canvas, "yview_scroll")))
            for widget in (panel.monster_category_combo, listbox, scale, label, panel.arm_button):
                with self.subTest(widget=str(widget)):
                    self.assertIsNone(panel._scroll_page(SimpleNamespace(widget=widget, delta=-120, num=None)))
            self.assertEqual(panel._scroll_page(SimpleNamespace(widget=panel._capture_buttons["hp_bar"], delta=-120)), "break")
            for scroll in scrolls:
                scroll.assert_not_called()

    def test_every_combobox_has_its_own_wheel_guard(self) -> None:
        combos = [widget for widget in self.descendants(self.root) if isinstance(widget, ttk.Combobox)]
        self.assertGreaterEqual(len(combos), 5)
        for combo in combos:
            for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                with self.subTest(combo=str(combo), sequence=sequence):
                    self.assertTrue(combo.bind(sequence))
        self.assertNotIn("_scroll_page", self.root.bind_all("<MouseWheel>"))

    def test_long_parameter_labels_wrap_and_remain_with_their_entries(self) -> None:
        panel = self.panel
        for key in ("behavior.player_lost_move_seconds", "behavior.max_runtime_minutes",
                    "vision.player_minimap_navigation_seconds"):
            entry = panel._entries[key]
            label = next(widget for widget in entry.master.winfo_children() if isinstance(widget, tk.Label))
            with self.subTest(key=key):
                self.assertGreater(int(label.cget("wraplength")), 0)
                self.assertEqual(str(label.cget("justify")), "left")
                self.assertGreaterEqual(label.winfo_reqwidth(), int(label.cget("wraplength")))
                self.assertTrue(label.winfo_manager())


if __name__ == "__main__":
    unittest.main()
