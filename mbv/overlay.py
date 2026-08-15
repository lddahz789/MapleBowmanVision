from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import queue
import os
import threading
import time
import tkinter as tk
from typing import Any, Callable

from PIL import Image, ImageTk


user32 = ctypes.windll.user32

GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
HWND_TOPMOST = wintypes.HWND(-1)
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
SWP_SHOWWINDOW = 0x0040
WDA_EXCLUDEFROMCAPTURE = 0x00000011
LONG_PTR = ctypes.c_ssize_t
user32.GetWindowLongPtrW.restype = LONG_PTR
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetWindowLongPtrW.restype = LONG_PTR
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, LONG_PTR]
TRANSPARENT = "#010101"
INTERACTIVE_CAPTURE_BG = "#020202"
INTERACTIVE_CAPTURE_ALPHA = 0.02
CALIBRATION_HINT = "F7 显示 Debug 框"


def overlay_draw_plan(state: dict[str, Any]) -> dict[str, Any]:
    """Decide which HUD layers to paint. Hide() is independent of this toggle."""
    show_calibration = bool(state.get("show_calibration", True))
    return {
        "show_calibration": show_calibration,
        "banner": str(state.get("banner", "")) if show_calibration else "",
        "hint": "" if show_calibration else CALIBRATION_HINT,
        "notice": str(state.get("notice", "") or ""),
    }


def debug_item_enabled(state: dict[str, Any], item: str) -> bool:
    """Return whether a Debug layer belongs to the active all/single-item view."""
    selected = str(state.get("debug_item") or "").strip()
    return not selected or selected == item


def _top_level_hwnd(widget: tk.Misc) -> int:
    hwnd = int(widget.winfo_id())
    while True:
        parent = int(user32.GetParent(hwnd) or 0)
        if not parent:
            return hwnd
        hwnd = parent


def _exclude_from_capture(hwnd: int) -> None:
    if os.environ.get("MBV_QA_CAPTURE") == "1":
        return
    # Windows 10 2004+ 支持；旧系统失败时 HUD 仍可工作，只是可能被截图采入。
    try:
        user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
    except Exception:
        pass


def prevent_window_activate(hwnd: int) -> None:
    """Keep a helper window from taking foreground away from the game."""
    hwnd = int(hwnd)
    style = int(user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE))
    user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE)
    user32.SetWindowPos(
        hwnd,
        None,
        0,
        0,
        0,
        0,
        SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
    )


def frozen_frame_image(frame: Any, width: int, height: int) -> Image.Image:
    """将 OpenCV BGR 帧转换成叠加层使用的 RGB 静态画面。"""
    frame_height, frame_width = frame.shape[:2]
    image = Image.frombytes(
        "RGB",
        (frame_width, frame_height),
        frame.tobytes(),
        "raw",
        "BGR",
    )
    if (frame_width, frame_height) != (width, height):
        image = image.resize((width, height), Image.Resampling.BILINEAR)
    return image


def _rect(roi: dict[str, float], width: int, height: int) -> tuple[int, int, int, int]:
    return (
        int(round(float(roi["x"]) * width)),
        int(round(float(roi["y"]) * height)),
        int(round(float(roi["w"]) * width)),
        int(round(float(roi["h"]) * height)),
    )


