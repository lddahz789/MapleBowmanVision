"""固定应用图标；与个人档案、采集素材及游戏窗口无关。"""
from __future__ import annotations

import ctypes
from pathlib import Path
import sys
import tkinter as tk


APP_ID = "MapleBowmanVision.ControlPanel"
APP_ICON_PATH = Path(__file__).resolve().parent.parent / "resources" / "app_icon.png"


def configure_app_identity() -> bool:
    """在创建 Tk 前设置 Windows 任务栏分组；失败不阻断启动。"""
    if sys.platform != "win32":
        return False
    try:
        set_app_id = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
        set_app_id.argtypes = [ctypes.c_wchar_p]
        set_app_id.restype = ctypes.c_long
        return set_app_id(APP_ID) == 0
    except (AttributeError, OSError):
        return False


def install_app_icon(root: tk.Tk) -> bool:
    """设置当前窗口及后续子窗口的默认图标，不弹窗、不改变运行状态。"""
    try:
        if not APP_ICON_PATH.is_file():
            return False
        icon = tk.PhotoImage(master=root, file=str(APP_ICON_PATH))
        # iconphoto 在调用时快照图像；保留引用也让 Tk 图片生命周期明确。
        root.iconphoto(True, icon)
        root._mbv_app_icon = icon
        return True
    except (OSError, tk.TclError):
        return False
