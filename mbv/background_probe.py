from __future__ import annotations

import argparse
import json
import time

from mbv.background_capture import BackgroundCapture
from mbv.config import load_config
from mbv.input import window_is_foreground
from mbv.paths import PROFILE_KEYS, profile_paths
from mbv.template_store import template_roots_from_config
from mbv.vision import (
    SceneFeatures, crop, find_detections, load_templates, verify_nameplate_identities,
)
from mbv.window import find_game_window


def main() -> int:
    parser = argparse.ArgumentParser(description="只读检查后台窗口截图和姓名板匹配，不发送按键")
    parser.add_argument("--profile", choices=PROFILE_KEYS, default="newmaple")
    parser.add_argument("--samples", type=int, default=3)
    args = parser.parse_args()
    config = load_config(profile_paths(args.profile).config)
    window = find_game_window(config)
    templates = load_templates(template_roots_from_config(config).player)
    vision = config["vision"]
    capture = BackgroundCapture()
    try:
        for _index in range(max(1, min(30, args.samples))):
            started = time.monotonic()
            frame = capture.capture(window)
            capture_ms = (time.monotonic() - started) * 1000
            combat, _rect = crop(frame, config["regions"]["combat"])
            detections, score, _name = find_detections(
                SceneFeatures(combat), templates,
                float(vision.get("player_template_threshold", 0.7)),
                float(vision.get("player_detection_scale", 0.5)),
                structure_weight=0.55,
            )
            verified = verify_nameplate_identities(combat, detections, templates)
            print(json.dumps({
                "window": window.title,
                "foreground": window_is_foreground(window.hwnd),
                "frame_size": [frame.shape[1], frame.shape[0]],
                "capture_ms": round(capture_ms, 1),
                "nameplate_score": round(score, 4),
                "identity_score": max((d.identity_score for d in verified), default=None),
                "input_tested": False,
            }, ensure_ascii=False))
            time.sleep(0.1)
    finally:
        capture.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
