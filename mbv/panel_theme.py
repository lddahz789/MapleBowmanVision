from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageDraw, ImageTk


# macOS-inspired 中性层级；蓝色表示操作，绿色仅表示成功，红色保留给停止/危险。
BG = "#f5f5f7"
PANEL = "#ffffff"
SURFACE = "#ececef"
ENTRY_BG = "#fafafb"
FG = "#1d1d1f"
MUTED = "#6e6e73"
ACCENT = "#0071e3"
ACCENT_HOVER = "#0066cc"
SUCCESS = "#248a3d"
WARNING = "#9a6700"
ARMED = "#d7332f"
DANGER_HOVER = "#bf2b27"
BUTTON_BG = PANEL
BUTTON_ACTIVE = "#f0f0f2"
BORDER = "#d2d2d7"
ACCENT_SOFT = "#eaf3ff"

FONT = ("Microsoft YaHei UI", 10)
FONT_TITLE = ("Microsoft YaHei UI", 14, "bold")
FONT_SECTION = ("Microsoft YaHei UI", 10, "bold")
FONT_SMALL = ("Microsoft YaHei UI", 9)


def _rounded_image(
    root: tk.Misc,
    size: tuple[int, int],
    fill: str,
    *,
    outline: str | None = None,
    radius: int = 7,
) -> ImageTk.PhotoImage:
    """生成内存中的可九宫格伸缩圆角元素，不读取或保存任何图像文件。"""
    scale = 4
    width, height = size
    image = Image.new("RGBA", (width * scale, height * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (scale, scale, (width - 1) * scale - 1, (height - 1) * scale - 1),
        radius=radius * scale,
        fill=fill,
        outline=outline,
        width=scale,
    )
    return ImageTk.PhotoImage(image.resize(size, Image.Resampling.LANCZOS), master=root)


def _install_rounded_elements(root: tk.Misc, style: ttk.Style) -> None:
    """图像随 Tk 解释器存活；重复安装复用元素，不重建控件或覆盖事件。"""
    owner = root._root()
    cache = getattr(owner, "_mbv_theme_images", None)
    if cache is None:
        cache = {
            "tab_idle": _rounded_image(owner, (28, 28), SURFACE),
            "tab_hover": _rounded_image(owner, (28, 28), BUTTON_ACTIVE, outline=BORDER),
            "tab_selected": _rounded_image(owner, (28, 28), PANEL, outline=BORDER),
            "tab_focus": _rounded_image(owner, (28, 28), PANEL, outline=ACCENT),
            "tab_disabled": _rounded_image(owner, (28, 28), BG),
        }
        for orientation, size in (("Horizontal", (28, 10)), ("Vertical", (10, 28))):
            for state, color in (("idle", "#c4c4c8"), ("active", "#aeafb6"),
                                 ("pressed", "#92939b"), ("disabled", BORDER)):
                cache[f"{orientation}_{state}"] = _rounded_image(owner, size, color, radius=4)
        owner._mbv_theme_images = cache
    names = set(style.element_names())
    if "MBV.Segmented.tab" not in names:
        style.element_create(
            "MBV.Segmented.tab", "image", cache["tab_idle"],
            ("disabled", cache["tab_disabled"]),
            ("selected", "focus", cache["tab_focus"]),
            ("selected", cache["tab_selected"]),
            ("active", cache["tab_hover"]),
            border=8, sticky="nsew",
        )
    for orientation in ("Horizontal", "Vertical"):
        element = f"MBV.{orientation}.Scrollbar.thumb"
        if element not in names:
            style.element_create(
                element, "image", cache[f"{orientation}_idle"],
                ("disabled", cache[f"{orientation}_disabled"]),
                ("pressed", cache[f"{orientation}_pressed"]),
                ("active", cache[f"{orientation}_active"]),
                border=4, sticky="nsew",
            )


def install_theme(root: tk.Misc) -> ttk.Style:
    """统一面板及其子对话框的外观；不绑定事件，也不改变窗口焦点。"""
    root.option_add("*Font", FONT, "widgetDefault")
    defaults: dict[str, object] = {
        "*Button.background": BUTTON_BG,
        "*Button.foreground": FG,
        "*Button.activeBackground": BUTTON_ACTIVE,
        "*Button.activeForeground": FG,
        "*Button.disabledForeground": MUTED,
        "*Button.relief": "flat",
        "*Button.borderWidth": 0,
        "*Button.highlightThickness": 1,
        "*Button.highlightBackground": BORDER,
        "*Button.highlightColor": ACCENT,
        "*Button.padX": 12,
        "*Button.padY": 5,
        "*Checkbutton.background": PANEL,
        "*Checkbutton.foreground": FG,
        "*Checkbutton.activeBackground": PANEL,
        "*Checkbutton.activeForeground": ACCENT,
        "*Checkbutton.disabledForeground": MUTED,
        "*Checkbutton.selectColor": ENTRY_BG,
        "*Checkbutton.highlightThickness": 0,
        "*Radiobutton.background": PANEL,
        "*Radiobutton.foreground": FG,
        "*Radiobutton.activeBackground": PANEL,
        "*Radiobutton.activeForeground": ACCENT,
        "*Radiobutton.disabledForeground": MUTED,
        "*Radiobutton.selectColor": ENTRY_BG,
        "*Radiobutton.highlightThickness": 0,
        "*Entry.background": ENTRY_BG,
        "*Entry.foreground": FG,
        "*Entry.insertBackground": FG,
        "*Entry.disabledBackground": PANEL,
        "*Entry.disabledForeground": MUTED,
        "*Entry.readonlyBackground": ENTRY_BG,
        "*Entry.selectBackground": ACCENT_SOFT,
        "*Entry.selectForeground": FG,
        "*Entry.relief": "flat",
        "*Entry.borderWidth": 0,
        "*Entry.highlightThickness": 1,
        "*Entry.highlightBackground": BORDER,
        "*Entry.highlightColor": ACCENT,
        "*Label.background": PANEL,
        "*Label.foreground": FG,
        "*Label.disabledForeground": MUTED,
        "*LabelFrame.background": PANEL,
        "*LabelFrame.foreground": MUTED,
        "*LabelFrame.relief": "solid",
        "*LabelFrame.borderWidth": 1,
        "*Listbox.background": ENTRY_BG,
        "*Listbox.foreground": FG,
        "*Listbox.selectBackground": ACCENT_SOFT,
        "*Listbox.selectForeground": FG,
        "*Listbox.disabledForeground": MUTED,
        "*Listbox.relief": "flat",
        "*Listbox.borderWidth": 0,
        "*Listbox.highlightThickness": 0,
        # 下拉列表由 Tcl 延后创建，不能只配置 Combobox 本体的样式。
        "*TCombobox*Listbox.background": ENTRY_BG,
        "*TCombobox*Listbox.foreground": FG,
        "*TCombobox*Listbox.selectBackground": ACCENT_SOFT,
        "*TCombobox*Listbox.selectForeground": FG,
        "*TCombobox*Listbox.font": FONT,
    }
    for pattern, value in defaults.items():
        root.option_add(pattern, value, "widgetDefault")

    style = ttk.Style(root)
    style.theme_use("clam")
    _install_rounded_elements(root, style)
    style.configure(".", font=FONT, background=PANEL, foreground=FG)

    for name in ("TFrame", "MBV.TFrame"):
        style.configure(name, background=PANEL)
    for name in ("TLabel", "MBV.TLabel"):
        style.configure(name, background=PANEL, foreground=FG)
        style.map(name, foreground=[("disabled", MUTED)])

    for name in ("TButton", "MBV.TButton"):
        style.configure(name, background=BUTTON_BG, foreground=FG, bordercolor=BORDER,
                        lightcolor=BUTTON_BG, darkcolor=BUTTON_BG, borderwidth=1,
                        relief="flat", padding=(12, 5), focusthickness=1, focuscolor=ACCENT)
        style.map(name, background=[("disabled", BG), ("pressed", SURFACE),
                                    ("active", BUTTON_ACTIVE)],
                  foreground=[("disabled", MUTED)], bordercolor=[("focus", ACCENT)])

    for name in ("TCombobox", "MBV.TCombobox"):
        style.configure(
            name,
            background=BUTTON_BG,
            fieldbackground=ENTRY_BG,
            foreground=FG,
            arrowcolor=MUTED,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            insertcolor=FG,
            selectbackground=ACCENT_SOFT,
            selectforeground=FG,
            padding=(8, 5),
            arrowsize=11,
            borderwidth=1,
            relief="flat",
        )
        style.map(
            name,
            fieldbackground=[("disabled", PANEL), ("readonly", ENTRY_BG)],
            foreground=[("disabled", MUTED), ("readonly", FG)],
            background=[("disabled", PANEL), ("active", BUTTON_ACTIVE)],
            arrowcolor=[("disabled", MUTED), ("active", ACCENT)],
            bordercolor=[("focus", ACCENT), ("!focus", BORDER)],
            lightcolor=[("focus", ACCENT), ("!focus", BORDER)],
            darkcolor=[("focus", ACCENT), ("!focus", BORDER)],
            selectbackground=[("readonly", ENTRY_BG)],
            selectforeground=[("readonly", FG)],
        )

    for name in ("TNotebook", "MBV.TNotebook"):
        style.configure(name, background=BG, borderwidth=0, tabmargins=(0, 0, 0, 10))
        tab_name = f"{name}.Tab"
        style.layout(tab_name, [
            ("MBV.Segmented.tab", {"sticky": "nsew", "children": [
                ("Notebook.padding", {"side": "top", "sticky": "nsew", "children": [
                    ("Notebook.label", {"side": "top", "sticky": ""}),
                ]}),
            ]}),
        ])
        style.configure(
            tab_name,
            background=SURFACE,
            foreground=MUTED,
            bordercolor=BG,
            lightcolor=BG,
            darkcolor=BG,
            padding=(8, 7),
            font=FONT_SMALL,
            expand=(0, 0, 0, 0),
            focuscolor=ACCENT,
        )
        style.map(
            tab_name,
            background=[("disabled", BG), ("selected", PANEL), ("active", BUTTON_ACTIVE)],
            foreground=[("disabled", MUTED), ("selected", FG), ("active", FG)],
            bordercolor=[("selected", BORDER), ("!selected", BG)],
            lightcolor=[("selected", BORDER), ("!selected", BG)],
            darkcolor=[("selected", BORDER), ("!selected", BG)],
        )

    for orientation in ("Horizontal", "Vertical"):
        for prefix in ("", "MBV."):
            name = f"{prefix}{orientation}.TScrollbar"
            style.layout(name, [
                (f"{orientation}.Scrollbar.trough", {"sticky": "nsew", "children": [
                    (f"MBV.{orientation}.Scrollbar.thumb", {
                        "expand": "1", "sticky": "nsew",
                    }),
                ]}),
            ])
            style.configure(
                name,
                background=SURFACE,
                troughcolor=BG,
                bordercolor=BG,
                lightcolor=BG,
                darkcolor=BG,
                arrowcolor=MUTED,
                arrowsize=0,
                borderwidth=0,
                gripcount=0,
                width=10,
            )
            style.map(
                name,
                background=[("active", BUTTON_ACTIVE), ("pressed", BUTTON_ACTIVE)],
                arrowcolor=[("active", FG), ("disabled", BORDER)],
            )
            progress_name = f"{prefix}{orientation}.TProgressbar"
            style.configure(
                progress_name,
                background=ACCENT,
                troughcolor=ENTRY_BG,
                bordercolor=ENTRY_BG,
                lightcolor=ACCENT,
                darkcolor=ACCENT,
                borderwidth=0,
                thickness=5,
            )

    for name in ("Treeview", "MBV.Treeview"):
        style.configure(
            name,
            background=PANEL,
            fieldbackground=PANEL,
            foreground=FG,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            borderwidth=0,
            rowheight=31,
        )
        style.map(
            name,
            background=[("selected", ACCENT_SOFT)],
            foreground=[("disabled", MUTED), ("selected", FG)],
        )
        style.configure(
            f"{name}.Heading",
            background=ENTRY_BG,
            foreground=MUTED,
            font=FONT_SMALL,
            bordercolor=BORDER,
            lightcolor=PANEL,
            darkcolor=PANEL,
            relief="flat",
            padding=(8, 6),
        )
        style.map(f"{name}.Heading", background=[("active", BUTTON_BG)])
    return style


def style_button(button: tk.Button, *, primary: bool = False, danger: bool = False) -> None:
    """设置静态按钮样式；运行状态仍可通过 configure 自由覆盖。"""
    background = ARMED if danger else ACCENT if primary else BUTTON_BG
    foreground = PANEL if primary or danger else FG
    button.configure(
        bg=background,
        fg=foreground,
        activebackground=DANGER_HOVER if danger else ACCENT_HOVER if primary else BUTTON_ACTIVE,
        activeforeground=foreground,
        disabledforeground=BUTTON_BG if primary or danger else MUTED,
        font=FONT,
        relief="flat",
        bd=0,
        padx=12,
        pady=5,
        highlightthickness=1,
        highlightbackground=background if primary or danger else BORDER,
        highlightcolor=ACCENT,
    )
