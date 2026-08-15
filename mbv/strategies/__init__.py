from __future__ import annotations

from copy import deepcopy
from typing import Any

from mbv.strategies.base import CombatStrategy
from mbv.strategies.bowman.dynamic import BowmanDynamicStrategy
from mbv.strategies.common.stationary_attack import StationaryAttackStrategy
from mbv.strategies.thief.throwing_star import ThrowingStarSafeStrategy

DEFAULT_STRATEGY = "bowman_dynamic"
_REGISTRY: dict[str, CombatStrategy] = {}


def register_strategy(strategy: CombatStrategy) -> None:
    key = str(strategy.key).strip()
    if not key or key in _REGISTRY:
        raise ValueError(f"职业策略标识无效或重复：{key}")
    _REGISTRY[key] = strategy


def list_strategies() -> tuple[CombatStrategy, ...]:
    return tuple(_REGISTRY.values())


def get_strategy(key: str) -> CombatStrategy:
    try:
        return _REGISTRY[str(key).strip()]
    except KeyError as exc:
        raise RuntimeError(f"未知职业策略：{key}") from exc


def active_strategy(config: dict[str, Any]) -> CombatStrategy:
    return get_strategy(config["strategy"]["active"])


def strategy_settings(config: dict[str, Any], key: str | None = None) -> dict[str, Any]:
    selected = str(key or config["strategy"]["active"])
    return config["strategy"]["options"][selected]


def missing_recognition_data(config: dict[str, Any], strategy: CombatStrategy) -> tuple[str, ...]:
    recognition = config.get("recognition", {})
    settings = strategy_settings(config, strategy.key)
    missing: list[str] = []
    for key in strategy.required_recognition_data:
        if key not in recognition or not bool(recognition.get(f"{key}_captured")):
            missing.append(key)
    for field in strategy.capture_fields:
        if field.settings_path:
            captured = settings.get(field.settings_path)
            has_enabled_item = bool(
                isinstance(captured, list)
                and any(isinstance(item, dict) and bool(item.get("enabled", True)) for item in captured)
            )
        else:
            has_enabled_item = bool(recognition.get(f"{field.recognition_key}_captured"))
        if (
            field.enable_setting
            and bool(settings.get(field.enable_setting))
            and not has_enabled_item
            and field.recognition_key not in missing
        ):
            missing.append(field.recognition_key)
    return tuple(missing)


def _fill_defaults(target: dict[str, Any], defaults: dict[str, Any]) -> None:
    for key, value in defaults.items():
        if isinstance(value, dict):
            child = target.setdefault(key, {})
            if not isinstance(child, dict):
                target[key] = child = {}
            _fill_defaults(child, value)
        else:
            target.setdefault(key, deepcopy(value))


def normalize_strategy_config(config: dict[str, Any]) -> None:
    section = config.setdefault("strategy", {})
    section.setdefault("active", DEFAULT_STRATEGY)
    active = str(section["active"]).strip()
    get_strategy(active)
    options = section.setdefault("options", {})
    if not isinstance(options, dict):
        section["options"] = options = {}
    for strategy in list_strategies():
        settings = options.setdefault(strategy.key, {})
        if not isinstance(settings, dict):
            options[strategy.key] = settings = {}
        _fill_defaults(settings, strategy.default_settings)
        normalize = getattr(strategy, "normalize_settings", None)
        if callable(normalize):
            normalize(settings)


register_strategy(BowmanDynamicStrategy())
register_strategy(StationaryAttackStrategy())
register_strategy(ThrowingStarSafeStrategy())


__all__ = [
    "DEFAULT_STRATEGY",
    "active_strategy",
    "get_strategy",
    "list_strategies",
    "missing_recognition_data",
    "normalize_strategy_config",
    "register_strategy",
    "strategy_settings",
]
