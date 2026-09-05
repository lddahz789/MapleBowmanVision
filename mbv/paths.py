from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config.json"
EXAMPLE_CONFIG = ROOT / "config.example.json"
NEW_MAPLE_PROFILE_DIR = ROOT / "profiles" / "newmaple"
NEW_MAPLE_CONFIG = NEW_MAPLE_PROFILE_DIR / "config.json"
NEW_MAPLE_EXAMPLE_CONFIG = NEW_MAPLE_PROFILE_DIR / "config.example.json"
ASSET_DIR = ROOT / "assets" / "monsters"
MONSTER_FILTER_ASSET_DIR = ROOT / "assets" / "monster_filters"
TEMPLATE_TRASH_DIR = ROOT / "assets" / "template_trash"
PLAYER_ASSET_DIR = ROOT / "assets" / "player"
PLAYER_HEAD_ASSET_DIR = ROOT / "assets" / "player_head"
PLAYER_TITLE_ASSET_DIR = ROOT / "assets" / "player_title"
LOG_DIR = ROOT / "logs"

CLASSIC_PROFILE = "classic"
NEW_MAPLE_PROFILE = "newmaple"
PROFILE_KEYS = (CLASSIC_PROFILE, NEW_MAPLE_PROFILE)


@dataclass(frozen=True)
class AssetPaths:
    root: Path
    monster: Path
    filter: Path
    trash: Path
    player: Path
    head: Path
    title: Path


@dataclass(frozen=True)
class ProfilePaths:
    key: str
    config: Path
    example_config: Path
    assets: AssetPaths


def _asset_paths(root: Path) -> AssetPaths:
    return AssetPaths(
        root=root,
        monster=root / "monsters",
        filter=root / "monster_filters",
        trash=root / "template_trash",
        player=root / "player",
        head=root / "player_head",
        title=root / "player_title",
    )


CLASSIC_PATHS = ProfilePaths(
    CLASSIC_PROFILE,
    DEFAULT_CONFIG,
    EXAMPLE_CONFIG,
    _asset_paths(ROOT / "assets"),
)
NEW_MAPLE_PATHS = ProfilePaths(
    NEW_MAPLE_PROFILE,
    NEW_MAPLE_CONFIG,
    NEW_MAPLE_EXAMPLE_CONFIG,
    _asset_paths(NEW_MAPLE_PROFILE_DIR / "assets"),
)
_PROFILE_PATHS = {
    CLASSIC_PROFILE: CLASSIC_PATHS,
    NEW_MAPLE_PROFILE: NEW_MAPLE_PATHS,
}


def profile_paths(profile: str) -> ProfilePaths:
    key = str(profile).strip().casefold()
    try:
        return _PROFILE_PATHS[key]
    except KeyError as exc:
        supported = "、".join(PROFILE_KEYS)
        raise ValueError(f"不支持的运行档案：{profile!r}，可用档案：{supported}") from exc


def profile_key_from_config(config: dict[str, Any]) -> str:
    return profile_paths(str(config.get("profile", CLASSIC_PROFILE))).key


def asset_paths_from_config(config: dict[str, Any]) -> AssetPaths:
    return profile_paths(profile_key_from_config(config)).assets


def profile_paths_from_config_path(config_path: Path) -> ProfilePaths | None:
    resolved = config_path.resolve()
    for paths in _PROFILE_PATHS.values():
        if resolved == paths.config.resolve():
            return paths
    return None