class RuntimeOverlay:
    """主线程中的透明点击穿透 HUD；识别线程通过队列更新。"""

    def __init__(self, parent: tk.Misc | None = None) -> None:
        self._updates: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=1)
        self._exit_handler: Callable[[], None] | None = None
        self._owns_loop = parent is None
        root = tk.Tk() if parent is None else tk.Toplevel(parent)
        root.withdraw()
        root.overrideredirect(True)
        root.configure(bg=TRANSPARENT)
        root.wm_attributes("-transparentcolor", TRANSPARENT)
        root.wm_attributes("-topmost", True)
        canvas = tk.Canvas(root, bg=TRANSPARENT, highlightthickness=0, bd=0)
        canvas.pack(fill="both", expand=True)
        root.update_idletasks()
        hwnd = _top_level_hwnd(root)
        style = int(user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE))
        style |= WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style)
        _exclude_from_capture(hwnd)

        exit_root = tk.Toplevel(root)
        exit_root.withdraw()
        exit_root.overrideredirect(True)
        exit_root.configure(bg="#7a2020")
        exit_root.wm_attributes("-topmost", True)
        exit_button = tk.Button(
            exit_root,
            text="退出程序",
            command=self._request_exit,
            bg="#a52828",
            fg="white",
            activebackground="#d43c3c",
            activeforeground="white",
            relief="flat",
            bd=0,
            font=("Microsoft YaHei UI", 12, "bold"),
            cursor="hand2",
        )
        exit_button.pack(fill="both", expand=True)
        exit_root.update_idletasks()
        exit_hwnd = _top_level_hwnd(exit_root)
        exit_style = int(user32.GetWindowLongPtrW(exit_hwnd, GWL_EXSTYLE)) | WS_EX_TOOLWINDOW
        exit_style &= ~WS_EX_TRANSPARENT
        exit_style &= ~WS_EX_NOACTIVATE
        user32.SetWindowLongPtrW(exit_hwnd, GWL_EXSTYLE, exit_style)
        _exclude_from_capture(exit_hwnd)

        self._root = root
        self._canvas = canvas
        self._hwnd = hwnd
        self._exit_root = exit_root
        self._exit_button = exit_button
        self._exit_hwnd = exit_hwnd
        self._closed = False
        self._visible = True
        self._last_state: dict[str, Any] | None = None

    def set_exit_handler(self, handler: Callable[[], None]) -> None:
        self._exit_handler = handler

    def _request_exit(self) -> None:
        self._exit_button.configure(text="正在退出…", state="disabled")
        if self._exit_handler is not None:
            self._exit_handler()

    def update(self, state: dict[str, Any]) -> None:
        try:
            self._updates.put_nowait(state)
        except queue.Full:
            try:
                self._updates.get_nowait()
            except queue.Empty:
                pass
            try:
                self._updates.put_nowait(state)
            except queue.Full:
                pass

    def close(self) -> None:
        try:
            self._updates.put_nowait(None)
        except queue.Full:
            try:
                self._updates.get_nowait()
                self._updates.put_nowait(None)
            except (queue.Empty, queue.Full):
                pass

    def hide(self) -> None:
        self._visible = False
        self._root.withdraw()
        self._exit_root.withdraw()

    def show(self) -> None:
        if self._closed:
            return
        self._visible = True
        if self._last_state is not None:
            try:
                self._draw(self._root, self._canvas, self._hwnd, self._last_state)
            except tk.TclError:
                self._closed = True

    def start_polling(self) -> None:
        self._root.after(0, self._poll)

    def mainloop(self) -> None:
        self.start_polling()
        if self._owns_loop:
            self._root.mainloop()

    def _poll(self) -> None:
        latest: dict[str, Any] | None | object = ...
        while True:
            try:
                latest = self._updates.get_nowait()
            except queue.Empty:
                break
        if latest is None:
            self._closed = True
            self._root.destroy()
            return
        if latest is not ...:
            self._last_state = latest
            if self._visible:
                self._draw(self._root, self._canvas, self._hwnd, latest)
        if not self._closed:
            self._root.after(25, self._poll)

    def _draw(self, root: tk.Tk, canvas: tk.Canvas, hwnd: int, state: dict[str, Any]) -> None:
        left = int(state["left"])
        top = int(state["top"])
        width = int(state["width"])
        height = int(state["height"])
        root.geometry(f"{width}x{height}+{left}+{top}")
        root.deiconify()
        user32.SetWindowPos(
            hwnd,
            HWND_TOPMOST,
            left,
            top,
            width,
            height,
            SWP_NOACTIVATE | SWP_SHOWWINDOW,
        )
        exit_width = 116
        exit_height = 34
        exit_left = left + max(0, width - exit_width)
        self._exit_root.geometry(f"{exit_width}x{exit_height}+{exit_left}+{top}")
        self._exit_root.deiconify()
        user32.SetWindowPos(
            self._exit_hwnd,
            HWND_TOPMOST,
            exit_left,
            top,
            exit_width,
            exit_height,
            SWP_NOACTIVATE | SWP_SHOWWINDOW,
        )
        self._exit_button.configure(text="退出程序")
        canvas.delete("all")
        font = ("Microsoft YaHei UI", max(12, int(height * 0.016)), "bold")
        small = ("Microsoft YaHei UI", max(11, int(height * 0.014)))
        armed = bool(state.get("armed"))
        mode_color = "#ff4545" if armed else "#4cff79"
        plan = overlay_draw_plan(state)
        canvas.create_rectangle(0, 0, width, 34, fill="#151515", outline="")
        if plan["show_calibration"]:
            canvas.create_text(9, 17, anchor="w", text=plan["banner"], fill=mode_color, font=font)
        else:
            canvas.create_text(9, 17, anchor="w", text=plan["hint"], fill="#c8c8c8", font=font)

        notice = plan["notice"]
        if notice:
            canvas.create_rectangle(0, 35, width, 70, fill="#7a2020", outline="")
            canvas.create_text(9, 52, anchor="w", text=notice, fill="white", font=font)

        if not plan["show_calibration"]:
            return

        roi_specs = [
            ("hp_bar", "hp_roi", "血量", "#ff4040", f"{float(state.get('hp', 0)):.0%}"),
            ("mp_bar", "mp_roi", "蓝量", "#40aaff", f"{float(state.get('mp', 0)):.0%}"),
            ("minimap", "minimap_roi", "小地图", "#ffe04a", ""),
            ("combat_region", "combat_roi", "识别区域", "#55ff88", ""),
        ]
        for item, key, label, color, value in roi_specs:
            if not debug_item_enabled(state, item):
                continue
            roi = state.get(key)
            if not roi:
                continue
            x, y, w, h = _rect(roi, width, height)
            canvas.create_rectangle(x, y, x + w, y + h, outline=color, width=2)
            canvas.create_text(x + 3, y + 3, anchor="nw", text=f"{label} {value}".strip(), fill=color, font=small)

        marker = state.get("marker_screen")
        if marker and debug_item_enabled(state, "player_marker"):
            mx, my = marker
            canvas.create_oval(mx - 6, my - 6, mx + 6, my + 6, outline="#3cff55", width=3)

        attack_range = state.get("attack_range_box")
        if attack_range and debug_item_enabled(state, "targeting_range"):
            x, y, w, h = attack_range
            canvas.create_rectangle(x, y, x + w, y + h, outline="#ffe04a", width=1)
            canvas.create_text(
                x + 3,
                y + 3,
                anchor="nw",
                text=str(state.get("attack_range_label", "有效索敌区")),
                fill="#ffe04a",
                font=small,
            )

        for area in state.get("strategy_area_boxes", []):
            area_key = str(area.get("key", "")) if isinstance(area, dict) else ""
            if not debug_item_enabled(state, area_key):
                continue
            box = area.get("box") if isinstance(area, dict) else None
            if not box:
                continue
            x, y, w, h = box
            label = str(area.get("label", "策略安全区"))
            canvas.create_rectangle(x, y, x + w, y + h, outline="#ff66d9", width=2)
            canvas.create_text(x + 3, y + 3, anchor="nw", text=label, fill="#ff8fe3", font=small)

        overlap_ratio = state.get("close_overlap_ratio")
        overlap_threshold = state.get("close_overlap_threshold")
        if (
            not state.get("debug_item")
            and overlap_ratio is not None
            and overlap_threshold is not None
        ):
            triggered = float(overlap_ratio) >= float(overlap_threshold)
            color = "#ff654d" if triggered else "#ffb347"
            span = state.get("close_overlap_span")
            if span:
                sx, sy, sw = span
                canvas.create_line(sx, sy, sx + sw, sy, fill=color, width=5)
            combat = state.get("combat_roi")
            if combat:
                x, y, _w, _h = _rect(combat, width, height)
                canvas.create_text(
                    x + 3,
                    y + 42,
                    anchor="nw",
                    text=f"近身重叠 {float(overlap_ratio):.0%} / {float(overlap_threshold):.0%}",
                    fill=color,
                    font=small,
                )

        if not state.get("debug_item"):
            for candidate in state.get("monster_boxes", []):
                x, y, w, h = candidate
                canvas.create_rectangle(x, y, x + w, y + h, outline="#b94cff", width=1)

        player_box = state.get("player_box")
        if player_box and debug_item_enabled(state, "player"):
            x, y, w, h = player_box
            player_score = float(state.get("player_score", -1))
            player_source = str(state.get("player_source", "玩家定位"))
            stationary = player_source == "小地图静止确认"
            player_color = "#ffb347" if stationary else "#00e5ff"
            rectangle_options = {"outline": player_color, "width": 3}
            if stationary:
                rectangle_options["dash"] = (4, 3)
            canvas.create_rectangle(x, y, x + w, y + h, **rectangle_options)
            canvas.create_text(
                x,
                max(35, y - 20),
                anchor="nw",
                text=f"玩家·{player_source} {player_score:.2f}" if player_score >= 0 else f"玩家·{player_source}",
                fill=player_color,
                font=font,
            )

        chase = state.get("chase_box")
        if chase and not state.get("debug_item"):
            x, y, w, h = chase
            distance = state.get("target_distance_px")
            direction = str(state.get("target_direction", ""))
            chase_text = "追踪目标"
            if distance is not None:
                chase_text += f"｜{direction}侧 {int(distance)}px"
            canvas.create_rectangle(x, y, x + w, y + h, outline="#ff9d2e", width=3)
            canvas.create_text(
                x,
                max(35, y - 20),
                anchor="nw",
                text=chase_text,
                fill="#ffb04d",
                font=font,
            )

        monster = state.get("monster_box")
        if monster and not state.get("debug_item"):
            x, y, w, h = monster
            canvas.create_rectangle(x, y, x + w, y + h, outline="#39ff72", width=3)
            distance = state.get("target_distance_px")
            direction = str(state.get("target_direction", ""))
            target_text = f"当前目标 {float(state.get('monster_score', 0)):.2f}"
            if distance is not None:
                target_text += f"｜{direction}侧 {int(distance)}px"
            canvas.create_text(
                x,
                max(35, y - 20),
                anchor="nw",
                text=target_text,
                fill="#39ff72",
                font=font,
            )
        elif not state.get("debug_item") and float(state.get("monster_score", -1)) >= 0:
            combat = state.get("combat_roi")
            if combat:
                x, y, _w, _h = _rect(combat, width, height)
                canvas.create_text(
                    x + 3,
                    y + 24,
                    anchor="nw",
                    text=f"未确认：{float(state['monster_score']):.2f} / {float(state['monster_threshold']):.2f}",
                    fill="#ffb33b",
                    font=small,
                )


