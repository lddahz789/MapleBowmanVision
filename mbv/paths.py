from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config.json"
ASSET_DIR = ROOT / "assets" / "monsters"
PLAYER_ASSET_DIR = ROOT / "assets" / "player"
PLAYER_HEAD_ASSET_DIR = ROOT / "assets" / "player_head"
PLAYER_TITLE_ASSET_DIR = ROOT / "assets" / "player_title"
LOG_DIR = ROOT / "logs"
