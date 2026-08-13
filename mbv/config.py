from __future__ import annotations

from datetime import datetime
import json
import time
from pathlib import Path
from typing import Any

from mbv.input import input_delivery
from mbv.paths import ASSET_DIR, LOG_DIR, PLAYER_ASSET_DIR, PLAYER_HEAD_ASSET_DIR, PLAYER_TITLE_ASSET_DIR
from mbv.vision import attack_box_from_config

class SessionLog:
    def __init__(self) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.path = LOG_DIR / f"session-{stamp}.jsonl"

    def write(self, event: str, **data: Any) -> None:
        record = {"ts": time.time(), "event": event, **data}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def png_count(directory: Path) -> int:
    if not directory.is_dir():
        return 0
    return sum(1 for path in directory.glob("*.png"))


def template_counts() -> dict[str, int]:
    return {
        "monster": png_count(ASSET_DIR),
        "player": png_count(PLAYER_ASSET_DIR),
        "head": png_count(PLAYER_HEAD_ASSET_DIR),
        "title": png_count(PLAYER_TITLE_ASSET_DIR),
    }


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        config = json.load(handle)
    if config.get("version") != 1:
        raise RuntimeError("不支持的配置文件版本")
    config.setdefault("input", {})
    config["input"].setdefault("delivery", "foreground")
    input_delivery(config)
    config.setdefault("behavior", {})
    config["behavior"]["bow_attack_box"] = attack_box_from_config(config["behavior"])
    return config


def save_config(path: Path, config: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
