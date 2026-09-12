from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
import unittest

from mbv.panel_theme import (
    ACCENT,
    ACCENT_HOVER,
    ACCENT_SOFT,
    ARMED,
    BG,
    BORDER,
    BUTTON_BG,
    DANGER_HOVER,
    ENTRY_BG,
    FG,
    FONT,
    FONT_SECTION,
    FONT_SMALL,
    FONT_TITLE,
    MUTED,
    PANEL,
    SUCCESS,
    SURFACE,
    install_theme,
    style_button,
)


class PanelThemeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.style = install_theme(self.root)

    def test_standard_and_namespaced_combos_remain_readable_when_disabled(self) -> None:
        self.assertEqual(self.style.theme_use(), "clam")
        for name in ("TCombobox", "MBV.TCombobox"):
            with self.subTest(style=name):
                self.assertEqual(self.style.lookup(name, "foreground", ("readonly",)), FG)
                self.assertEqual(self.style.lookup(name, "fieldbackground", ("readonly",)), ENTRY_BG)
                self.assertEqual(self.style.lookup(name, "foreground", ("disabled", "readonly")), MUTED)
                self.assertEqual(self.style.lookup(name, "fieldbackground", ("disabled", "readonly")), PANEL)
                self.assertEqual(self.style.lookup(name, "bordercolor", ("focus",)), ACCENT)

    def test_combobox_popup_uses_light_option_defaults(self) -> None:
        combo = ttk.Combobox(self.root, state="readonly", values=("作用窗口", "默认配置"))
        popup = self.root.tk.call("ttk::combobox::PopdownWindow", str(combo))
        listbox = f"{popup}.f.l"
        for option, color in (
            ("-background", ENTRY_BG),
            ("-foreground", FG),
            ("-selectbackground", ACCENT_SOFT),
            ("-selectforeground", FG),
        ):
            with self.subTest(option=option):
                self.assertEqual(self.root.tk.call(listbox, "cget", option), color)

    def test_notebook_styles_and_new_widgets_have_valid_layouts(self) -> None:
        for prefix in ("", "MBV."):
            notebook_style = f"{prefix}TNotebook"
            with self.subTest(prefix=prefix):
                self.assertEqual(self.style.lookup(f"{notebook_style}.Tab", "foreground", ("selected",)), FG)
                self.assertEqual(self.style.lookup(f"{notebook_style}.Tab", "background", ("selected",)), PANEL)
                self.assertEqual(self.style.lookup(notebook_style, "background"), BG)
                notebook = ttk.Notebook(self.root, style=notebook_style)
                notebook.add(tk.Frame(notebook), text="采集校准")
                notebook.add(tk.Frame(notebook), text="策略设置")
                self.assertIn("MBV.Segmented.tab", str(self.style.layout(f"{notebook_style}.Tab")))
                self.assertIn("Notebook.label", str(self.style.layout(f"{notebook_style}.Tab")))
                ttk.Treeview(self.root, style=f"{prefix}Treeview")
                for orientation in ("Horizontal", "Vertical"):
                    ttk.Scrollbar(self.root, orient=orientation.lower(), style=f"{prefix}{orientation}.TScrollbar")
                    ttk.Progressbar(self.root, orient=orientation.lower(), style=f"{prefix}{orientation}.TProgressbar")

    def test_tk_controls_inherit_quiet_light_defaults(self) -> None:
        button = tk.Button(self.root, text="连接所选窗口", state="disabled")
        entry = tk.Entry(self.root)
        check = tk.Checkbutton(self.root, text="自动喝药")
        self.assertEqual(button.cget("background"), BUTTON_BG)
        self.assertEqual(button.cget("disabledforeground"), MUTED)
        self.assertEqual(button.cget("highlightbackground"), BORDER)
        self.assertEqual(entry.cget("background"), ENTRY_BG)
        self.assertEqual(entry.cget("foreground"), FG)
        self.assertEqual(check.cget("selectcolor"), ENTRY_BG)

    def test_button_helper_preserves_command_state_and_dynamic_colors(self) -> None:
        calls: list[str] = []
        button = tk.Button(self.root, command=lambda: calls.append("clicked"))
        original_command = button.cget("command")
        original_bindings = button.bind()
        style_button(button, primary=True)
        self.assertEqual(button.cget("background"), ACCENT)
        self.assertEqual(button.cget("foreground"), PANEL)
        self.assertEqual(button.cget("activebackground"), ACCENT_HOVER)
        self.assertNotEqual(button.cget("activebackground"), SUCCESS)
        self.assertEqual(button.cget("command"), original_command)
        self.assertEqual(button.bind(), original_bindings)
        button.invoke()
        self.assertEqual(calls, ["clicked"])
        button.configure(bg=ARMED, state="disabled")
        button.event_generate("<Enter>")
        button.event_generate("<Leave>")
        self.root.update_idletasks()
        self.assertEqual(button.cget("background"), ARMED)
        self.assertEqual(str(button.cget("state")), "disabled")

    def test_theme_can_be_installed_again_without_resetting_widget_values(self) -> None:
        variable = tk.StringVar(self.root, value="已选择的窗口")
        combo = ttk.Combobox(self.root, textvariable=variable, state="readonly")
        install_theme(self.root)
        self.assertEqual(variable.get(), "已选择的窗口")
        self.assertEqual(str(combo.cget("state")), "readonly")

    def test_palette_has_neutral_surfaces_and_distinct_semantic_actions(self) -> None:
        self.assertEqual(FG, "#1d1d1f")
        self.assertEqual(BG, "#f5f5f7")
        self.assertEqual(SURFACE, "#ececef")
        self.assertEqual(ACCENT, "#0071e3")
        self.assertEqual(len({ACCENT, SUCCESS, ARMED}), 3)
        danger = tk.Button(self.root, text="停止")
        style_button(danger, danger=True)
        self.assertEqual(danger.cget("background"), ARMED)
        self.assertEqual(danger.cget("activebackground"), DANGER_HOVER)
        self.assertEqual(danger.cget("foreground"), PANEL)

    def test_primary_and_secondary_text_have_readable_contrast(self) -> None:
        def luminance(color):
            channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
            linear = [channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
                      for channel in channels]
            return sum(channel * weight for channel, weight in zip(linear, (0.2126, 0.7152, 0.0722)))

        for foreground, background in ((FG, PANEL), (MUTED, BG), (PANEL, ACCENT), (PANEL, ARMED)):
            values = sorted((luminance(foreground), luminance(background)))
            contrast = (values[1] + 0.05) / (values[0] + 0.05)
            with self.subTest(foreground=foreground, background=background):
                self.assertGreaterEqual(contrast, 4.5)

    def test_chinese_fonts_have_clear_hierarchy_and_never_drop_below_nine_points(self) -> None:
        for description in (FONT, FONT_SMALL, FONT_TITLE, FONT_SECTION):
            self.assertEqual(description[0], "Microsoft YaHei UI")
            self.assertGreaterEqual(description[1], 9)
            font = tkfont.Font(self.root, font=description)
            self.assertGreaterEqual(font.actual("size"), 9)
        self.assertGreater(FONT_TITLE[1], FONT[1])
        self.assertEqual(FONT_SECTION[1], FONT[1])

    def test_reinstall_keeps_rounded_images_and_existing_notebook_state(self) -> None:
        notebook = ttk.Notebook(self.root, style="MBV.TNotebook")
        pages = [ttk.Frame(notebook) for _ in range(7)]
        for index, page in enumerate(pages):
            notebook.add(page, text=f"功能 {index + 1}")
        notebook.select(pages[3])
        notebook.tab(pages[5], state="disabled")
        images = self.root._mbv_theme_images
        image_names = {str(image) for image in images.values()}
        elements = set(self.style.element_names())
        event_calls = []
        notebook.bind("<<NotebookTabChanged>>", lambda _event: event_calls.append("changed"))
        bindings = notebook.bind()
        bind_all = self.root.tk.call("bind", "all")
        class_binding = self.root.tk.call("bind", "TNotebook", "<Button-1>")
        install_theme(self.root)
        self.root.update_idletasks()
        self.assertIs(self.root._mbv_theme_images, images)
        self.assertTrue(image_names.issubset(set(self.root.tk.call("image", "names"))))
        self.assertEqual(set(self.style.element_names()), elements)
        self.assertEqual(notebook.tabs(), tuple(str(page) for page in pages))
        self.assertEqual(notebook.select(), str(pages[3]))
        self.assertEqual(str(notebook.tab(pages[5], "state")), "disabled")
        self.assertEqual(notebook.bind(), bindings)
        self.assertEqual(self.root.tk.call("bind", "all"), bind_all)
        self.assertEqual(self.root.tk.call("bind", "TNotebook", "<Button-1>"), class_binding)

    def test_theme_in_child_dialog_reuses_same_interpreter_image_cache(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.withdraw()
        self.addCleanup(dialog.destroy)
        images = self.root._mbv_theme_images
        install_theme(dialog)
        self.assertIs(self.root._mbv_theme_images, images)
        self.assertFalse(hasattr(dialog, "_mbv_theme_images"))

    def test_scrollbar_is_lightweight_but_retains_native_range_and_command(self) -> None:
        for orientation in ("Horizontal", "Vertical"):
            for prefix in ("", "MBV."):
                name = f"{prefix}{orientation}.TScrollbar"
                scrollbar = ttk.Scrollbar(self.root, orient=orientation.lower(), style=name,
                                          command=lambda *_args: None)
                original_command = scrollbar.cget("command")
                scrollbar.set(0.2, 0.6)
                layout = str(self.style.layout(name))
                self.assertIn(f"MBV.{orientation}.Scrollbar.thumb", layout)
                self.assertIn(f"{orientation}.Scrollbar.trough", layout)
                self.assertNotIn("arrow", layout)
                install_theme(self.root)
                self.assertEqual(scrollbar.get(), (0.2, 0.6))
                self.assertEqual(scrollbar.cget("command"), original_command)

    def test_treeview_selection_is_soft_blue_with_dark_readable_text(self) -> None:
        for name in ("Treeview", "MBV.Treeview"):
            self.assertEqual(self.style.lookup(name, "background"), PANEL)
            self.assertEqual(self.style.lookup(name, "background", ("selected",)), ACCENT_SOFT)
            self.assertEqual(self.style.lookup(name, "foreground", ("selected",)), FG)
            self.assertEqual(self.style.lookup(name, "borderwidth"), 0)


if __name__ == "__main__":
    unittest.main()
