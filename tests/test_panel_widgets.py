from __future__ import annotations

import gc
import tkinter as tk
import unittest
from unittest.mock import MagicMock, patch

from mbv import panel_theme as theme
from mbv.panel_widgets import RoundedButton, RoundedCard, _rounded_image


class PanelWidgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tk.Tk()
        self.root.withdraw()
        self.old_scaling = float(self.root.tk.call("tk", "scaling"))
        self.errors: list[BaseException] = []
        self.root.report_callback_exception = lambda _kind, value, _trace: self.errors.append(value)
        self.root.configure(bg=theme.BG)

    def tearDown(self) -> None:
        try:
            self.root.update_idletasks()
            self.root.tk.call("tk", "scaling", self.old_scaling)
        finally:
            self.root.destroy()
        self.assertEqual(self.errors, [])

    def button(self, **kwargs: object) -> RoundedButton:
        button = RoundedButton(self.root, **kwargs)
        self.root.update_idletasks()
        return button

    def reference(self, rounded: RoundedButton) -> tk.Button:
        return tk.Button(self.root, **{
            key: rounded.cget(key)
            for key in ("text", "textvariable", "font", "width", "height", "padx", "pady",
                        "borderwidth", "highlightthickness", "wraplength", "justify", "underline",
                        "default", "relief")
        })

    def test_card_body_controls_requested_size_and_canvas_is_only_decoration(self) -> None:
        card = RoundedCard(self.root, padding=8)
        tk.Label(card.body, text="窗口连接", font=theme.FONT, bg=theme.PANEL).pack()
        self.root.update_idletasks()
        self.assertEqual(card.winfo_reqwidth(), card.body.winfo_reqwidth() + 16)
        self.assertEqual(card.winfo_reqheight(), card.body.winfo_reqheight() + 16)
        self.assertEqual(card._background.winfo_manager(), "place")
        self.assertEqual(card.body.winfo_manager(), "pack")
        self.assertEqual(card.cget("bg"), theme.BG)
        self.assertEqual(card.body.cget("bg"), theme.PANEL)

    def test_card_cache_skips_identical_geometry(self) -> None:
        card = RoundedCard(self.root)
        with patch.object(card, "winfo_width", return_value=560), \
             patch.object(card, "winfo_height", return_value=90):
            card._draw_background()
            photo = card._photo
            for _ in range(20):
                card._draw_background()
            self.assertIs(card._photo, photo)

    def test_card_destroy_cancels_pending_draw(self) -> None:
        card = RoundedCard(self.root)
        card._queue_draw()
        card.destroy()
        self.root.update_idletasks()
        self.assertEqual(self.errors, [])

    def test_destroyed_card_background_is_not_accessed_by_pending_callback(self) -> None:
        card = RoundedCard(self.root)
        card._queue_draw()
        card._background.destroy()
        self.root.update_idletasks()
        self.assertTrue(card.winfo_exists())
        self.assertEqual(self.errors, [])

    def test_card_child_destruction_cannot_schedule_a_new_idle_draw(self) -> None:
        card = RoundedCard(self.root)
        destroy_canvas = card._background.destroy

        def destroy_with_reentrant_redraw() -> None:
            card._queue_draw()
            card._draw_background()
            self.assertIsNone(card._pending_draw)
            destroy_canvas()

        with patch.object(card._background, "destroy", side_effect=destroy_with_reentrant_redraw), \
             patch("mbv.panel_widgets._render_rounded_image") as render:
            card.destroy()
            render.assert_not_called()

    def test_card_rendering_does_not_retain_large_rasters_in_button_cache(self) -> None:
        card = RoundedCard(self.root)
        before = _rounded_image.cache_info()
        with patch.object(card, "winfo_width", return_value=600), \
             patch.object(card, "winfo_height", return_value=1000):
            card._draw_background()
        self.assertEqual(_rounded_image.cache_info(), before)

    def test_generated_round_corner_uses_parent_color_and_reuses_raster(self) -> None:
        fill, outside = (10, 80, 170), (240, 240, 240)
        image = _rounded_image(100, 32, 9, fill, outside)
        self.assertEqual(image.getpixel((0, 0)), outside)
        self.assertEqual(image.getpixel((50, 16)), fill)
        self.assertIs(image, _rounded_image(100, 32, 9, fill, outside))

    def test_first_request_is_never_temporarily_interpreted_as_character_pixels(self) -> None:
        button = RoundedButton(self.root, text="连接", width=6)
        reference = self.reference(button)
        # 特意不处理 idle；构造函数返回时即应是合理尺寸。
        self.assertEqual(button.winfo_reqwidth(), reference.winfo_reqwidth())
        self.assertEqual(button.winfo_reqheight(), reference.winfo_reqheight())
        self.assertLess(button.winfo_reqwidth(), 200)

    def test_character_width_and_padding_match_native_button_at_two_point_scaling(self) -> None:
        self.root.tk.call("tk", "scaling", 2.0)
        for width in (0, 3, 5, 6):
            with self.subTest(width=width):
                button = self.button(text="连接窗口", width=width, padx=10, pady=5)
                reference = self.reference(button)
                self.assertEqual(button.winfo_reqwidth(), reference.winfo_reqwidth())
                self.assertEqual(button.winfo_reqheight(), reference.winfo_reqheight())
                self.assertEqual(int(button.cget("width")), width)
                self.assertLess(button.winfo_reqwidth(), 560)
                self.assertFalse(self.root.winfo_viewable())

    def test_resizing_background_does_not_change_natural_request(self) -> None:
        button = self.button(text="连接窗口", width=6)
        expected = (button.winfo_reqwidth(), button.winfo_reqheight())
        for width in (560, 320, 140, 560):
            with patch.object(button, "winfo_width", return_value=width), \
                 patch.object(button, "winfo_height", return_value=40):
                button._draw_background()
                self.assertEqual((button.winfo_reqwidth(), button.winfo_reqheight()), expected)

    def test_dynamic_width_font_padding_and_text_preserve_native_geometry(self) -> None:
        button = self.button(text="采集", width=3)
        for values in (
            {"width": 0, "text": "新增龙咆哮攻击范围"},
            {"font": (theme.FONT[0], 13), "padx": 12, "pady": 7},
            {"width": 5, "text": "连接", "padx": 3, "pady": 2},
        ):
            with self.subTest(values=values):
                button.configure(**values)
                self.root.update_idletasks()
                reference = self.reference(button)
                self.assertEqual(button.winfo_reqwidth(), reference.winfo_reqwidth())
                self.assertEqual(button.winfo_reqheight(), reference.winfo_reqheight())

    def test_logical_colors_and_configure_queries_survive_theme_styling(self) -> None:
        button = self.button(text="连接")
        theme.style_button(button, primary=True)
        self.root.update_idletasks()
        self.assertEqual(button.cget("bg"), theme.ACCENT)
        self.assertEqual(button["background"], theme.ACCENT)
        self.assertEqual(button.configure("background")[-1], theme.ACCENT)
        self.assertEqual(button.configure()["background"][-1], theme.ACCENT)
        self.assertEqual(button.cget("fg"), theme.PANEL)
        reference = tk.Button(self.root)
        theme.style_button(reference, primary=True)
        self.assertEqual(button.cget("padx"), reference.cget("padx"))
        self.assertEqual(int(tk.Button.cget(button, "highlightthickness")), 0)
        self.assertEqual(tk.Button.cget(button, "background"), theme.BG)

    def test_theme_border_and_focus_outline_are_rendered_without_square_highlight(self) -> None:
        button = self.button(text="连接")
        theme.style_button(button)
        self.root.update_idletasks()
        expected_border = tuple(value // 257 for value in button.winfo_rgb(theme.BORDER))
        self.assertEqual(button._draw_signature[-1], expected_border)
        with patch.object(button, "_has_focus", return_value=True):
            button._draw_background()
        expected_focus = tuple(value // 257 for value in button.winfo_rgb(theme.ACCENT))
        self.assertEqual(button._draw_signature[-1], expected_focus)
        self.assertEqual(int(tk.Button.cget(button, "highlightthickness")), 0)

    def test_default_buttons_have_a_border_unless_explicitly_disabled(self) -> None:
        button = self.button(text="+")
        expected_border = tuple(value // 257 for value in button.winfo_rgb(theme.BORDER))
        self.assertEqual(button._draw_signature[-1], expected_border)
        borderless = self.button(text="×", highlightthickness=0)
        self.assertIsNone(borderless._draw_signature[-1])

    def test_tcl_owned_combobox_focus_is_not_resolved_as_a_python_widget(self) -> None:
        button = self.button()
        fake_tk = MagicMock()
        fake_tk.call.return_value = ".!combobox.popdown.f.l"
        with patch.object(button, "tk", fake_tk), \
             patch.object(button, "focus_get", side_effect=AssertionError("must not resolve Tcl widgets")):
            self.assertFalse(button._has_focus())
            fake_tk.call.assert_called_once_with("focus")
            fake_tk.call.return_value = str(button)
            self.assertTrue(button._has_focus())

    def test_dictionary_config_and_item_assignment_preserve_standard_aliases(self) -> None:
        button = self.button(text="采集")
        button.configure({"text": "连接", "bg": theme.ACCENT})
        button["fg"] = theme.PANEL
        button.config(state="disabled")
        self.assertEqual(button.cget("text"), "连接")
        self.assertEqual(button.cget("bg"), theme.ACCENT)
        self.assertEqual(button.cget("fg"), theme.PANEL)
        self.assertEqual(button.cget("state"), "disabled")

    def test_repeated_unchanged_state_and_text_do_not_schedule_drawing(self) -> None:
        button = self.button(text="启动挂机", state="normal")
        photo = button._photo
        with patch.object(button, "_queue_draw") as queue_draw, \
             patch.object(button, "_refresh_layout") as measure:
            for _ in range(200):
                button.configure(text="启动挂机", state="normal", bg=button.cget("bg"))
        queue_draw.assert_not_called()
        measure.assert_not_called()
        self.assertIs(button._photo, photo)

    def test_active_and_disabled_states_keep_native_semantics(self) -> None:
        action = MagicMock(return_value="done")
        button = self.button(text="连接", command=action, bg=theme.ACCENT,
                             activebackground=theme.BUTTON_ACTIVE, disabledforeground=theme.MUTED)
        self.assertEqual(button.invoke(), "done")
        button.configure(state="active")
        self.root.update_idletasks()
        active_image = button._photo
        self.assertEqual(button.cget("state"), "active")
        button.configure(state="disabled")
        self.root.update_idletasks()
        self.assertEqual(button.invoke(), "")
        self.assertEqual(action.call_count, 1)
        self.assertIsNot(button._photo, active_image)
        self.assertNotEqual(button.cget("disabledforeground"), button.cget("bg"))

    def test_command_can_be_replaced_without_replacing_native_button_binding(self) -> None:
        first, second = MagicMock(), MagicMock()
        class_binding = self.root.bind_class("Button", "<ButtonRelease-1>")
        all_binding = self.root.bind_all("<MouseWheel>")
        button = self.button(command=first, takefocus=True)
        self.assertIsInstance(button, tk.Button)
        self.assertIn("Button", button.bindtags())
        button.configure(command=second)
        button.invoke()
        first.assert_not_called()
        second.assert_called_once_with()
        self.assertEqual(self.root.bind_class("Button", "<ButtonRelease-1>"), class_binding)
        self.assertEqual(self.root.bind_all("<MouseWheel>"), all_binding)
        self.assertTrue(self.root.tk.getboolean(button.cget("takefocus")))
        self.assertIsNone(self.root.focus_get())

    def test_native_space_command_path_runs_on_the_hidden_button(self) -> None:
        action = MagicMock()
        button = self.button(command=action)
        # 调用原生Tk按钮类处理器；不向系统/游戏注入任何键鼠事件。
        pending_before = set(self.root.tk.call("after", "info"))
        self.root.tk.call("tk::ButtonInvoke", button)
        # Windows Tk 通过 after 100 完成原生按压动画与命令；执行它排入的脚本，
        # 不映射窗口、不依赖墙钟睡眠，也不直接调用本控件的 invoke 绕过类绑定。
        pending_native = set(self.root.tk.call("after", "info")) - pending_before
        for callback in pending_native:
            script = self.root.tk.call("after", "info", callback)[0]
            self.root.tk.call("after", "cancel", callback)
            self.root.tk.call("eval", script)
        action.assert_called_once_with()
        self.assertFalse(self.root.winfo_viewable())

    def test_textvariable_changes_refresh_geometry_without_owning_the_variable(self) -> None:
        variable = tk.StringVar(self.root, value="连接")
        button = self.button(textvariable=variable)
        short_width = button.winfo_reqwidth()
        variable.set("连接所选择的实际作用窗口")
        self.root.update_idletasks()
        self.assertGreater(button.winfo_reqwidth(), short_width)
        self.assertEqual(variable.get(), "连接所选择的实际作用窗口")
        button.destroy()
        gc.collect()
        self.assertEqual(variable.get(), "连接所选择的实际作用窗口")
        variable.set("仍能继续使用")
        self.assertEqual(variable.get(), "仍能继续使用")

    def test_replacing_textvariable_preserves_both_callers_variables(self) -> None:
        first = tk.StringVar(self.root, value="原变量")
        second = tk.StringVar(self.root, value="新变量")
        button = self.button(textvariable=first)
        button.configure(textvariable=second)
        self.root.update_idletasks()
        gc.collect()
        self.assertEqual(first.get(), "原变量")
        self.assertEqual(second.get(), "新变量")
        first.set("原变量仍有效")
        self.root.update_idletasks()
        self.assertEqual(button.cget("text"), "新变量")
        button.destroy()
        self.assertEqual(first.get(), "原变量仍有效")
        self.assertEqual(second.get(), "新变量")

    def test_destroy_with_pending_text_or_state_update_has_no_late_callback(self) -> None:
        variable = tk.StringVar(self.root, value="连接")
        button = self.button(textvariable=variable)
        variable.set("准备退出")
        button.configure(state="disabled")
        button.destroy()
        self.root.update_idletasks()
        self.assertEqual(self.errors, [])
        self.assertEqual(variable.get(), "准备退出")

    def test_invalid_color_fails_synchronously_without_corrupting_logical_background(self) -> None:
        button = self.button()
        original = button.cget("bg")
        with self.assertRaises(tk.TclError):
            button.configure(bg="not-a-real-color")
        self.assertEqual(button.cget("bg"), original)


if __name__ == "__main__":
    unittest.main()
