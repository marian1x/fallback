"""Pluggable strategy framework with auto-discovery registry."""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

from .base import BaseStrategy


_STRATEGY_REGISTRY: dict[str, type[BaseStrategy]] = {}


def register_strategy(cls: type[BaseStrategy]) -> type[BaseStrategy]:
    """Class decorator that registers a strategy in the global registry."""
    name = cls.strategy_name
    if not name:
        raise ValueError(f"{cls.__name__} must define a non-empty 'strategy_name'")
    _STRATEGY_REGISTRY[name] = cls
    return cls


def get_strategy(name: str) -> BaseStrategy:
    """Look up a strategy by name and return an instance."""
    cls = _STRATEGY_REGISTRY.get(name)
    if cls is None:
        available = ", ".join(sorted(_STRATEGY_REGISTRY)) or "(none)"
        raise KeyError(
            f"Unknown strategy '{name}'. Available: {available}\n"
            "Hint: run 'hyperview list-strategies' to see registered strategies."
        )
    return cls()


def list_strategies() -> list[str]:
    """Return sorted list of registered strategy names."""
    return sorted(_STRATEGY_REGISTRY)


def _auto_discover() -> None:
    """Import all strategy modules in this package so they self-register.

    Scans for top-level ``.py`` files inside the ``strategy/`` package,
    skipping private modules (names starting with ``_``).
    """
    package_dir = Path(__file__).resolve().parent
    for finder, module_name, is_pkg in pkgutil.iter_modules([str(package_dir)]):
        if module_name.startswith("_"):
            continue
        importlib.import_module(f"{__name__}.{module_name}")


_auto_discover()

__all__ = [
    "BaseStrategy",
    "register_strategy",
    "get_strategy",
    "list_strategies",
]