@dataclass
class InteractiveResult:
    cancelled: bool
    rectangle: tuple[int, int, int, int] | None = None
    point: tuple[int, int] | None = None
    key: str | None = None


_KEYSYM_ALIASES = {
    "shift_l": "shift",
    "shift_r": "shift",
    "control_l": "ctrl",
    "control_r": "ctrl",
    "alt_l": "alt",
    "alt_r": "alt",
    "escape": "esc",
    "return": "enter",
    "prior": "pageup",
    "next": "pagedown",
}


def _vk_name_from_code(vk_map: dict[str, int], code: int) -> str | None:
    for name, vk in vk_map.items():
        if vk == code:
            return name
    return None


def _vk_name_from_keysym(vk_map: dict[str, int], keysym: str) -> str | None:
    lowered = keysym.lower()
    name = _KEYSYM_ALIASES.get(lowered, lowered)
    return name if name in vk_map else None


def _pressed_vk_name(vk_map: dict[str, int], ignore: set[int]) -> str | None:
    esc = vk_map.get("esc")
    if esc is not None and esc not in ignore and user32.GetAsyncKeyState(esc) & 0x8000:
        return "esc"
    for name, vk in vk_map.items():
        if name == "esc" or vk in ignore:
            continue
        if user32.GetAsyncKeyState(vk) & 0x8000:
            return name
    return None


