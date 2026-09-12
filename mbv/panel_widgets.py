from __future__ import annotations

from collections import OrderedDict
from functools import lru_cache
import tkinter as tk
from typing import Any

from PIL import Image, ImageDraw, ImageTk

from mbv import panel_theme as theme


def _parent_background(parent: tk.Misc) -> str:
    try:
        return str(parent.cget("background"))
    except tk.TclError:
        return theme.PANEL


def _rgb(widget: tk.Misc, color: str) -> tuple[int, int, int]:
    return tuple(channel // 257 for channel in widget.winfo_rgb(color))


def _render_rounded_image(
    width: int, height: int, radius: int,
    fill: tuple[int, int, int], outside: tuple[int, int, int],
    outline: tuple[int, int, int] | None = None,
) -> Image.Image:
    """纯像素绘制；大卡片的历史尺寸不驻留缓存。"""
    scale = 3
    image = Image.new("RGB", (width * scale, height * scale), outside)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (0, 0, width * scale - 1, height * scale - 1),
        radius=min(radius, width // 2, height // 2) * scale,
        fill=fill, outline=outline, width=scale if outline is not None else 1,
    )
    return image.resize((width, height), Image.Resampling.LANCZOS)


@lru_cache(maxsize=128)
def _rounded_image(
    width: int, height: int, radius: int,
    fill: tuple[int, int, int], outside: tuple[int, int, int],
    outline: tuple[int, int, int] | None = None,
) -> Image.Image:
    """仅为按钮缓存与 Tk 解释器无关的像素，不跨窗口共享 PhotoImage。"""
    return _render_rounded_image(width, height, radius, fill, outside, outline)


class RoundedCard(tk.Frame):
    """圆角仅为装饰背景；普通 Frame 内容区仍由原生几何管理器布局。"""

    def __init__(
        self, master: tk.Misc, *, padding: int = 8, radius: int = 10,
        bg: str | None = None, **kwargs: Any,
    ) -> None:
        self._fill = str(bg or kwargs.pop("background", theme.PANEL))
        self._outside = _parent_background(master)
        self._radius = max(0, int(radius))
        self._destroying = False
        self._pending_draw: str | None = None
        self._draw_signature: tuple[Any, ...] | None = None
        self._photo: ImageTk.PhotoImage | None = None
        super().__init__(master, bg=self._outside, bd=0, highlightthickness=0, **kwargs)
        self._background = tk.Canvas(self, bg=self._outside, bd=0, highlightthickness=0,
                                     takefocus=False)
        self._background.place(x=0, y=0, relwidth=1, relheight=1)
        self._background_image = self._background.create_image(0, 0, anchor="nw")
        self.body = tk.Frame(self, bg=self._fill, bd=0, highlightthickness=0)
        inset = max(0, int(padding))
        self.body.pack(fill="both", expand=True, padx=inset, pady=inset)
        self.bind("<Configure>", self._queue_draw, add="+")

    def _queue_draw(self, _event: tk.Event | None = None) -> None:
        if not self._destroying and self._pending_draw is None and self.winfo_exists() \
                and self._background.winfo_exists():
            self._pending_draw = self.after_idle(self._draw_background)

    def _draw_background(self) -> None:
        self._pending_draw = None
        if self._destroying or not self.winfo_exists() or not self._background.winfo_exists():
            return
        width, height = max(1, self.winfo_width()), max(1, self.winfo_height())
        signature = (width, height, self._radius, _rgb(self, self._fill), _rgb(self, self._outside))
        if signature == self._draw_signature:
            return
        photo = ImageTk.PhotoImage(_render_rounded_image(*signature), master=self)
        if self._destroying or not self._background.winfo_exists():
            return
        self._photo = photo
        self._background.itemconfigure(self._background_image, image=self._photo)
        self._draw_signature = signature

    def destroy(self) -> None:
        self._destroying = True
        if self._pending_draw is not None:
            self.after_cancel(self._pending_draw)
            self._pending_draw = None
        super().destroy()


class RoundedButton(tk.Button):
    """保留 Tk Button 的命令、禁用、焦点与键鼠类绑定，只增加圆角底图。

    带 image 的 Tk Button 把 width 解释成像素。本类保留调用者的字符宽度，
    通过未映射的临时原生按钮测量布局，避免底图在 Configure 回调中撑大请求尺寸。
    """

    _ALIASES = {"bg": "background", "fg": "foreground", "bd": "borderwidth"}
    _VIRTUAL = {
        "background", "activebackground", "width", "height", "padx", "pady",
        "borderwidth", "highlightthickness", "highlightbackground", "relief", "overrelief",
    }
    _MEASURE = {
        "text", "textvariable", "font", "width", "height", "padx", "pady",
        "borderwidth", "highlightthickness", "wraplength", "justify", "underline", "default",
    }

    def __init__(self, master: tk.Misc, *, radius: int = 8, **kwargs: Any) -> None:
        self._ready = False
        self._destroying = False
        self._radius = max(0, int(radius))
        self._outside = _parent_background(master)
        self._pending_draw: str | None = None
        self._layout_dirty = True
        self._draw_signature: tuple[Any, ...] | None = None
        self._photo: ImageTk.PhotoImage | None = None
        self._photos: OrderedDict[tuple[Any, ...], ImageTk.PhotoImage] = OrderedDict()
        self._text_variable_name = ""
        self._text_trace_command: str | None = None
        defaults: dict[str, Any] = {
            "bg": theme.BUTTON_BG, "fg": theme.FG,
            "activebackground": theme.BUTTON_ACTIVE, "activeforeground": theme.FG,
            "disabledforeground": theme.MUTED, "font": theme.FONT,
            "relief": "flat", "bd": 0, "highlightthickness": 1,
            "highlightbackground": theme.BORDER, "highlightcolor": theme.ACCENT,
            "padx": 10, "pady": 5,
        }
        # 先标准化别名，避免传入 background 时仍与默认 bg 并存。
        options = {self._ALIASES.get(key, key): value for key, value in defaults.items()}
        options.update({self._ALIASES.get(key, key): value for key, value in kwargs.items()})
        super().__init__(master, **options)
        self._logical = {key: tk.Button.cget(self, key) for key in self._VIRTUAL}
        # image 必须先出现，随后原生 width/height 才按像素解释，避免首帧几何膨胀。
        self._photo = ImageTk.PhotoImage(Image.new("RGB", (1, 1), _rgb(self, self._outside)), master=self)
        tk.Button.configure(self, image=self._photo)
        self._ready = True
        self.bind("<Configure>", self._queue_draw, add="+")
        # 不返回 break，不执行 invoke；事件仍完整交给原生 Button 类绑定。
        for sequence in ("<Enter>", "<Leave>", "<ButtonPress-1>", "<ButtonRelease-1>",
                         "<KeyPress-space>", "<KeyRelease-space>", "<FocusIn>", "<FocusOut>"):
            self.bind(sequence, self._queue_draw, add="+")
        self._watch_textvariable()
        self._refresh_layout()
        self._queue_draw()

    def cget(self, key: str) -> Any:
        canonical = self._ALIASES.get(key, key)
        if self._ready and canonical in self._VIRTUAL:
            return self._logical[canonical]
        return tk.Button.cget(self, key)

    __getitem__ = cget

    def configure(self, cnf: Any = None, **kwargs: Any) -> Any:
        if not self._ready:
            return tk.Button.configure(self, cnf, **kwargs)
        if isinstance(cnf, str) and not kwargs:
            result = tk.Button.configure(self, cnf)
            canonical = self._ALIASES.get(cnf, cnf)
            if canonical in self._VIRTUAL and len(result) == 5:
                return (*result[:-1], self._logical[canonical])
            return result
        if cnf is None and not kwargs:
            result = tk.Button.configure(self)
            for key in self._VIRTUAL:
                result[key] = (*result[key][:-1], self._logical[key])
            return result
        changes = dict(cnf or {})
        changes.update(kwargs)
        changes = {self._ALIASES.get(key, key): value for key, value in changes.items()}
        changes = {key: value for key, value in changes.items()
                   if key == "command" or str(self.cget(key)) != str(value)}
        if not changes:
            return None
        for key in ("background", "activebackground", "highlightbackground"):
            if key in changes:
                self.winfo_rgb(changes[key])
        # 原生命令等非视觉参数不进入缓存；仍由 Tk 正常注册/更新与报错。
        native = {key: value for key, value in changes.items() if key not in self._VIRTUAL}
        if native:
            tk.Button.configure(self, **native)
        self._logical.update({key: value for key, value in changes.items() if key in self._VIRTUAL})
        if set(changes) & self._MEASURE:
            self._layout_dirty = True
            self._refresh_layout()
        if "textvariable" in changes:
            self._watch_textvariable()
        if set(changes) - {"command", "takefocus", "cursor"}:
            self._queue_draw()
        return None

    config = configure

    def _watch_textvariable(self) -> None:
        self._remove_text_trace()
        name = str(tk.Button.cget(self, "textvariable"))
        if name:
            # 不创建同名 StringVar 包装；包装析构会 unset 调用者仍在使用的变量。
            self._text_variable_name = name
            self._text_trace_command = self._register(self._text_changed)
            self.tk.call("trace", "add", "variable", name, "write", self._text_trace_command)

    def _remove_text_trace(self) -> None:
        if self._text_trace_command is not None:
            self.tk.call("trace", "remove", "variable", self._text_variable_name,
                         "write", self._text_trace_command)
            self.deletecommand(self._text_trace_command)
            self._text_trace_command = None
        self._text_variable_name = ""

    def _text_changed(self, *_args: Any) -> None:
        self._layout_dirty = True
        self._queue_draw()

    def _refresh_layout(self) -> None:
        if not self._layout_dirty:
            return
        options = {key: self.cget(key) for key in self._MEASURE}
        options.update(relief=self._logical["relief"], takefocus=False)
        probe = tk.Button(self, **options)
        try:
            self._natural_size = (max(1, probe.winfo_reqwidth()), max(1, probe.winfo_reqheight()))
        finally:
            probe.destroy()
        # 宽高是文字和内边距的原生请求。image 可以随实际分配拉伸，但请求不跟随。
        tk.Button.configure(self, width=self._natural_size[0], height=self._natural_size[1],
                            padx=0, pady=0, borderwidth=0, highlightthickness=0,
                            relief="flat", overrelief="", compound="center",
                            background=self._outside, activebackground=self._outside)
        self._layout_dirty = False

    def _queue_draw(self, _event: tk.Event | None = None) -> None:
        if not self._destroying and self._pending_draw is None and self.winfo_exists():
            self._pending_draw = self.after_idle(self._draw_background)

    def _has_focus(self) -> bool:
        # Combobox 下拉列表等 Tcl 控件没有 Python 对象，不能调用 focus_get 解析。
        return str(self.tk.call("focus")) == str(self)

    def _draw_background(self) -> None:
        self._pending_draw = None
        if self._destroying or not self.winfo_exists():
            return
        self._refresh_layout()
        width = self.winfo_width() if self.winfo_width() > 1 else self._natural_size[0]
        height = self.winfo_height() if self.winfo_height() > 1 else self._natural_size[1]
        state = str(tk.Button.cget(self, "state"))
        fill = self._logical["activebackground" if state == "active" else "background"]
        outline = None
        if self._has_focus():
            outline = _rgb(self, str(self.cget("highlightcolor")))
        elif self.winfo_pixels(self._logical["highlightthickness"]) > 0:
            outline = _rgb(self, str(self._logical["highlightbackground"]))
        signature = (width, height, self._radius, _rgb(self, str(fill)), _rgb(self, self._outside), outline)
        if signature == self._draw_signature:
            return
        photo = self._photos.get(signature)
        if photo is None:
            photo = ImageTk.PhotoImage(_rounded_image(*signature), master=self)
            self._photos[signature] = photo
            while len(self._photos) > 6:
                self._photos.popitem(last=False)
        else:
            self._photos.move_to_end(signature)
        self._photo = photo
        tk.Button.configure(self, image=photo)
        self._draw_signature = signature

    def destroy(self) -> None:
        self._destroying = True
        if self._pending_draw is not None:
            self.after_cancel(self._pending_draw)
            self._pending_draw = None
        self._remove_text_trace()
        super().destroy()


__all__ = ["RoundedButton", "RoundedCard"]
