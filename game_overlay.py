"""兼容入口。HUD 实现在 mbv.overlay。"""
from mbv.overlay import *  # noqa: F403
from mbv.overlay import (  # noqa: F401
    CALIBRATION_HINT,
    _vk_name_from_code,
    _vk_name_from_keysym,
    frozen_frame_image,
    overlay_draw_plan,
    prevent_window_activate,
)