def _snapshot_held_vks(vk_map: dict[str, int]) -> set[int]:
    held: set[int] = set()
    for vk in vk_map.values():
        if user32.GetAsyncKeyState(vk) & 0x8000:
            held.add(vk)
    return held


def interactive_overlay(
    window: Any,
    title: str,
    mode: str,
    guide_rect: tuple[int, int, int, int] | None = None,
    parent: tk.Misc | None = None,
    vk_map: dict[str, int] | None = None,
    frozen_frame: Any | None = None,
) -> InteractiveResult:
    """直接覆盖游戏进行框选；传入 frozen_frame 时显示该静态游戏帧。"""
    # Windows 会把透明色键像素当成鼠标穿透区。因此交互框选使用两个窗口：
    # 几乎不可见的 capture_root 铺满客户区并接收鼠标，visual_root 只绘制提示和选框。
    owns_loop = parent is None
    root = tk.Tk() if owns_loop else tk.Toplevel(parent)
    root.withdraw()
    root.overrideredirect(True)
    root.configure(bg=INTERACTIVE_CAPTURE_BG)
    root.wm_attributes("-alpha", INTERACTIVE_CAPTURE_ALPHA)
    root.wm_attributes("-topmost", True)
    root.geometry(f"{window.width}x{window.height}+{window.left}+{window.top}")
    capture_canvas = tk.Canvas(
        root,
        bg=INTERACTIVE_CAPTURE_BG,
        highlightthickness=0,
        bd=0,
        cursor="arrow" if mode == "key" else "crosshair",
    )
    capture_canvas.pack(fill="both", expand=True)

    visual_root = tk.Toplevel(root)
    visual_root.withdraw()
    visual_root.overrideredirect(True)
    visual_root.configure(bg=TRANSPARENT)
    visual_root.wm_attributes("-transparentcolor", TRANSPARENT)
    visual_root.wm_attributes("-topmost", True)
    visual_root.geometry(f"{window.width}x{window.height}+{window.left}+{window.top}")
    canvas = tk.Canvas(visual_root, bg=TRANSPARENT, highlightthickness=0, bd=0)
    canvas.pack(fill="both", expand=True)

    frozen_photo: ImageTk.PhotoImage | None = None
    if frozen_frame is not None:
        frozen_image = frozen_frame_image(frozen_frame, window.width, window.height)
        frozen_photo = ImageTk.PhotoImage(frozen_image, master=visual_root)

    root.update_idletasks()
    visual_root.update_idletasks()
    hwnd = _top_level_hwnd(root)
    visual_hwnd = _top_level_hwnd(visual_root)

    style = int(user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)) | WS_EX_LAYERED | WS_EX_TOOLWINDOW
    style &= ~WS_EX_TRANSPARENT
    style &= ~WS_EX_NOACTIVATE
    user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style)
    visual_style = int(user32.GetWindowLongPtrW(visual_hwnd, GWL_EXSTYLE))
    visual_style |= WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
    user32.SetWindowLongPtrW(visual_hwnd, GWL_EXSTYLE, visual_style)

    _exclude_from_capture(hwnd)
    _exclude_from_capture(visual_hwnd)
    root.deiconify()
    visual_root.deiconify()
    user32.SetWindowPos(hwnd, HWND_TOPMOST, window.left, window.top, window.width, window.height, SWP_SHOWWINDOW)
    user32.SetWindowPos(
        visual_hwnd,
        HWND_TOPMOST,
        window.left,
        window.top,
        window.width,
        window.height,
        SWP_NOACTIVATE | SWP_SHOWWINDOW,
    )
    user32.SetForegroundWindow(hwnd)
    root.focus_force()

    start: list[tuple[int, int]] = []
    end: list[tuple[int, int]] = []
    selected_point: list[tuple[int, int]] = []
    result = InteractiveResult(cancelled=True)

    finished = [False]
    held_vks: set[int] = set()

    def redraw() -> None:
        canvas.delete("all")
        if frozen_photo is not None:
            canvas.create_image(0, 0, anchor="nw", image=frozen_photo)
        canvas.create_rectangle(0, 0, window.width, 42, fill="#151515", outline="")
        if mode == "key":
            suffix = "请按下要绑定的键，Esc 取消"
        elif mode == "rectangle":
            suffix = "静态帧｜拖动框选｜回车确认｜R 重选｜Esc 取消" if frozen_photo is not None else "整个游戏区域都可拖框｜回车确认｜R 重选｜Esc 取消"
        else:
            suffix = "点击目标中心｜回车确认｜R 重选｜Esc 取消"
        canvas.create_text(10, 21, anchor="w", text=f"{title}｜{suffix}", fill="#fff04a", font=("Microsoft YaHei UI", 16, "bold"))
        if guide_rect:
            x, y, w, h = guide_rect
            canvas.create_rectangle(x, y, x + w, y + h, outline="#ffe04a", width=2)
        if start and end:
            x1, y1 = start[0]
            x2, y2 = end[0]
            canvas.create_rectangle(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2), outline="#00ffff", width=3)
        if selected_point:
            x, y = selected_point[0]
            canvas.create_line(x - 12, y, x + 12, y, fill="#00ff55", width=3)
            canvas.create_line(x, y - 12, x, y + 12, fill="#00ff55", width=3)

    dragging = [False]

    def finish_key(name: str | None) -> None:
        nonlocal result
        if finished[0]:
            return
        finished[0] = True
        if name and name != "esc":
            result = InteractiveResult(cancelled=False, key=name)
        try:
            root.destroy()
        except tk.TclError:
            pass

    def mouse_down(event: tk.Event) -> None:
        if mode == "key":
            return
        if mode == "point":
            selected_point[:] = [(event.x, event.y)]
        else:
            start[:] = [(event.x, event.y)]
            end[:] = [(event.x, event.y)]
            dragging[0] = True
        redraw()

    def mouse_move(event: tk.Event) -> None:
        if mode == "rectangle" and dragging[0]:
            end[:] = [(event.x, event.y)]
            redraw()

    def mouse_up(event: tk.Event) -> None:
        if mode == "rectangle" and dragging[0]:
            end[:] = [(event.x, event.y)]
            dragging[0] = False
            redraw()

    def confirm(_event: tk.Event | None = None) -> None:
        nonlocal result
        if mode == "point" and selected_point:
            result = InteractiveResult(False, point=selected_point[0])
            root.destroy()
        elif mode == "rectangle" and start and end:
            x1, y1 = start[0]
            x2, y2 = end[0]
            left, top = min(x1, x2), min(y1, y2)
            right, bottom = max(x1, x2), max(y1, y2)
            if right - left >= 4 and bottom - top >= 4:
                result = InteractiveResult(False, rectangle=(left, top, right - left, bottom - top))
                root.destroy()

    def reset(_event: tk.Event | None = None) -> None:
        start.clear()
        end.clear()
        selected_point.clear()
        dragging[0] = False
        redraw()

    def cancel(_event: tk.Event | None = None) -> None:
        if mode == "key":
            finish_key(None)
            return
        root.destroy()

    def on_keypress(event: tk.Event) -> None:
        if mode != "key" or vk_map is None:
            return
        name = _vk_name_from_code(vk_map, int(event.keycode))
        if name is None:
            name = _vk_name_from_keysym(vk_map, str(event.keysym))
        if name == "esc":
            finish_key(None)
        elif name:
            finish_key(name)

    def poll_key() -> None:
        if finished[0] or vk_map is None:
            return
        try:
            if not root.winfo_exists():
                return
        except tk.TclError:
            return
        name = _pressed_vk_name(vk_map, held_vks)
        if name == "esc":
            finish_key(None)
            return
        if name:
            finish_key(name)
            return
        released = {vk for vk in held_vks if not (user32.GetAsyncKeyState(vk) & 0x8000)}
        held_vks.difference_update(released)
        root.after(20, poll_key)

    def start_key_poll() -> None:
        if vk_map is None:
            return
        held_vks.update(_snapshot_held_vks(vk_map))
        poll_key()

    capture_canvas.bind("<ButtonPress-1>", mouse_down)
    capture_canvas.bind("<B1-Motion>", mouse_move)
    capture_canvas.bind("<ButtonRelease-1>", mouse_up)
    if mode == "key":
        if not vk_map:
            raise ValueError("按键采集需要提供 vk_map")
        root.bind("<KeyPress>", on_keypress)
        root.bind("<Escape>", cancel)
        capture_canvas.bind("<KeyPress>", on_keypress)
        capture_canvas.focus_set()
        root.after(80, start_key_poll)
    else:
        root.bind("<Return>", confirm)
        root.bind("<space>", confirm)
        root.bind("r", reset)
        root.bind("R", reset)
        root.bind("<Escape>", cancel)
    redraw()
    if owns_loop:
        root.mainloop()
    else:
        try:
            root.grab_set()
        except tk.TclError:
            pass
        parent.wait_window(root)
    return result
